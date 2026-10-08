"""Code-enforced rules between the model's decision and any write to osTicket.

The model proposes; this module decides what is allowed, using the evidence the checks
recorded (not what the model says they found). The core rule is the Java agent's: a ticket
may only auto-close when every account it covers was checked and none has an escalating
status (socket_pending/failed/not_found/unknown, salaries_unverified).

Tickets filed under another help topic (picked up by triage) get one more rule: the agent may reply
to or close them only when triage found the ticket to be about SMS alerts alone. Otherwise its
findings go into an internal note for the team that owns the ticket.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .osticket import Ticket, TicketState
from .pii import TOKEN_RE, Pseudonymizer
from .tools import Evidence

MIN_REPLY_CHARS = 20
MAX_REPLY_CHARS = 2000
SECRET_WORDS = re.compile(r"\b(PIN|OTP|CVV|CVC|password|passcode|one[- ]time (?:pin|password))\b", re.I)
URL_RE = re.compile(r"https?://|www\.", re.I)
DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
DIGIT_RUN = re.compile(r"\d(?:[\s-]?\d)+")
TEMPLATE_LEFTOVERS = re.compile(r"\{\{|\}\}|\[(?:insert|name|customer|todo|account|number)[^\]]*\]", re.I)
LOOSE_TOKEN = re.compile(r"<[A-Z_]+_\d+>")


def has_raw_long_number(text: str) -> bool:
    """8+ digits (spaces/dashes allowed) that did not come from a masked token, ignoring dates.
    The model never sees real account or phone numbers, so a raw one in its reply is invented."""
    text = DATES.sub(" ", TOKEN_RE.sub(" ", text))
    return any(len(re.sub(r"\D", "", m.group())) >= 8 for m in DIGIT_RUN.finditer(text))


def reply_problems(text: str, pseudo: Optional[Pseudonymizer] = None) -> list[str]:
    problems: list[str] = []
    if len(text) < MIN_REPLY_CHARS:
        problems.append("reply is too short")
    if len(text) > MAX_REPLY_CHARS:
        problems.append("reply is too long")
    unknown = pseudo.unknown_tokens(text) if pseudo else TOKEN_RE.findall(text)
    stray = [t for t in LOOSE_TOKEN.findall(text) if not TOKEN_RE.fullmatch(t)]
    if unknown or stray:
        problems.append("reply contains a placeholder that does not exist in this ticket")
    if has_raw_long_number(text):
        problems.append("reply contains an account/phone number the agent was never shown")
    if SECRET_WORDS.search(text):
        problems.append("reply mentions a PIN, OTP, password or card code")
    if URL_RE.search(text):
        problems.append("reply contains a link")
    if TEMPLATE_LEFTOVERS.search(text):
        problems.append("reply contains template leftovers")
    return problems


@dataclass
class GuardrailResult:
    proposed: Decision
    final: Decision
    notes: list[str] = field(default_factory=list)
    deferred: bool = False  # retry next run (not a real decision)

    @property
    def changed(self) -> bool:
        return self.final.action != self.proposed.action


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
    no_close_statuses: frozenset = frozenset({"invalid_account_number", "invalid_ticket_phone"}),
    max_info_requests: int = 2,
    agent_staff_id: Optional[int] = None,
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
        if fresh.help_topic.strip() != ticket.help_topic.strip():
            return to(NO_ACTION, f"help topic is now {fresh.help_topic or '(none)'!r}", deferred=True)
        if fresh.last_entry_id != ticket.last_entry_id:
            return to(NO_ACTION, "new thread activity during analysis; will retry next run", deferred=True)

    if decision.action not in REPLY_ACTIONS:
        return GuardrailResult(decision, decision, notes)

    # The model tried to look up a number that is not on the ticket.
    if evidence.refused:
        return to(ESCALATE, "reply blocked: the agent tried to look up a number that is not on the ticket")

    # Filed under another topic: only a ticket that is purely about SMS alerts may get a reply.
    if not ticket.topic_match and ticket.triage_kind != "sms_alerts":
        what = ("also raises something other than SMS alerts" if ticket.triage_kind == "mixed"
                else "was not confirmed as an SMS alerts issue")
        return to(ESCALATE, f"reply blocked: the ticket is under {ticket.help_topic or 'no topic'!r} and {what}; "
                            "findings left as a note")

    if decision.action == REQUEST_INFO:
        if not allow_request_info:
            return to(ESCALATE, "request_info is disabled (ALLOW_REQUEST_INFO=false)")
        asked = ticket.agent_replies(agent_staff_id)
        if asked >= max_info_requests:
            return to(ESCALATE, f"the agent has already replied {asked} time(s) on this ticket; a person should take over")

    # 2. The text that will be posted.
    problems = reply_problems(decision.reply_text, pseudo)
    if problems:
        return to(ESCALATE, "reply blocked: " + "; ".join(problems))

    # 3. Confidence.
    if decision.confidence < confidence_threshold:
        return to(ESCALATE, f"confidence {decision.confidence:.2f} is below {confidence_threshold:.2f}")

    if decision.action == REPLY_AND_CLOSE:
        # 4. Accounts. Every account checked must come from the ticket itself (form or an owner message),
        #    and every form account must be checked unless the owner supplied a replacement in the thread.
        checked = list(evidence.account_results)
        foreign = [a for a in checked if not ticket.mentions_account(a)]
        if foreign:
            return to(ESCALATE, f"close blocked: account(s) {', '.join(pseudo.mask(a) for a in foreign)} "
                                "do not appear on the ticket form or in the owner's messages")
        form_accounts = ticket.required_accounts
        missing = [a for a in form_accounts if a not in checked]
        replacements = [a for a in checked if a not in form_accounts]
        if not checked or len(replacements) < len(missing):
            what = (f"account(s) {', '.join(pseudo.mask(a) for a in missing)} not checked"
                    if missing else "no account was checked")
            if evidence.check_errors:
                return to(NO_ACTION, f"close blocked: {what} because a data source failed; will retry", deferred=True)
            return to(ESCALATE, f"close blocked: {what}")
        if evidence.check_errors:
            return to(NO_ACTION, "close blocked: a check failed during analysis; will retry", deferred=True)
        # 5. The Java rule: no escalating status on any account checked. Plus statuses that mean the
        #    ticket's own data is wrong, which should be corrected with the owner, not closed.
        escalating = evidence.escalating
        if escalating:
            detail = ", ".join(f"{pseudo.mask(a)}={s}" for a, s in escalating.items())
            return to(ESCALATE, f"close blocked by check status ({detail})")
        if not replacements:
            blocking = {a: st for a, st in evidence.statuses.items() if st in no_close_statuses}
        else:
            # The form value was wrong but the owner supplied a replacement: judge the replacement.
            blocking = {a: evidence.statuses[a] for a in replacements if evidence.statuses[a] in no_close_statuses}
        if blocking:
            detail = ", ".join(f"{pseudo.mask(a)}={s}" for a, s in blocking.items())
            return to(ESCALATE, f"close blocked: ticket data needs correcting first ({detail}); use request_info")
        # 6. Rate limit.
        if closes_so_far >= max_closes:
            return to(NO_ACTION, f"close limit of {max_closes} per run reached", deferred=True)

    return GuardrailResult(decision, decision, notes)
