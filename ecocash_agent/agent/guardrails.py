"""Code-enforced rules between the model's decision and any write to osTicket.

The model proposes; this module decides what is allowed, using the evidence the lookups
recorded (not what the model says they found). The core rule: a ticket only closes on a link
attempt that belongs to the client on the ticket and whose outcome the branch or client can act on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

from datetime import timedelta

from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .ecocash import CLOSABLE
from .normalize import account_key, id_key, mobile_key, parse_timestamp
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


def topic_is_ecocash(topic: str, topics: list[str]) -> bool:
    topic = (topic or "").strip().upper()
    return any(topic == x.strip().upper() or topic.startswith(x.strip().upper() + " / ") for x in topics if x.strip())


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
    incident_open: bool = False,
    stale_days: int = 14,
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
        if ticket.topic_match and topics and not topic_is_ecocash(fresh.help_topic, topics):
            return to(NO_ACTION, f"help topic is now {fresh.help_topic!r}", deferred=True)
        if fresh.last_entry_id != ticket.last_entry_id:
            return to(NO_ACTION, "new thread activity during analysis; will retry next run", deferred=True)

    if decision.action not in REPLY_ACTIONS:
        return GuardrailResult(decision, decision, notes)

    # 2. The model tried to look up someone else's details.
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
        # 5. A close stands on a lookup that worked and an attempt that belongs to this client.
        if evidence.check_errors:
            return to(NO_ACTION, "close blocked: a lookup failed during analysis; will retry", deferred=True)
        if "find_ecocash_attempts" not in evidence.tools_used:
            return to(ESCALATE, "close blocked: the client's link attempts were not looked up")
        latest = evidence.latest
        if latest is None:
            return to(ESCALATE, "close blocked: no link attempt found for the client's details")
        owner = ticket.owner_identifiers()
        belongs = ((mobile_key(latest.mobile) in owner.mobiles) or (account_key(latest.account) in owner.accounts)
                   or (id_key(latest.id_number) in owner.ids))
        if not belongs:
            return to(ESCALATE, "close blocked: the latest attempt does not match a mobile, account or ID the "
                                "ticket owner gave")
        status = latest.type_key
        if status == "already_linked" or status == "unlinked":
            # The branch must act (delink, or have the client link again) and report back.
            asked = ticket.agent_replies(agent_staff_id)
            if not allow_request_info or asked >= max_info_requests:
                return to(ESCALATE, f"close blocked: latest attempt is {status} and the agent cannot ask again")
            notes.append(f"close changed to a reply that keeps the ticket open: latest attempt is {status}")
            return GuardrailResult(decision, replace(decision, action=REQUEST_INFO), notes)
        if status not in CLOSABLE:
            return to(ESCALATE, f"close blocked: the latest attempt is {status}, which needs a person")
        opened = parse_timestamp(ticket.created)
        if status != "linked" and opened and latest.when and latest.when < opened - timedelta(days=stale_days):
            return to(ESCALATE, f"close blocked: the latest attempt is more than {stale_days} days older than the ticket")
        if status == "kyc_mismatch" and not owner.ids:
            return to(ESCALATE, "close blocked: KYC failure but no national ID on the ticket to compare")
        if status == "ecocash_system_error":
            recent = [a for a in evidence.attempts.values() if a.type_key == "ecocash_system_error"
                      and a.when and latest.when and a.when >= latest.when - timedelta(hours=24)]
            if len(recent) >= 3:
                return to(ESCALATE, f"close blocked: {len(recent)} EcoCash system errors for the client in 24 h")
            if incident_open:
                return to(ESCALATE, "close blocked: an EcoCash incident is open, so 'try again later' may not help")
        # 6. The same failure again after the agent's advice: the advice did not work.
        advice = ticket.last_agent_reply(agent_staff_id)
        advised_at = parse_timestamp(advice.created) if advice else None
        if advised_at and status != "linked":
            repeat = [a for a in evidence.attempts.values() if a.type_key == status and a.when and a.when > advised_at]
            if repeat:
                return to(ESCALATE, f"close blocked: {status} happened again after the agent's advice")
        # 7. Rate limit.
        if closes_so_far >= max_closes:
            return to(NO_ACTION, f"close limit of {max_closes} per run reached", deferred=True)

    return GuardrailResult(decision, decision, notes)
