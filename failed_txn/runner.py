"""The failed-transactions desk's pass over its tickets, and the closed-ticket replay. The pass itself is
agentcore.runner.DeskRunner; this module says what is particular to failed transactions."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from agentcore.runner import EXPIRED_CATEGORY, REQUEST_EXPIRED, DeskRunner, RunSummary  # noqa: F401 - re-exported
from .approval import approval_reason, standard_action
from .guardrails import GuardrailResult, apply_guardrails
from .loop import AgentResult
from .normalize import format_timestamp
from .osticket import ESCALATION_NOTE_MARKER, RECOMMENDATION_NOTE_MARKER, Ticket
from .rules import rules_decide
from .tools import standard_reply

log = logging.getLogger(__name__)

NEXT_STEPS = {
    "reversal_pending": "Operations: a reversal is queued but not confirmed. Check it completes (biller / switch "
                        "reconciliation) before telling the client the money is back.",
    "reversal_failed": "Operations: the automatic reversal failed. Reverse the debit manually, quoting the reference "
                       "above, then advise the branch.",
    "uncertain": "Operations: the transaction has no final answer (timeout or pending). Check core banking for a debit "
                 "and reverse it if the money did not reach the recipient, quoting the reference above.",
    "failed": "Operations: the transaction failed and core banking shows a debit without a refund. Reverse it, "
              "quoting the reference above.",
    "posted": "Check the posting above against the client's complaint.",
    "not_found": "Find the transaction with the branch (date, amount, reference) or in Postilion Office.",
}


class Runner(DeskRunner):
    agent_name = "failed-transactions AI agent"
    incident_label = "Open incident at the time"
    escalation_marker = ESCALATION_NOTE_MARKER
    recommendation_marker = RECOMMENDATION_NOTE_MARKER
    result_type = AgentResult
    audit_scope = "closed failed-transaction"
    approval_reason = staticmethod(approval_reason)

    # ---- which tickets ---------------------------------------------------------------------
    def discover(self, summary: RunSummary) -> list[Ticket]:
        return self.store.open_tickets()

    def on_request(self, number: str) -> list[Ticket]:
        tickets = [t for t in self.store.open_tickets() if t.number == number]
        if not tickets:
            log.error("Ticket #%s is not an open failed-transaction ticket", number)
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
        )

    def standard_action(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return standard_action(result.evidence)

    def template(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        ev = result.evidence
        return standard_reply(ev.subject, ev.debit, ev.status)[1] or None

    def template_for(self, ticket: Ticket, result: AgentResult, action: str) -> Optional[str]:
        ev = result.evidence
        expected, text = standard_reply(ev.subject, ev.debit, ev.status)
        return text if text and expected == action else None

    # ---- notes and the log -----------------------------------------------------------------------
    def finding_lines(self, result: AgentResult) -> list[str]:
        ev = result.evidence
        lines: list[str] = []
        txn = ev.subject
        if txn is not None:
            lines.append(f"Confirmed transaction: {txn.channel}, {format_timestamp(txn.when)}, "
                         f"{txn.currency or ''} {txn.amount if txn.amount is not None else '?'} on account "
                         f"{txn.account or '-'}" + (f", to {txn.counterparty}" if txn.counterparty else ""))
            lines.append(f"  {txn.detail} -> {ev.status}" + (f"; {txn.reversal}" if txn.reversal else ""))
            if txn.reference or txn.trace:
                lines.append(f"  Reference {txn.reference or '-'}" + (f", STAN/system ref {txn.trace}" if txn.trace else ""))
            if ev.debit:
                d = ev.debit
                lines.append(f"  Core banking: debited {d.get('debited')}" + (f" ({d.get('debit_on')})" if d.get("debit_on") else "")
                             + (f"; refunded {d.get('refunded')}" if d.get("refunded") else "")
                             + (f" ({d.get('refund_on')}: {d.get('refund')})" if d.get("refund_on") else ""))
            lines.append("")
        others = [t for t in ev.txns.values() if txn is None or t.id != txn.id][:8]
        if others:
            lines.append("Other transactions the lookups found:")
            for t in others:
                lines.append(f"- {t.id} {t.channel}, {format_timestamp(t.when)}, {t.currency or ''} "
                             f"{t.amount if t.amount is not None else '?'}: {t.status} ({t.detail[:90]})")
            lines.append("")
        for s in ev.searches:
            args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
            lines.append(f"Searched {s['tool']} ({args}){' ' + s['window'] if s.get('window') else ''}: {s['found']} found")
        if ev.unread:
            lines.append("Could not read: " + "; ".join(ev.unread[:4]))
        if ev.searches or ev.unread:
            lines.append("")
        return lines

    def next_step(self, ticket: Ticket, result: AgentResult) -> str:
        step = NEXT_STEPS.get(result.evidence.status)
        return "Next step: " + (step or "review the transactions above and follow up with the branch.")

    def record_fields(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        ev = result.evidence
        template = self.template(ticket, result)
        txn = ev.subject
        facts = None
        if txn is not None:
            facts = {"confirmed": {"source": txn.source, "channel": txn.channel, "status": ev.status,
                                   "amount": str(txn.amount) if txn.amount is not None else None,
                                   "currency": txn.currency, "debited": (ev.debit or {}).get("debited"),
                                   "refunded": (ev.debit or {}).get("refunded"), "branch": ticket.org_name}}
        return {"facts": facts, "account_statuses": ev.statuses,
                "template_reply": result.pseudo.mask(template) if template else None}

    # ---- closed-ticket replay ------------------------------------------------------------------------
    def closed_tickets(self, since: date) -> list[Ticket]:
        return self.store.closed_tickets(since)

    def audit_columns(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        return {"status": result.evidence.status}
