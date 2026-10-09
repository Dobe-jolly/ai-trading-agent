"""The router's pass over tickets under catch-all topics (and tickets with none), and the closed-ticket replay. The
pass itself is agentcore.runner.DeskRunner; this module says what is particular to routing: the move, and how the
notes read."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from agentcore.runner import EXPIRED_CATEGORY, REQUEST_EXPIRED, DeskRunner, RunSummary  # noqa: F401 - re-exported
from .approval import approval_reason, standard_action
from .decision import ESCALATE, MOVE, NO_ACTION, REQUEST_INFO
from .guardrails import GuardrailResult, apply_guardrails
from .loop import AgentResult
from .osticket import ESCALATION_NOTE_MARKER, MOVE_NOTE_MARKER, RECOMMENDATION_NOTE_MARKER, Ticket
from .rules import ASK, rules_decide
from .topics import Catalogue

log = logging.getLogger(__name__)

# Where a decision sends the ticket, for the console's overview (the decision's category is set to one of these).
ROUTE_GROUPS = ("desk_topic", "staff_topic", "stays", "unclear", "no_description")


class Router(DeskRunner):
    agent_name = "routing agent"
    incident_label = "Open incident at the time"
    escalation_marker = ESCALATION_NOTE_MARKER
    recommendation_marker = RECOMMENDATION_NOTE_MARKER
    result_type = AgentResult
    audit_scope = "closed catch-all"
    approval_reason = staticmethod(approval_reason)

    def __init__(self, *args: Any, catalogue: Optional[Catalogue] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.catalogue = catalogue or Catalogue([])
        self.moves = 0

    # ---- which tickets ---------------------------------------------------------------------
    def discover(self, summary: RunSummary) -> list[Ticket]:
        return self.store.open_tickets()

    def on_request(self, number: str) -> list[Ticket]:
        tickets = [t for t in self.store.open_tickets() if t.number == number]
        if not tickets:
            log.error("Ticket #%s is not an open ticket under a catch-all topic (or without one)", number)
        return tickets

    def _skip_reason(self, ticket: Ticket, force: bool) -> Optional[str]:
        ticket.moved_earlier = self.moved_in_log(ticket)
        reason = super()._skip_reason(ticket, force)
        if reason or force:
            return reason
        if ticket.moved_before():
            return "moved before; a person filed it back here"
        if ticket.handled_by_staff(self.s.reply_staff_id):
            return "a person is handling it"
        return None

    def moved_in_log(self, ticket: Ticket) -> bool:
        """The decision log has a move carried out on this ticket (also when its note could not be written)."""
        row = self.audit.db.execute("SELECT 1 FROM decisions WHERE ticket_id = ? AND executed LIKE '%\"moved\"%' LIMIT 1",
                                    (ticket.ticket_id,)).fetchone()
        return row is not None

    # ---- deciding ----------------------------------------------------------------------------
    def fallback(self, ticket: Ticket, why: str) -> Optional[AgentResult]:
        result = rules_decide(ticket, why=why, agent_staff_id=self.s.reply_staff_id)
        return None if result is None or result.decision.action == NO_ACTION else result

    def guard(self, ticket: Ticket, result: AgentResult, fresh: Any, live: bool) -> GuardrailResult:
        d = result.decision
        target = self.catalogue.find(getattr(d, "target_topic", "")) if d.action == MOVE else None
        if target is not None and not self.catalogue.may_move_to(target):
            # By design, not a safety stop: only topics an agent works are moved to without a person.
            d.action = ESCALATE
        d.category = self.route_group(ticket, result)
        guard = apply_guardrails(
            result.decision, ticket, fresh, result.evidence, result.pseudo, self.catalogue,
            confidence_threshold=self.s.confidence_threshold,
            moves_so_far=self.moves,
            max_moves=self.s.max_moves_per_run,
            allow_request_info=self.s.allow_request_info,
            check_live_state=live,
            max_info_requests=self.s.max_info_requests,
            agent_staff_id=self.s.reply_staff_id,
        )
        if guard.final.action == MOVE and live:
            self.moves += 1  # a pass (not a replay), in every mode: shadow mirrors the live limit
        return guard

    def route_group(self, ticket: Ticket, result: AgentResult) -> str:
        d = result.decision
        if d.category in ("rules_fallback", "approved_reply", EXPIRED_CATEGORY, "agent_fallback"):
            return d.category
        if d.action == REQUEST_INFO:
            return "no_description"
        if d.action == NO_ACTION:
            return "stays"
        topic = self.catalogue.find(getattr(d, "target_topic", ""))
        if topic is None or (d.action == ESCALATE and d.confidence < self.s.confidence_threshold):
            return "unclear"
        return "desk_topic" if topic.desk else "staff_topic"

    def standard_action(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return standard_action(ticket)

    def template(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return ASK if result.decision.action == REQUEST_INFO else None

    def template_for(self, ticket: Ticket, result: AgentResult, action: str) -> Optional[str]:
        return ASK if action == REQUEST_INFO else None

    # ---- carrying out a move -------------------------------------------------------------------
    def execute_other(self, ticket: Ticket, result: AgentResult, guard: GuardrailResult) -> list[str]:
        if guard.final.action != MOVE:
            raise ValueError(f"the router has no action {guard.final.action!r}")
        target = self.catalogue.find(guard.final.target_topic)
        if target is None:  # checked by the guardrails; the catalogue cannot change within a pass
            raise ValueError(f"{guard.final.target_topic!r} is not an active help topic")
        if not self.writer.set_topic(ticket, ticket.topic_id, target.topic_id):
            log.info("#%s: not moved: filed elsewhere or assigned to a person meanwhile", ticket.number)
            return ["not moved: the ticket changed"]
        done = ["moved"]
        if self.writer.note(ticket, self.move_note(ticket, result, guard)):
            done.append("note")
        return done

    # ---- notes -----------------------------------------------------------------------------------
    def _where(self, name: str) -> str:
        topic = self.catalogue.find(name)
        if topic is None:
            return name
        return topic.name + (f" (worked by the {topic.desk} agent)" if topic.desk else "")

    def move_note(self, ticket: Ticket, result: AgentResult, guard: GuardrailResult) -> str:
        d, f = guard.proposed, guard.final
        lines = [MOVE_NOTE_MARKER,
                 f"From: {ticket.help_topic.strip() or '(no help topic)'}",
                 f"To: {self._where(f.target_topic)}",
                 f"Confidence: {d.confidence:.0%}", "", "Why:", d.reason, ""]
        if d.evidence:
            lines += [f"- {item}" for item in d.evidence] + [""]
        lines.append("If this is wrong, file it back or under the right topic; the routing agent will not move it again.")
        return result.pseudo.unmask("\n".join(lines).strip())

    def finding_lines(self, result: AgentResult) -> list[str]:
        d, ev = result.decision, result.evidence
        target, alternatives = getattr(d, "target_topic", ""), getattr(d, "alternatives", [])
        lines: list[str] = []
        if target:
            lines.append(f"Suggested topic: {self._where(target)} (confidence {d.confidence:.0%})")
        if alternatives:
            lines.append("Also possible: " + "; ".join(self._where(a) for a in alternatives))
        if lines:
            lines.append("")
        if ev.similar_topics:
            lines.append("Similar closed tickets were filed under: "
                         + ", ".join(f"{t} ({n})" for t, n in ev.similar_topics.most_common(4)))
        for s in ev.searches:
            args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
            lines.append(f"Looked at {s['tool']} ({args}): {s['found']} found")
        if ev.check_errors:
            lines.append("Could not read: " + "; ".join(ev.check_errors[:3]))
        if ev.similar_topics or ev.searches:
            lines.append("")
        return lines

    def next_step(self, ticket: Ticket, result: AgentResult) -> str:
        target = getattr(result.decision, "target_topic", "")
        topic = self.catalogue.find(target) if target else None
        if topic is None:
            return "Next step: read the ticket and file it under the topic that fits."
        why = "" if self.catalogue.may_move_to(topic) else \
            " (the routing agent moves tickets on its own only to topics an agent works)"
        return f"Next step: if you agree, change the help topic to {topic.name}{why}."

    def recommendation_note(self, result: AgentResult, guard: GuardrailResult, posted_text: Optional[str],
                            ticket: Optional[Any] = None) -> str:
        p, f = guard.proposed, guard.final
        if f.action == MOVE:
            head = f"Recommended: file this ticket under {self._where(f.target_topic)} (confidence {p.confidence:.0%})."
        elif f.action == ESCALATE:
            head = "Recommended: a person decides where this ticket belongs."
        elif f.action == REQUEST_INFO:
            head = "Recommended: ask the branch to describe the problem."
        else:
            head = "Recommended: leave the ticket where it is."
        lines = [self.recommendation_marker, head]
        if guard.changed:
            lines.append(f"Model proposed '{p.action}'; safety rules changed it: {'; '.join(guard.notes)}")
        if posted_text:
            lines += ["", "Reply it would post:", posted_text]
        lines += ["", "Reasoning:", p.reason, ""]
        if p.evidence:
            lines += [f"- {item}" for item in p.evidence] + [""]
        lines += self.finding_lines(result)
        return result.pseudo.unmask("\n".join(lines).strip())

    def record_fields(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        d = result.decision
        target = getattr(d, "target_topic", "")
        topic = self.catalogue.find(target) if target else None
        statuses = {"topic": topic.name} if topic else {}
        facts = {"route": {"from": ticket.help_topic.strip() or None, "to": topic.name if topic else target or None,
                           "desk": topic.desk if topic else None, "alternatives": getattr(d, "alternatives", []),
                           "similar": dict(result.evidence.similar_topics.most_common(5)), "branch": ticket.org_name}}
        template = self.template(ticket, result)
        return {"facts": facts, "account_statuses": statuses,
                "template_reply": result.pseudo.mask(template) if template else None}

    # ---- closed-ticket replay ------------------------------------------------------------------------
    def closed_tickets(self, since: date) -> list[Ticket]:
        return self.store.closed_tickets(since)

    def audit_columns(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        d = result.decision
        return {"status": getattr(d, "target_topic", "") or d.action}
