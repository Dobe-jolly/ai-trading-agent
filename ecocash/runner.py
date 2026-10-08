"""The EcoCash desk's pass over its tickets, and the closed-ticket replay. The pass itself is
agentcore.runner.DeskRunner; this module says what is particular to EcoCash linking."""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime
from typing import Any, Optional

from agentcore.metabase import MetabaseError
from agentcore.runner import EXPIRED_CATEGORY, REQUEST_EXPIRED, DeskRunner, RunSummary  # noqa: F401 - re-exported
from .anomaly.features import attempt_facts
from .approval import approval_reason, standard_action
from .decision import NO_ACTION, Decision
from .guardrails import GuardrailResult, apply_guardrails
from .loop import AgentResult
from .normalize import LOCAL_ZONE, format_timestamp
from .osticket import ESCALATION_NOTE_MARKER, RECOMMENDATION_NOTE_MARKER, Ticket
from .rules import rules_decide
from .tools import standard_reply

log = logging.getLogger(__name__)

NOT_A_CASE = "not an EcoCash link case"
NEXT_STEPS = {
    "already_linked": "ICT: the branch could not delink the account. Delink it at EcoCash (or on the Universal "
                      "dashboard) and ask the branch to have the client link again.",
    "account_rejected": "ICT: EcoCash rejected the account number on a delink. Confirm with EcoCash which account the "
                        "wallet is linked to, then delink that one.",
    "no_response": "ICT: EcoCash did not answer this request. Check ecocashreg.service around that time and retry.",
    "unknown": "ICT: EcoCash returned an error the agent has no rule for. Check the message above with EcoCash.",
    "ecocash_system_error": "Check whether EcoCash is having an outage before advising the client to retry.",
}



class Runner(DeskRunner):
    agent_name = "EcoCash AI agent"
    incident_label = "Open EcoCash incident at the time"
    escalation_marker = ESCALATION_NOTE_MARKER
    recommendation_marker = RECOMMENDATION_NOTE_MARKER
    result_type = AgentResult
    audit_scope = "closed EcoCash"
    approval_reason = staticmethod(approval_reason)

    # ---- which tickets ---------------------------------------------------------------------
    def discover(self, summary: RunSummary) -> list[Ticket]:
        return self.store.open_tickets()

    def on_request(self, number: str) -> list[Ticket]:
        tickets = [t for t in self.store.open_tickets() if t.number == number]
        if not tickets:
            log.error("Ticket #%s is not an open EcoCash ticket", number)
        return tickets

    def precheck(self, ticket: Ticket, run_id: str) -> Optional[str]:
        """A ticket under another topic that mentions EcoCash: only worth a model call if the owner's
        details have a link attempt. Recorded, so it is looked at again only when the ticket changes."""
        found = ticket.owner_identifiers()
        try:
            for kind, values in (("mobile", found.mobiles), ("account", found.accounts), ("id_number", found.ids)):
                for value in sorted(values)[:3]:
                    if self.toolbox.attempts.find(**{kind: value}, days=self.toolbox.lookback_days).attempts:
                        return None
        except (MetabaseError, ValueError) as exc:
            log.warning("#%s: content pre-check failed: %s", ticket.number, exc)
            return None
        decision = Decision(NO_ACTION, 1.0, "not_ecocash_case",
                            "Picked up because it mentions EcoCash, but no link attempt matches the details the "
                            "ticket owner gave, so the ticket was left alone (no model call).")
        self.audit.record(run_id=run_id, mode=self.mode, ticket_id=ticket.ticket_id, ticket_number=ticket.number,
                          last_entry_id=ticket.last_entry_id, last_message_id=ticket.last_message_id,
                          proposed_action=NO_ACTION, final_action=NO_ACTION, confidence=1.0,
                          category=decision.category, reason=decision.reason, branch=ticket.org_name)
        return NOT_A_CASE

    # ---- deciding ----------------------------------------------------------------------------
    def fallback(self, ticket: Ticket, why: str) -> Optional[AgentResult]:
        return rules_decide(ticket, self.toolbox, why=why)

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
            topics=self.s.ecocash_topics,
            incident_open=str(getattr(self.agent, "context", "") or "").upper().startswith("INCIDENT"),
        )

    def standard_action(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return standard_action(result.evidence, ticket)

    def template(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return standard_reply(result.evidence.latest, ticket, result.evidence.link_records)[1]

    def template_for(self, ticket: Ticket, result: AgentResult, action: str) -> Optional[str]:
        expected, template = standard_reply(result.evidence.latest, ticket, result.evidence.link_records)
        return template if template and expected == action else None

    # ---- notes and the log -----------------------------------------------------------------------
    def finding_lines(self, result: AgentResult) -> list[str]:
        """What the lookups found, newest attempts first (masked values; unmasked by the caller)."""
        ev = result.evidence
        lines: list[str] = []
        attempts = sorted((a for a in ev.attempts.values() if a.when), key=lambda a: a.when, reverse=True)
        if attempts:
            lines.append("EcoCash link attempts (newest first):")
            for a in attempts[:6]:
                what = a.error.label + (f" (EcoCash {a.error.ecocash_code})" if a.error and a.error.ecocash_code else "") \
                    if a.error else a.outcome
                lines.append(f"- {format_timestamp(a.when)} {('delink' if a.operation == 'UNLINK' else 'link')} via "
                             f"{a.via or 'unknown'}: {what}; mobile {a.mobile or '-'}, account {a.account or '-'}"
                             + (f"; EcoCash said: {a.message}" if a.error and a.message else ""))
            lines.append("")
        elif "find_ecocash_attempts" in ev.tools_used:
            lines += ["No EcoCash link attempt was found for the details on the ticket.", ""]
        for row in ev.link_records[:3]:
            lines.append(f"Bank link record: mobile {row.get('mobile')}, account {row.get('account')}, "
                         f"since {row.get('linked_on')}, status {row.get('status')}")
        for account, data in list(ev.accounts_checked.items())[:2]:
            lines.append(f"Core banking: account {data.get('account_number')} exists={data.get('exists')}, "
                         f"currency {data.get('currency_code')}, mobile on file {data.get('mobile_on_file')}")
        for item in ev.other_lookups:
            lines.append(f"Lookup {item['tool']}: {item['summary']}")
        if ev.link_records or ev.accounts_checked or ev.other_lookups:
            lines.append("")
        return lines


    def next_step(self, ticket: Ticket, result: AgentResult) -> str:
        step = NEXT_STEPS.get(result.evidence.status)
        return "Next step: " + (step or "review the attempts above and follow up with the branch.")

    def record_fields(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        latest = result.evidence.latest
        _, template = standard_reply(latest, ticket, result.evidence.link_records) \
            if "find_ecocash_attempts" in result.evidence.tools_used else ("", "")
        return {"facts": {"latest": attempt_facts(latest, ticket)} if latest is not None else None,
                "account_statuses": result.evidence.statuses,
                "template_reply": result.pseudo.mask(template) if template else None}

    # ---- closed-ticket replay ------------------------------------------------------------------------
    def closed_tickets(self, since: date) -> list[Ticket]:
        return self.store.closed_tickets(since)

    def toolbox_as_of(self, day: Optional[date]) -> Any:
        return datetime.combine(day, dtime(23, 59, 59), tzinfo=LOCAL_ZONE) if day else None

    def audit_columns(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        return {"status": result.evidence.status}
