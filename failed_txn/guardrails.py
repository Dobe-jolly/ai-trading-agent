"""Code-enforced rules between the model's decision and any write to osTicket.

The model proposes; this module decides what is allowed, from the evidence the lookups recorded (not from what
the model says they found). The core rule: a ticket only closes on a confirmed transaction that belongs to the
client on the ticket, whose reading is final (succeeded, declined before any debit, or reversed / refunded), and a
reply that says what that reading says. Pending, flagged, failed-reversal and uncertain transactions always go to a
person: the client may be out of pocket. A confirmed EcoCash link may also close an EcoCash ticket; and, with
QUERY_CLOSE=true, what query_data found for the ticket owner, when core banking's postings were checked and agree
with what the reply says about the money (query_close_problem).
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Optional

from agentcore.guardrails import (  # noqa: F401 - shared reply rules, re-exported
    GuardrailResult,
    has_raw_long_number,
    reply_problems,
)
from agentcore.pii import Pseudonymizer
from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .normalize import account_key, mobile_key, reference_key
from .osticket import Ticket, TicketState, topic_matches
from .sources import DECLINED, REVERSED, SUCCEEDED, debit_check
from .tools import CLOSABLE, FAILED_NO_DEBIT, FAILED_REFUNDED, LINK_CLOSABLE, Evidence

SAYS_REVERSED = re.compile(r"(?i)\b(revers\w*|refund\w*|credited back|returned to|money is back|back in (?:the|their|your) account)\b")
SAYS_SUCCEEDED = re.compile(r"(?i)\b(went through|was successful|successfully|completed successfully|was processed)\b")
ABOUT_ECOCASH = re.compile(r"(?i)eco\s*cash|\bb2w\b|\bw2b\b|bank\s*to\s*wallet|wallet\s*to\s*bank|\bwallet\b")
SAYS_NO_DEBIT = re.compile(r"(?i)\b(no money (?:left|was taken|was deducted)|not debited|no debit|no funds (?:were|was) (?:deducted|taken))\b")
CITED_QUERY = re.compile(r"\bQ(\d{1,3})\b")


def _days(window: Optional[dict]) -> Optional[tuple[date, date]]:
    """A query's window ({from, to}, to exclusive) as inclusive days; None when the source has no time column."""
    if not window or not window.get("from"):
        return None
    start = datetime.strptime(str(window["from"])[:16], "%Y-%m-%d %H:%M")
    end = datetime.strptime(str(window["to"])[:16], "%Y-%m-%d %H:%M") - timedelta(minutes=1) if window.get("to") else start
    return start.date(), end.date()


def other_amount_problem(txn, owner, evidence: Evidence) -> Optional[str]:
    """Why a declined attempt of another amount cannot answer the ticket, or None: core banking's postings for the
    client's account around that day (read when the attempt was confirmed) must show no debit of the ticket's amount
    that stayed out."""
    if evidence.debit is None:
        return "core banking's postings were not read, so a debit of the ticket's amount cannot be ruled out"
    accounts = set(owner.accounts) | set(evidence.client_accounts) | {account_key(txn.account)}
    postings = [t for t in evidence.txns.values() if t.source == "core" and account_key(t.account) in accounts]
    day = txn.when.date() if txn.when else None
    for amount in sorted(owner.amounts):
        money = debit_check(postings, amount, day)
        if money["debited"] == "yes" and money.get("refunded") != "yes":
            return f"core banking shows a debit of {amount:,.2f} ({money.get('debit_on')}) that did not come back"
    return None


def query_close_problem(decision: Decision, ticket: Ticket, evidence: Evidence) -> tuple[Optional[str], list[str]]:
    """(why a close on query_data findings is not allowed, or None; the cited queries it rests on).

    Allowed (QUERY_CLOSE=true) when the decision cites a query (Q1, Q2, ...) that was matched on an account, mobile,
    reference or CIF the ticket owner gave and found rows; core banking's postings were checked
    (check_account_postings) for the ticket owner's account over the days of that finding; and what the reply says
    about the money agrees with those postings: "no money left" only if core banking shows no debit of the ticket's
    amount that stayed out, "reversed / refunded" only if it shows the debit and the money coming back."""
    cited = {f"Q{n}" for item in decision.evidence for n in CITED_QUERY.findall(str(item))}
    found = sorted((q for q, rec in evidence.queries.items() if q in cited and rec.get("mode") == "rows"
                    and rec.get("match_columns") and rec.get("owner_given") and rec.get("rows")),
                   key=lambda q: int(q[1:]))
    if not found:
        return ("no cited query (Q1, Q2, ...) found rows for an account, mobile or reference the ticket owner gave",
                found)
    owner = ticket.owner_details()
    mine = set(owner.accounts) | set(evidence.client_accounts)
    windows = [_days(evidence.queries[q].get("window")) for q in found]
    checks = [c for c in evidence.postings_checked if c.get("account") in mine and c.get("exists")
              and any(w is None or (c["start"] <= w[1] and w[0] <= c["end"]) for w in windows)]
    if not checks:
        return ("core banking postings were not checked (check_account_postings) for the ticket owner's account over "
                "the days of the finding", found)

    def postings(of: list[dict]) -> list:
        accounts = {c["account"] for c in of}
        return [t for t in evidence.txns.values() if t.source == "core" and account_key(t.account) in accounts
                and (t.when is None or any(c["start"] <= t.when.date() <= c["end"] for c in of))]

    said = decision.reply_text or ""
    no_debit, came_back = bool(SAYS_NO_DEBIT.search(said)), bool(SAYS_REVERSED.search(said))
    if not (no_debit or came_back):
        return None, found
    if not owner.amounts:
        if came_back or any(p.debit for p in postings(checks)):
            return ("the reply says what happened to the money, but the ticket gives no amount to check it against "
                    "core banking's postings", found)
        return None, found
    for amount in sorted(owner.amounts):
        usable = [c for c in checks if c.get("amount") in (None, amount)]
        if not usable:
            return (f"core banking postings were checked for another amount, not {amount:,.2f}", found)
        money = debit_check(postings(usable), amount, None)
        if no_debit and money["debited"] == "yes" and money.get("refunded") != "yes":
            return (f"the reply says no money left the account, but core banking shows a debit of {amount:,.2f} "
                    f"({money.get('debit_on')}) that did not come back", found)
        if came_back and not (money["debited"] == "yes" and money.get("refunded") == "yes"):
            return (f"the reply says the money came back, but core banking does not show a debit of {amount:,.2f} "
                    "and its reversal", found)
    return None, found


def apply_guardrails(
    decision: Decision,
    ticket: Ticket,
    fresh: Optional[TicketState],
    evidence: Evidence,
    pseudo: Pseudonymizer,
    *,
    confidence_threshold: float,
    closes_so_far: int,
    max_closes: int,
    allow_request_info: bool = True,
    check_live_state: bool = True,
    max_info_requests: int = 2,
    agent_staff_id: Optional[int] = None,
    topics: Optional[list[str]] = None,
    allow_query_close: bool = False,
) -> GuardrailResult:
    notes: list[str] = []

    def to(action: str, note: str, deferred: bool = False) -> GuardrailResult:
        notes.append(note)
        final = replace(decision, action=action, reply_text=decision.reply_text if action in REPLY_ACTIONS else "")
        return GuardrailResult(decision, final, notes, deferred=deferred)

    # 1. The ticket may have changed while the model was working.
    if check_live_state:
        if fresh is None:
            return to(NO_ACTION, "ticket could not be re-read", deferred=True)
        if not fresh.is_open:
            return to(NO_ACTION, f"ticket is no longer open ({fresh.status_name})", deferred=True)
        if topics and not topic_matches(fresh.help_topic, topics):
            return to(NO_ACTION, f"help topic is now {fresh.help_topic!r}", deferred=True)
        if fresh.last_entry_id != ticket.last_entry_id:
            return to(NO_ACTION, "new thread activity during analysis; will retry next run", deferred=True)

    if decision.action not in REPLY_ACTIONS:
        return GuardrailResult(decision, decision, notes)

    # 2. The model tried to look at someone else's account.
    if evidence.refused:
        return to(ESCALATE, "reply blocked: the agent tried to look up "
                            f"{', '.join(sorted(set(evidence.refused)))} not given on the ticket")

    if decision.action == REQUEST_INFO:
        if not allow_request_info:
            return to(ESCALATE, "request_info is disabled (ALLOW_REQUEST_INFO=false)")
        asked = ticket.agent_replies(agent_staff_id)
        if asked >= max_info_requests:
            return to(ESCALATE, f"the agent has already replied {asked} time(s) on this ticket; a person should take over")

    # 3. The text that will be posted.
    problems = reply_problems(decision.reply_text, pseudo)
    if problems:
        return to(ESCALATE, "reply blocked: " + "; ".join(problems))

    # 4. Confidence.
    if decision.confidence < confidence_threshold:
        return to(ESCALATE, f"confidence {decision.confidence:.2f} is below {confidence_threshold:.2f}")

    if decision.action == REPLY_AND_CLOSE:
        # 5. A close stands on lookups that worked and a confirmed transaction of this client.
        if evidence.check_errors:
            return to(NO_ACTION, "close blocked: a lookup failed during analysis; will retry", deferred=True)
        txn = evidence.subject
        if txn is None:
            link = evidence.link or {}
            if link.get("status") not in LINK_CLOSABLE:
                # 5b. QUERY_CLOSE: what query_data found for the ticket owner, with core banking's postings checked.
                if allow_query_close and evidence.queries:
                    problem, found = query_close_problem(decision, ticket, evidence)
                    if problem is None:
                        if closes_so_far >= max_closes:
                            return to(NO_ACTION, f"close limit of {max_closes} per run reached", deferred=True)
                        evidence.query_close = found
                        notes.append("closed on what query_data found (" + ", ".join(
                            f"{q} {evidence.queries[q]['source']}" for q in found) + "), core banking postings checked")
                        return GuardrailResult(decision, decision, notes)
                    return to(ESCALATE, "close blocked: no transaction or EcoCash link was confirmed, and the query "
                                        f"finding cannot close it: {problem}")
                return to(ESCALATE, "close blocked: no transaction or EcoCash link was confirmed"
                          + (f" (the link is {link['status']})" if link.get("status") else ""))
            # A close on the EcoCash link: on an EcoCash ticket, for an account or mobile the ticket owner gave, saying
            # what to do about it. ("No universal account" on a ZIPIT is the destination account failing validation,
            # not the EcoCash link: #971899.)
            about = " ".join([ticket.help_topic or "", ticket.subject or "", ticket.owner_text()])
            if not ABOUT_ECOCASH.search(about):
                return to(ESCALATE, "close blocked: the EcoCash link only answers EcoCash bank-to-wallet / wallet-to-bank "
                                    "tickets, and this ticket is not about EcoCash")
            owner = ticket.owner_details()
            mine = (link.get("account") and (account_key(link["account"]) in owner.accounts
                                             or account_key(link["account"]) in evidence.client_accounts)) \
                or (link.get("mobile") and mobile_key(link["mobile"]) in owner.mobiles)
            if not mine:
                return to(ESCALATE, "close blocked: the EcoCash link checked is not for an account or mobile the "
                                    "ticket owner gave")
            if closes_so_far >= max_closes:
                return to(NO_ACTION, f"close limit of {max_closes} per run reached", deferred=True)
            return GuardrailResult(decision, decision, notes)
        owner = ticket.owner_details()
        mine = account_key(txn.account) in owner.accounts or account_key(txn.counterparty) in owner.accounts \
            or (txn.reference and reference_key(txn.reference) in owner.references) \
            or account_key(txn.account) in evidence.client_accounts  # an account of the CIF the owner gave
        if not mine:
            return to(ESCALATE, "close blocked: the confirmed transaction is not on an account or reference the ticket "
                                "owner gave")
        status = evidence.status
        if status not in CLOSABLE:
            return to(ESCALATE, f"close blocked: the confirmed transaction is {status}, which needs a person")
        if evidence.amount_on_ticket == "no":
            # A declined attempt of another amount may be replied on when no money of the ticket's amount left the
            # account either: core banking's postings around that day show no debit of it that stayed out.
            if status != DECLINED:
                return to(ESCALATE, "close blocked: the confirmed transaction's amount is not the amount on the ticket")
            problem = other_amount_problem(txn, owner, evidence)
            if problem:
                return to(ESCALATE, f"close blocked: the confirmed transaction's amount is not the amount on the "
                                    f"ticket, and {problem}")
        if evidence.same_amount_rivals() and not (txn.reference and reference_key(txn.reference) in owner.references):
            return to(ESCALATE, "close blocked: another transaction of the same amount that day reads differently")
        debit = evidence.debit or {}
        if status == DECLINED and debit.get("debited") == "yes" and debit.get("refunded") != "yes":
            return to(ESCALATE, "close blocked: declined, but core banking shows the client was debited")
        if status == SUCCEEDED and txn.raw.get("client_is_beneficiary"):
            return to(ESCALATE, "close blocked: the client is the beneficiary of the confirmed transfer, not the sender")
        # 6. The reply must say what the evidence says.
        text = decision.reply_text or ""
        refunded = status in (REVERSED, FAILED_REFUNDED)
        if SAYS_REVERSED.search(text) and not refunded:
            return to(ESCALATE, f"reply blocked: it says the money came back, but the transaction is {status}")
        if SAYS_SUCCEEDED.search(text) and status != SUCCEEDED and not SAYS_REVERSED.search(text) \
                and not SAYS_NO_DEBIT.search(text):
            return to(ESCALATE, f"reply blocked: it says the transaction went through, but it is {status}")
        if SAYS_NO_DEBIT.search(text) and status not in (DECLINED, FAILED_NO_DEBIT):
            return to(ESCALATE, f"reply blocked: it says no money left the account, but the transaction is {status}")
        # 7. Rate limit.
        if closes_so_far >= max_closes:
            return to(NO_ACTION, f"close limit of {max_closes} per run reached", deferred=True)

    return GuardrailResult(decision, decision, notes)
