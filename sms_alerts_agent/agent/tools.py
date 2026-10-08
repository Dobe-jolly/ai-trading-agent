"""The tools the model can call, and the evidence they leave behind for the guardrails.

Arguments arrive masked (<MOBILE_1>, <NUMBER_2>); they are unmasked here, validated, run
against Metabase, and the results are masked again before the model sees them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from agentcore.metabase import MetabaseError
from .normalize import (
    ALERT_DEBITS,
    ALERT_SALARIES,
    normalize_account_number,
    parse_date,
    uses_both_accounts,
)
from .lookups import Lookups
from agentcore.pii import TOKEN_RE, Pseudonymizer
from .sms_checks import ESCALATING_STATUSES, SmsChecks
from agentcore.sql import UnsafeValue

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 7000
# Internal identifiers the model does not need (they stay in the evidence for staff notes).
REDACT_KEYS = {"internal_account_number"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items() if k not in REDACT_KEYS}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj

CHECK_ACCOUNT_TOOL: dict[str, Any] = {
    "name": "check_sms_alert_account",
    "description": (
        "Runs the production SMS-alert check for ONE account (the same logic the Java SMS agent uses) and "
        "returns a status code, the standard reply wording for that status ('reply'), and the evidence. "
        "Debits (transactional) path: account exists (IACLINK) -> account-level SMS flag (ACNTS_SMS_OPERN) -> "
        "IDCCOB registration (COBICADDR_SMS_ALERT) -> alert triggers in the last 15 days (TRAN/EVTSLOG) and the "
        "number they were addressed to -> if the ticket number matches, the SMS gateway delivery record. "
        "Salaries path: customer_accounts_idc cellphone vs the ticket number. "
        "'auto_close_allowed' false means this result can never close the ticket."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "account_number": {
                "type": "string",
                "description": "The account to check, exactly as it appears in the ticket (may be a masked value like <NUMBER_1>).",
            },
            "alert_types": {
                "type": "string",
                "enum": ["debits", "salaries", "both"],
                "description": "Which alerts the client is missing. Use the ticket's requested alert types unless the message clearly says otherwise.",
            },
            "ticket_phone": {
                "type": "string",
                "description": "The mobile number the client expects alerts on (may be masked like <MOBILE_1>). Omit to use the ticket's phone field.",
            },
        },
        "required": ["account_number", "alert_types"],
    },
}

TRACE_PIPELINE_TOOL: dict[str, Any] = {
    "name": "trace_alert_pipeline",
    "description": (
        "Diagnostic for escalations only. Traces SMS alerts to one mobile number through every hop "
        "(CAE request -> CAE render -> dispatch service -> SMS gateway) over 3 days and names the hop where they "
        "stop. These queries are heavy: call it at most once per ticket, and only when the account check shows "
        "a delivery problem (socket_* status, sms_no_customer_email) or the client insists alerts stopped. "
        "It never justifies closing a ticket."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mobile": {"type": "string", "description": "Mobile number (may be masked like <MOBILE_1>)."},
            "transaction_date": {
                "type": "string",
                "description": "Optional date of a transaction that did not alert, YYYY-MM-DD. Checks that day +/- 1.",
            },
        },
        "required": ["mobile"],
    },
}


@dataclass
class Evidence:
    """What the checks found for one ticket. The guardrails read this, not the model's claims."""

    account_results: dict[str, dict[str, Any]] = field(default_factory=dict)  # by normalised account
    check_errors: dict[str, str] = field(default_factory=dict)  # account -> error, cleared on a later success
    phones_used: dict[str, Optional[str]] = field(default_factory=dict)
    pipeline_traces: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    lookups: list[dict[str, Any]] = field(default_factory=list)  # investigative lookups: name, masked args, summary
    refused: list[str] = field(default_factory=list)  # lookups asked for a number not on the ticket

    @property
    def statuses(self) -> dict[str, str]:
        return {acct: str(r.get("status")) for acct, r in self.account_results.items()}

    @property
    def escalating(self) -> dict[str, str]:
        return {acct: s for acct, s in self.statuses.items() if s in ESCALATING_STATUSES}

    def template_reply(self, required_accounts: list[str], currency: Optional[str]) -> Optional[str]:
        """The reply the Java agent would post (computeTicketVerdict), when every required account was checked."""
        accounts = [a for a in required_accounts if a in self.account_results]
        if not required_accounts or len(accounts) != len(required_accounts):
            return None
        if uses_both_accounts(currency):
            parts = []
            for index, acct in enumerate(accounts):
                result = self.account_results[acct]
                label = (result.get("account_currency") or "").upper() or ("Account number" if index == 0 else "Account number 2")
                parts.append(f"{label} account {acct}: {result.get('reply')}")
            return " ".join(parts)
        return str(self.account_results[accounts[0]].get("reply"))


SCOPED_ARGS = ("account_number", "mobile")


class Toolbox:
    def __init__(self, checks: SmsChecks, pipeline_trace_enabled: bool = True, as_of: Optional[date] = None,
                 lookups: Optional[Lookups] = None) -> None:
        self.checks = checks
        self.pipeline_trace_enabled = pipeline_trace_enabled
        self.as_of = as_of
        self.lookups = lookups

    def schemas(self) -> list[dict[str, Any]]:
        tools = [CHECK_ACCOUNT_TOOL]
        if self.pipeline_trace_enabled:
            tools.append(TRACE_PIPELINE_TOOL)
        if self.lookups is not None:
            tools += self.lookups.schemas()
        return tools

    def run(self, name: str, args: dict[str, Any], pseudo: Pseudonymizer, evidence: Evidence,
            default_phone: Optional[str]) -> tuple[str, bool]:
        """Returns (masked result text, is_error)."""
        try:
            if name == "check_sms_alert_account":
                data = self._check_account(pseudo.unmask_obj(args), evidence, default_phone)
            elif name == "trace_alert_pipeline" and self.pipeline_trace_enabled:
                if evidence.pipeline_traces:
                    return "The pipeline trace has already been run for this ticket; use that result.", True
                data = self._trace(pseudo.unmask_obj(args))
                evidence.pipeline_traces.append(data)
            elif self.lookups is not None and name in self.lookups.names:
                foreign = [str(args.get(k)) for k in SCOPED_ARGS if k in args and not _known_token(args.get(k), pseudo)]
                if foreign:
                    evidence.refused += foreign
                    return ("Refused: lookups only accept a masked number exactly as it appears in the ticket or in an "
                            "earlier result (for example <NUMBER_1>).", True)
                data = self.lookups.run(name, pseudo.unmask_obj(args))
                evidence.lookups.append({"tool": name, "args": dict(args), "summary": _lookup_summary(data)})
            else:
                return f"Unknown tool {name!r}.", True
        except (ValueError, UnsafeValue) as exc:
            return f"Invalid input: {pseudo.mask(str(exc))}", True
        except MetabaseError as exc:
            return pseudo.mask(f"Check failed (data source error): {exc}. You may retry once.")[:800], True
        evidence.tools_used.append(name)
        text = json.dumps(pseudo.mask_obj(_redact(data)), default=str, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...[truncated]"
        return text, False

    def _check_account(self, args: dict[str, Any], evidence: Evidence, default_phone: Optional[str]) -> dict[str, Any]:
        account = normalize_account_number(args.get("account_number"))
        if account is None or len(account) > 20:
            raise ValueError("account_number must contain the account digits")
        kinds = {"debits": [ALERT_DEBITS], "salaries": [ALERT_SALARIES], "both": [ALERT_SALARIES, ALERT_DEBITS]}
        alert_types = kinds.get(str(args.get("alert_types", "")).strip().lower())
        if alert_types is None:
            raise ValueError("alert_types must be debits, salaries or both")
        phone = args.get("ticket_phone")
        phone = str(phone).strip() if phone not in (None, "") else default_phone
        try:
            result = self.checks.check_account(account, alert_types, phone, as_of=self.as_of)
        except MetabaseError as exc:
            if account not in evidence.account_results:
                evidence.check_errors[account] = str(exc)
            raise
        evidence.check_errors.pop(account, None)
        evidence.account_results[account] = result
        evidence.phones_used[account] = phone
        return result

    def _trace(self, args: dict[str, Any]) -> dict[str, Any]:
        mobile = str(args.get("mobile") or "").strip()
        if not mobile:
            raise ValueError("mobile is required")
        txn_date = parse_date(args.get("transaction_date")) if args.get("transaction_date") else None
        return self.checks.trace_pipeline(mobile, txn_date)


def _known_token(value: Any, pseudo: Pseudonymizer) -> bool:
    text = str(value or "").strip()
    return bool(TOKEN_RE.fullmatch(text)) and not pseudo.unknown_tokens(text)


def _lookup_summary(data: dict[str, Any]) -> str:
    if data.get("found") is False:
        return "account not found"
    for key in ("summary",):
        if data.get(key):
            return str(data[key])
    if "messages" in data:
        return f"{len(data['messages'])} message(s): " + ", ".join(f"{k} {v}" for k, v in sorted(data["by_status"].items()))
    if "pending_changes" in data:
        return f"{len(data['pending_changes'])} pending change(s)"
    if "alert_registrations" in data:
        return f"{len(data['alert_registrations'])} alert registration(s); customer record mobile on file"
    return "done"
