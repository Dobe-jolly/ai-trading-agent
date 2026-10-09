"""The NMB Connect desk's pass over its tickets, and the closed-ticket replay. The pass itself is
agentcore.runner.DeskRunner; this module says what is particular to NMB Connect."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from agentcore.runner import EXPIRED_CATEGORY, REQUEST_EXPIRED, DeskRunner, RunSummary  # noqa: F401 - re-exported
from .approval import approval_reason, standard_action
from .guardrails import GuardrailResult, apply_guardrails
from .loop import AgentResult
from .osticket import ESCALATION_NOTE_MARKER, RECOMMENDATION_NOTE_MARKER, Ticket
from .rules import rules_decide
from .tools import STATUS_TEXT

log = logging.getLogger(__name__)


class Runner(DeskRunner):
    agent_name = "NMB Connect AI agent"
    incident_label = "Open incident at the time"
    escalation_marker = ESCALATION_NOTE_MARKER
    recommendation_marker = RECOMMENDATION_NOTE_MARKER
    result_type = AgentResult
    audit_scope = "closed NMB Connect"
    approval_reason = staticmethod(approval_reason)

    # ---- which tickets ---------------------------------------------------------------------
    def discover(self, summary: RunSummary) -> list[Ticket]:
        return self.store.open_tickets()

    def on_request(self, number: str) -> list[Ticket]:
        tickets = [t for t in self.store.open_tickets() if t.number == number]
        if not tickets:
            log.error("Ticket #%s is not an open NMB Connect ticket", number)
        return tickets

    # ---- deciding ----------------------------------------------------------------------------
    def fallback(self, ticket: Ticket, why: str) -> Optional[AgentResult]:
        result = rules_decide(ticket, why=why, agent_staff_id=self.s.reply_staff_id)
        return None if result is None or result.decision.action == "no_action" else result

    def guard(self, ticket: Ticket, result: AgentResult, fresh: Any, live: bool) -> GuardrailResult:
        return apply_guardrails(
            result.decision, ticket, fresh, result.evidence, result.pseudo,
            confidence_threshold=self.s.confidence_threshold,
            closes_so_far=self.closes if live else 0,
            max_closes=self.s.max_closes_per_run,
            allow_request_info=self.s.allow_request_info,
            check_live_state=live,
            max_info_requests=self.s.max_info_requests,
            agent_staff_id=self.s.reply_staff_id,
            topics=self.s.topics,
            exclude_topics=self.s.exclude_topics,
        )

    def standard_action(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return standard_action(result.evidence)

    def template(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        assessment = result.evidence.assessment
        return assessment.reply if assessment is not None and assessment.reply else None

    def template_for(self, ticket: Ticket, result: AgentResult, action: str) -> Optional[str]:
        assessment = result.evidence.assessment
        if assessment is None or not assessment.reply:
            return None
        return assessment.reply if assessment.action == action else None

    # ---- notes and the log -----------------------------------------------------------------------
    def finding_lines(self, result: AgentResult) -> list[str]:
        ev = result.evidence
        lines: list[str] = []
        a = ev.assessment
        if a is not None:
            lines.append(f"Assessment: {a.problem} -> {a.status} ({STATUS_TEXT.get(a.status, a.status)})"
                         + (f", CIF {a.cif}" if a.cif else "") + (f", account {a.account}" if a.account else ""))
            lines.append("")
        if ev.profiles:
            lines.append("NMB Connect profiles found:")
            for p in ev.profiles.values():
                lines.append(f"- {p.ref}: CIF {p.cif or '?'}, mobile {p.mobile or '?'}, created "
                             f"{p.created.strftime('%d/%m/%Y') if p.created else '?'}"
                             + ("" if p.active else ", INACTIVE") + (", LOCKED/BLOCKED" if p.locked else "")
                             + (f", {p.state}" if p.state else ""))
            lines.append("")
        elif ev.profile_lookups:
            lines.append("No NMB Connect profile found ("
                         + ", ".join(f"by {x['by']} {x['value']}" for x in ev.profile_lookups) + ").")
            lines.append("")
        for cif, customer in ev.customers.items():
            accounts = ", ".join(f"{acct.account} {acct.currency or ''} {acct.state}".strip()
                                 for acct in customer.get("accounts", [])) or "none"
            lines.append(f"Core banking CIF {cif}: accounts {accounts}; mobile on file "
                         f"{customer.get('mobile_on_file') or '-'}; e-mail on file {customer.get('email_on_file') or '-'}"
                         + (f"; IDCCOB mobile {', '.join(customer['idccob_mobiles'])}" if customer.get("idccob_mobiles") else ""))
        if ev.pending:
            lines.append(f"Pending changes awaiting authorisation: {ev.pending}")
        if ev.activity and ev.activity.get("found"):
            last = ev.activity.get("last_failure") or {}
            lines.append(f"App activity: {ev.activity.get('summary')}"
                         + (f"; last failure {last.get('when')} {last.get('operation')}: {last.get('message')}"
                            if last else ""))
        if ev.failures:
            f = ev.failures[0]
            lines.append(f"Failed registration {f['when'].strftime('%d/%m/%Y %H:%M') if f.get('when') else '?'}: "
                         f"{f['reason_code']} ({f.get('message') or f['explanation']})")
        if ev.sms:
            lines.append(f"SMS gateway: {ev.sms.get('summary')}")
        for s in ev.searches:
            args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
            lines.append(f"Looked up {s['tool']} ({args}): {s['found']} found")
        if ev.unavailable:
            lines.append("Not available: " + "; ".join(ev.unavailable[:4]))
        if ev.customers or ev.searches or ev.unavailable:
            lines.append("")
        return lines

    def next_step(self, ticket: Ticket, result: AgentResult) -> str:
        a = result.evidence.assessment
        if a is not None and a.next_step:
            return "Next step: " + a.next_step
        return "Next step: review the profile and core banking findings above and follow up with the branch."

    def record_fields(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        ev = result.evidence
        template = self.template(ticket, result)
        a = ev.assessment
        facts = None
        if a is not None:
            facts = {"assessment": {"problem": a.problem, "status": a.status, "action": a.action,
                                    "profiles": len(ev.client_profiles()), "branch": ticket.org_name, **a.facts}}
        return {"facts": facts, "account_statuses": ev.statuses,
                "template_reply": result.pseudo.mask(template) if template else None}

    # ---- closed-ticket replay ------------------------------------------------------------------------
    def closed_tickets(self, since: date) -> list[Ticket]:
        return self.store.closed_tickets(since)

    def audit_columns(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        a = result.evidence.assessment
        return {"problem": a.problem if a else "", "status": result.evidence.status}
