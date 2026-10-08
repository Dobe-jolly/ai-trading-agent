"""The SMS Alerts desk's pass over its tickets (and SMS issues filed under other topics), and the closed-ticket
audit replay. The pass itself is agentcore.runner.DeskRunner; this module says what is particular to SMS alerts."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from agentcore.metabase import MetabaseError
from agentcore.runner import EXPIRED_CATEGORY, REQUEST_EXPIRED, DeskRunner, RunSummary  # noqa: F401 - re-exported
from .anomaly.features import account_facts
from .approval import approval_reason, standard_action
from .config import Settings
from .decision import NO_ACTION, REPLY_AND_CLOSE
from .guardrails import GuardrailResult, apply_guardrails
from .loop import AgentResult
from .normalize import now_local
from .osticket import ESCALATION_NOTE_MARKER, RECOMMENDATION_NOTE_MARKER, Ticket
from .rules import rules_decide
from .triage import Triage, TriageError

log = logging.getLogger(__name__)


class Runner(DeskRunner):
    agent_name = "AI SMS agent"
    incident_label = "Open SMS incident at the time"
    escalation_marker = ESCALATION_NOTE_MARKER
    recommendation_marker = RECOMMENDATION_NOTE_MARKER
    result_type = AgentResult
    audit_scope = "closed SMS Alerts"
    approval_reason = staticmethod(approval_reason)

    def __init__(self, settings: Settings, store: Any, agent: Any, toolbox: Any, writer: Any, audit: Any, mode: str,
                 notifier: Optional[Any] = None, anomaly: Optional[Any] = None, reader: Optional[Any] = None,
                 triage: Optional[Triage] = None) -> None:
        super().__init__(settings, store, agent, toolbox, writer, audit, mode, notifier, anomaly, reader)
        self.triage = triage

    # ---- which tickets ---------------------------------------------------------------------
    def discover(self, summary: RunSummary) -> list[Ticket]:
        """The open SMS Alerts tickets, then the open tickets under other topics that triage judges to be about
        SMS alerts."""
        scanning = self.triage is not None and self.s.scan_other_topics
        since = (now_local() - timedelta(days=self.s.scan_days)).strftime("%Y-%m-%d %H:%M:%S") if scanning else None
        found = self.store.open_tickets(self.s.sms_topics, since, self.s.exclude_topics)
        tickets = [t for t in found if t.topic_match]
        if scanning:
            tickets += self._pick_from_other_topics([t for t in found if not t.topic_match], summary)
            tickets.sort(key=lambda t: (len(t.number), t.number), reverse=True)  # newest first, as the query orders them
        return tickets

    def on_request(self, number: str) -> list[Ticket]:
        tickets = [t for t in self.store.open_tickets(self.s.sms_topics, "2000-01-01 00:00:00") if t.number == number]
        if not tickets:
            log.error("Ticket #%s is not open", number)
        for t in tickets:
            if not t.topic_match:
                self._triage_on_request(t)
        return tickets

    def origin(self, ticket: Ticket) -> Optional[str]:
        return None if ticket.topic_match else f"filed under another topic; {ticket.found_by}"

    def _pick_from_other_topics(self, others: list[Ticket], summary: RunSummary) -> list[Ticket]:
        assert self.triage is not None
        if not others:
            return []
        last_messages = self.store.last_message_ids([t.ticket_id for t in others])
        verdicts = {t.ticket_id: self.triage.cached(t.ticket_id, last_messages.get(t.ticket_id)) for t in others}
        to_read = [t for t in others if verdicts[t.ticket_id] is None]
        budget = self.s.triage_max_per_run
        if len(to_read) > budget:
            summary.triage["left for the next pass"] += len(to_read) - budget
            to_read = to_read[:budget]
        threads = self.store.thread_ids(to_read)
        for ticket in to_read:
            if self.s.kill_switch_file.exists():
                break
            ticket.thread_id = threads.get(ticket.number)
            self.audit.live_ticket(ticket.number, ticket.help_topic or "(no topic)")
            self.audit.live_step("triage: is this about SMS alerts?")
            try:
                self.store.load_thread(ticket)
                verdicts[ticket.ticket_id] = self.triage.classify(ticket, last_messages.get(ticket.ticket_id))
                self.audit.live_step(f"triage: {verdicts[ticket.ticket_id].kind}")
            except (MetabaseError, TriageError) as exc:
                log.error("#%s: triage failed: %s", ticket.number, exc)
                summary.errors += 1
        picked = []
        for ticket in others:
            verdict = verdicts.get(ticket.ticket_id)
            if verdict is None:
                continue
            if self.triage.wants(verdict):
                ticket.triage_kind, ticket.found_by = verdict.kind, verdict.reason
                picked.append(ticket)
                summary.triage[f"picked ({verdict.kind})"] += 1
            else:
                summary.triage[verdict.kind if verdict.kind != "sms_alerts" else "unsure"] += 1
        if picked:
            log.info("%d ticket(s) under other topics are about SMS alerts: %s", len(picked),
                     ", ".join(f"#{t.number} ({t.help_topic or 'no topic'})" for t in picked))
        return picked

    def _triage_on_request(self, ticket: Ticket) -> None:
        """run --ticket N on a ticket under another topic: read it with triage too, so the same rules apply."""
        ticket.found_by = "run on request for this ticket"
        if self.triage is None:
            return
        try:
            self.store.load_thread(ticket)
            verdict = self.triage.classify(ticket, ticket.last_message_id)
        except (MetabaseError, TriageError) as exc:
            log.error("#%s: triage failed: %s", ticket.number, exc)
            return
        ticket.triage_kind, ticket.found_by = verdict.kind, f"run on request; triage: {verdict.kind} - {verdict.reason}"

    def other_topic_skip(self, ticket: Ticket, last: Any) -> Optional[str]:
        if ticket.handled_by_staff(self.s.reply_staff_id):
            return "staff are handling it under its own topic"
        if last is not None and last["final_action"] == NO_ACTION and last["last_entry_id"] == ticket.last_entry_id:
            return "not an SMS alerts issue (agent)"
        return None

    # ---- deciding ----------------------------------------------------------------------------
    def fallback(self, ticket: Ticket, why: str) -> Optional[AgentResult]:
        if not ticket.topic_match:
            return None  # the rules only know SMS Alerts tickets; others wait for the model
        return rules_decide(ticket, self.toolbox.checks, self.s.no_close_statuses, why=why)

    def guard(self, ticket: Ticket, result: AgentResult, fresh: Any, live: bool) -> GuardrailResult:
        return apply_guardrails(
            result.decision, ticket, fresh, result.evidence, result.pseudo,
            confidence_threshold=self.s.confidence_threshold,
            closes_so_far=self.closes if live else 0,
            max_closes=self.s.max_closes_per_run,
            allow_request_info=self.s.allow_request_info,
            check_live_state=live,
            no_close_statuses=self.s.no_close_statuses,
            max_info_requests=self.s.max_info_requests,
            agent_staff_id=self.s.reply_staff_id,
        )

    def standard_action(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return standard_action(result.evidence.statuses, self.s.no_close_statuses)

    def template(self, ticket: Ticket, result: AgentResult) -> Optional[str]:
        return result.evidence.template_reply(ticket.required_accounts, ticket.currency)

    def template_for(self, ticket: Ticket, result: AgentResult, action: str) -> Optional[str]:
        return self.template(ticket, result) if action == REPLY_AND_CLOSE else None

    # ---- notes and the log -----------------------------------------------------------------------
    def finding_lines(self, result: AgentResult) -> list[str]:
        lines: list[str] = []
        for account, r in result.evidence.account_results.items():
            lines.append(f"Account {account} - status: {r.get('status')}")
            lines.append(str(r.get("reply")))
            socket = r.get("socket_delivery")
            if isinstance(socket, dict) and socket.get("detail"):
                lines.append(f"Gateway: {socket['detail']}")
            lines.append("")
        for trace in result.evidence.pipeline_traces:
            lines.append(f"Pipeline trace ({trace.get('window')}): {trace.get('verdict')}")
            lines.append("")
        for item in result.evidence.lookups:
            args = ", ".join(f"{v}" for v in item["args"].values())
            lines.append(f"Lookup {item['tool']} ({args}): {item['summary']}")
        if result.evidence.lookups:
            lines.append("")
        return lines

    def next_step(self, ticket: Ticket, result: AgentResult) -> str:
        phone = ticket.phone_number or "the client's"
        return (f"Next step: check the SMS queue monitor for the {phone} traffic and confirm whether the CAE "
                "request and socket send exist for these alerts.")

    def found_line(self, ticket: Ticket) -> Optional[str]:
        return (f"This ticket is under the help topic '{ticket.help_topic or '(none)'}', not SMS Alerts; the agent picked "
                f"it up because it reads as an SMS alerts issue: {ticket.found_by or 'run on request'}")

    def record_fields(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        template = self.template(ticket, result)
        facts = {result.pseudo.mask(a): account_facts(r, result.evidence.phones_used.get(a), ticket.org_name)
                 for a, r in result.evidence.account_results.items()}
        return {"facts": facts or None,
                "account_statuses": {result.pseudo.mask(a): s for a, s in result.evidence.statuses.items()},
                "template_reply": result.pseudo.mask(template) if template else None}

    # ---- closed-ticket audit (port of SmsAgentService.runClosedAudit) ------------------------------
    def closed_tickets(self, since: date) -> list[Ticket]:
        return self.store.closed_sms_tickets(since)

    def audit_columns(self, ticket: Ticket, result: AgentResult) -> dict[str, Any]:
        return {"statuses": "; ".join(f"{a}={s}" for a, s in result.evidence.statuses.items())}
