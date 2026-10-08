"""Code-enforced rules between the model's decision and any write to osTicket.

The model proposes; this module decides what is allowed, from the evidence the lookups recorded (not from what
the model says they found). The core rule: a ticket only closes on a confirmed transaction that belongs to the
client on the ticket, whose reading is final (succeeded, declined before any debit, or reversed / refunded), and a
reply that says what that reading says. Pending, flagged, failed-reversal and uncertain transactions always go to a
person: the client may be out of pocket.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Optional

from agentcore.guardrails import (  # noqa: F401 - shared reply rules, re-exported
    GuardrailResult,
    has_raw_long_number,
    reply_problems,
)
from agentcore.pii import Pseudonymizer
from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .normalize import account_key, reference_key
from .osticket import Ticket, TicketState, topic_matches
from .sources import DECLINED, REVERSED, SUCCEEDED
from .tools import CLOSABLE, FAILED_NO_DEBIT, FAILED_REFUNDED, Evidence

SAYS_REVERSED = re.compile(r"(?i)\b(revers\w*|refund\w*|credited back|returned to|money is back|back in (?:the|their|your) account)\b")
SAYS_SUCCEEDED = re.compile(r"(?i)\b(went through|was successful|successfully|completed successfully|was processed)\b")
SAYS_NO_DEBIT = re.compile(r"(?i)\b(no money (?:left|was taken|was deducted)|not debited|no debit|no funds (?:were|was) (?:deducted|taken))\b")


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
            return to(ESCALATE, "close blocked: no transaction was confirmed")
        owner = ticket.owner_details()
        mine = account_key(txn.account) in owner.accounts or account_key(txn.counterparty) in owner.accounts \
            or (txn.reference and reference_key(txn.reference) in owner.references)
        if not mine:
            return to(ESCALATE, "close blocked: the confirmed transaction is not on an account or reference the ticket "
                                "owner gave")
        status = evidence.status
        if status not in CLOSABLE:
            return to(ESCALATE, f"close blocked: the confirmed transaction is {status}, which needs a person")
        if evidence.amount_on_ticket == "no":
            return to(ESCALATE, "close blocked: the confirmed transaction's amount is not the amount on the ticket")
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
