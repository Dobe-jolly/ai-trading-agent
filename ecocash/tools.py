"""The tools Claude can call, with masking, and the evidence they leave for the rules in code.

Every identifier the model passes must already be on the ticket (form, thread, files read) or have
come back from an earlier lookup on this ticket, so it cannot go looking up other customers.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from agentcore import sql
from .ecocash import BY_KEY, ESCALATING, AttemptStore, BankAccounts, LinkAttempt, LinkRecords, Lookup
from agentcore.metabase import MetabaseError
from .normalize import account_key, format_timestamp, id_key, mobile_key, now_local
from .osticket import Identifiers, Ticket, find_identifiers
from agentcore.pii import Pseudonymizer

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 9000

IDENTIFIER_PROPS = {
    "mobile": {"type": "string", "description": "Mobile number, e.g. <MOBILE_1>."},
    "account_number": {"type": "string", "description": "Bank account number, e.g. <NUMBER_1>."},
    "id_number": {"type": "string", "description": "National ID, e.g. <NATIONAL_ID_1>."},
}
FIND_ATTEMPTS_TOOL = {
    "name": "find_ecocash_attempts",
    "description": (
        "Every EcoCash link and delink request for a client in the last 60 days, newest first, from the "
        "registration log: when, who started it (client on USSD, branch staff, account opening), the mobile, "
        "account and national ID sent, and the outcome with EcoCash's error and what it means. Give any of mobile, "
        "account_number or id_number (OR). The summary says where the client stands now and suggests the standard "
        "reply. Names are never returned; name_sent_matches_ticket says whether the name the bank sent appears on "
        "the ticket."),
    "input_schema": {"type": "object", "properties": IDENTIFIER_PROPS},
}
LINK_RECORD_TOOL = {
    "name": "check_bank_link_record",
    "description": (
        "The bank's own record of successful EcoCash links (Postilion eftc_ecocash): which account is linked to "
        "which mobile, since when, and its status. Use it to confirm an existing link, e.g. for 'already linked' "
        "errors (is the account linked to the client's current number or to another one?)."),
    "input_schema": {"type": "object", "properties": {k: IDENTIFIER_PROPS[k] for k in ("mobile", "account_number")}},
}
BANK_ACCOUNT_TOOL = {
    "name": "check_bank_account",
    "description": "Core banking: whether the account exists, its currency, and the client's mobile number on file.",
    "input_schema": {"type": "object", "properties": {"account_number": IDENTIFIER_PROPS["account_number"]},
                     "required": ["account_number"]},
}


HEALTH_TOOL = {
    "name": "get_ecocash_service_health",
    "description": (
        "How EcoCash is answering everyone right now, not just this client: link and delink requests in the last "
        "few hours by result (linked, each EcoCash error, no answer), next to the same hours yesterday. Use it when "
        "a failure might be on EcoCash's side (system errors, no answer, an error you do not recognise)."),
    "input_schema": {"type": "object", "properties": {
        "hours": {"type": "integer", "minimum": 1, "maximum": 24, "description": "How many recent hours, default 3."}}},
}
PENDING_TOOL = {
    "name": "get_pending_profile_changes",
    "description": (
        "Changes to the account holder's bank profile captured at a branch but not yet authorised (for example a "
        "corrected national ID or a new mobile awaiting approval). Use it for KYC failures or when the thread says "
        "the details were recently changed."),
    "input_schema": {"type": "object", "properties": {"account_number": IDENTIFIER_PROPS["account_number"]},
                     "required": ["account_number"]},
}


@dataclass
class Evidence:
    """What the lookups found, for the guardrails and the log."""
    lookups: list[Lookup] = field(default_factory=list)
    attempts: dict[str, LinkAttempt] = field(default_factory=dict)  # by attempt id
    searched: dict[str, set] = field(default_factory=lambda: {"mobile": set(), "account": set(), "id": set()})
    known: Identifiers = field(default_factory=Identifiers)  # identifiers the model may search with
    link_records: list[dict[str, Any]] = field(default_factory=list)
    accounts_checked: dict[str, dict[str, Any]] = field(default_factory=dict)
    check_errors: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    other_lookups: list[dict[str, Any]] = field(default_factory=list)  # service health, pending changes: for notes

    @property
    def latest(self) -> Optional[LinkAttempt]:
        stamped = [a for a in self.attempts.values() if a.when is not None]
        return max(stamped, key=lambda a: a.when) if stamped else None

    @property
    def status(self) -> str:
        """linked | unlinked | <error type> | none: where the latest attempt leaves the client."""
        latest = self.latest
        return latest.type_key if latest else "none"

    @property
    def statuses(self) -> dict[str, str]:
        latest = self.latest
        return {"latest": latest.type_key} if latest else {}

    def summary(self) -> dict[str, Any]:
        latest = self.latest
        return {"status": self.status, "attempts_seen": len(self.attempts),
                "latest_attempt": latest.id if latest else None,
                "latest_when": format_timestamp(latest.when) if latest else None,
                "link_records": len(self.link_records), "accounts_checked": len(self.accounts_checked),
                "errors": self.check_errors[:3]}


def standard_reply(attempt: Optional[LinkAttempt], ticket: Optional[Ticket], link_records: Optional[list] = None
                   ) -> tuple[str, str]:
    """(suggested action, reply to the branch) for the latest attempt. Real values; masked by the caller."""
    if attempt is None:
        return ("request_info", "We could not find an EcoCash link attempt for the client's details on this ticket "
                "in the last 60 days. Kindly confirm the client's EcoCash mobile number and account number, and ask "
                "the client to try linking again so the error is recorded.")
    mobile = attempt.mobile or "the client's number"
    account = attempt.account or "the client's account"
    when = format_timestamp(attempt.when)
    if attempt.outcome == "linked":
        return ("reply_and_close", f"Account {account} was linked to EcoCash on {mobile} successfully on {when}. "
                "The client can now use the link; if the EcoCash menu still does not show the account, kindly ask "
                "the client to restart the phone and try again.")
    if attempt.outcome == "unlinked":
        return ("request_info", f"Account {account} was delinked from EcoCash on {when}. Kindly ask the client to "
                "link the account again and let us know on this ticket if it fails.")
    error = attempt.error or BY_KEY["unknown"]
    if error.key in ESCALATING:
        return ("escalate", "")
    if error.key == "already_linked":
        return ("request_info", error.guidance.format(mobile=mobile, account=account))
    if error.key == "kyc_mismatch":
        sent = id_key(attempt.id_number)
        on_ticket = find_identifiers(ticket.owner_text()).ids if ticket is not None else set()
        if not sent or not on_ticket:
            return ("request_info", error.guidance + " Kindly share the client's national ID number (or a copy of "
                    "the ID) so we can compare it with the details the bank sent to EcoCash.")
        if sent in on_ticket:
            return ("reply_and_close", error.guidance + " The national ID the bank sent matches the client's ID on "
                    "this ticket, so the difference is on EcoCash's side. Kindly advise the client to visit an EcoCash "
                    "shop to update their EcoCash details, then link the account again.")
        other = sorted(on_ticket)[0]
        return ("reply_and_close", error.guidance + f" The national ID the bank sent ({attempt.id_number}) differs "
                f"from the client's ID on this ticket ({other}). Kindly correct the client's ID number on the bank "
                "profile, then ask the client to link the account again.")
    return ("reply_and_close", error.guidance.format(mobile=mobile, account=account))


class Toolbox:
    def __init__(self, attempts: AttemptStore, link_records: Optional[LinkRecords] = None,
                 accounts: Optional[BankAccounts] = None, lookback_days: int = 60) -> None:
        self.attempts = attempts
        self.link_records = link_records
        self.accounts = accounts
        self.lookback_days = lookback_days
        self.as_of: Optional[datetime] = None  # closed-ticket replays: what was known then

    def schemas(self) -> list[dict[str, Any]]:
        tools = [FIND_ATTEMPTS_TOOL]
        if self.link_records is not None:
            tools.append(LINK_RECORD_TOOL)
        if self.accounts is not None:
            tools.append(BANK_ACCOUNT_TOOL)
        tools.append(HEALTH_TOOL)
        if self.accounts is not None and self.accounts.pending_sql:
            tools.append(PENDING_TOOL)
        return tools

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Evidence,
            ticket: Optional[Ticket]) -> tuple[str, bool]:
        """Returns (masked result text, is_error)."""
        if ticket is not None and not evidence.known:
            evidence.known.update(ticket.identifiers())
        try:
            values = self._identifiers(pseudo.unmask_obj(args or {}), evidence, ticket is not None)
            if name == "find_ecocash_attempts":
                data = self._find(values, evidence, ticket)
            elif name == "check_bank_link_record" and self.link_records is not None:
                data = self._link_record(values, evidence)
            elif name == "check_bank_account" and self.accounts is not None:
                if not values.get("account_number"):
                    raise ValueError("account_number is required")
                data = self.accounts.check(values["account_number"])
                evidence.accounts_checked[account_key(values["account_number"]) or ""] = data
            elif name == "get_ecocash_service_health":
                data = self._health(args.get("hours"))
                evidence.other_lookups.append({"tool": name, "summary": data["summary"]})
            elif name == "get_pending_profile_changes" and self.accounts is not None and self.accounts.pending_sql:
                if not values.get("account_number"):
                    raise ValueError("account_number is required")
                data = self.accounts.pending_changes(values["account_number"])
                evidence.other_lookups.append({"tool": name, "summary": f"{len(data.get('pending_changes') or [])} "
                                                                        "pending change(s)" if data.get("exists") else
                                               "account not found"})
            else:
                return f"Unknown tool {name!r}.", True
        except (ValueError, sql.UnsafeValue) as exc:
            return f"Invalid input: {pseudo.mask(str(exc))}", True
        except MetabaseError as exc:
            evidence.check_errors.append(f"{name}: {exc}"[:300])
            return pseudo.mask(f"Lookup failed (data source error): {exc}. You may retry once.")[:800], True
        evidence.tools_used.append(name)
        self._learn(data, evidence)
        text = json.dumps(pseudo.mask_obj(data), default=str, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...[truncated]"
        return text, False

    # ---- helpers -----------------------------------------------------------------------
    def _identifiers(self, args: dict[str, Any], evidence: Evidence, enforce: bool) -> dict[str, str]:
        out: dict[str, str] = {}
        checks = (("mobile", mobile_key, evidence.known.mobiles), ("account_number", account_key, evidence.known.accounts),
                  ("id_number", id_key, evidence.known.ids))
        for name, key_fn, known in checks:
            raw = str(args.get(name) or "").strip()
            if not raw:
                continue
            if raw.startswith("<") and raw.endswith(">"):
                raise ValueError(f"{name}: {raw} is not a value from this ticket")
            key = key_fn(raw)
            if key is None:
                raise ValueError(f"{name} is not a valid {name.replace('_', ' ')}")
            if enforce and key not in known:
                evidence.refused.append(name)
                raise ValueError(f"{name} is not on this ticket or in an earlier result; use the client's details "
                                 "from the ticket")
            out[name] = raw
        return out

    def _health(self, hours: Any) -> dict[str, Any]:
        try:
            hours = max(1, min(24, int(hours or 3)))
        except (TypeError, ValueError):
            hours = 3
        end = self.as_of or now_local()
        start = end - timedelta(hours=hours)

        def totals(lo: datetime, hi: datetime) -> dict[str, int]:
            out: dict[str, int] = {}
            for row in self.attempts.counts(lo, hi, by_hour=True):
                kind = str(row.get("kind") or "unknown")
                out[kind] = out.get(kind, 0) + int(row.get("n") or 0)
            return dict(sorted(out.items(), key=lambda kv: -kv[1]))

        now, before = totals(start, end), totals(start - timedelta(days=1), end - timedelta(days=1))
        failed = sum(v for k, v in now.items() if k not in ("linked", "unlinked"))
        total = sum(now.values())
        usual_failed = sum(v for k, v in before.items() if k not in ("linked", "unlinked"))
        return {"window": f"last {hours} hour(s)", "requests": total, "by_result": now,
                "same_hours_yesterday": before,
                "summary": (f"{failed} of {total} request(s) failed in the last {hours} h "
                            f"(same hours yesterday: {usual_failed} of {sum(before.values())})")}

    def _learn(self, data: Any, evidence: Evidence) -> None:
        """Identifiers in a result may be used in later lookups (e.g. the account an attempt used)."""
        evidence.known.update(find_identifiers(json.dumps(data, default=str)))

    def _find(self, values: dict[str, str], evidence: Evidence, ticket: Optional[Ticket]) -> dict[str, Any]:
        if not values:
            raise ValueError("give at least one of mobile, account_number or id_number")
        lookup = self.attempts.find(mobile=values.get("mobile"), account=values.get("account_number"),
                                    id_number=values.get("id_number"), days=self.lookback_days, as_of=self.as_of)
        evidence.lookups.append(lookup)
        for kind, name, fn in (("mobile", "mobile", mobile_key), ("account", "account_number", account_key),
                               ("id", "id_number", id_key)):
            if values.get(name):
                evidence.searched[kind].add(fn(values[name]))
        for attempt in lookup.attempts:
            evidence.attempts[attempt.id] = attempt
        text = ticket.owner_text() if ticket is not None else ""
        latest = lookup.latest
        action, reply = standard_reply(latest, ticket, evidence.link_records)
        latest_link = lookup.latest_link
        summary: dict[str, Any] = {
            "status": lookup.status,
            "status_meaning": _status_meaning(lookup),
            "suggested_action": action,
            "standard_reply": reply,
            "attempts_by_type": _counts(lookup),
        }
        if latest_link is not None and latest_link.outcome == "linked":
            summary["linked_on"] = format_timestamp(latest_link.when)
        if latest is not None and latest.error is not None:
            since = (latest.when or datetime.now()) - timedelta(hours=24)
            summary["ecocash_system_errors_last_24h"] = lookup.system_errors_since(since)
        return {"found": len(lookup.attempts), "attempts": [a.describe(text) for a in lookup.attempts[:15]],
                "summary": summary}

    def _link_record(self, values: dict[str, str], evidence: Evidence) -> dict[str, Any]:
        if not (values.get("mobile") or values.get("account_number")):
            raise ValueError("give mobile or account_number")
        assert self.link_records is not None
        rows = self.link_records.find(mobile=values.get("mobile"), account=values.get("account_number"))
        evidence.link_records += rows
        return {"found": len(rows), "links": rows,
                "note": "Rows are written by the registration service after EcoCash accepts a link. The status "
                        "codes are Postilion's own."}


def _counts(lookup: Lookup) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in lookup.attempts:
        counts[a.type_key] = counts.get(a.type_key, 0) + 1
    return counts


def _status_meaning(lookup: Lookup) -> str:
    latest = lookup.latest
    if latest is None:
        return "No link or delink request found for these details in the last 60 days."
    if latest.outcome == "linked":
        return "The latest request linked the account successfully."
    if latest.outcome == "unlinked":
        return "The latest request delinked the account; the client can now link again."
    error = latest.error or BY_KEY["unknown"]
    return f"The latest request failed: {error.label}. {error.meaning}"
