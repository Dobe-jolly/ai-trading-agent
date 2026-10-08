"""One pass over open EcoCash tickets, and the closed-ticket replay."""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

from agentcore.audit import AuditLog, is_stale
from .config import Settings
from .approval import ApprovalStore, approval_reason, standard_action, token_map
from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from agentcore.pii import Pseudonymizer
from .guardrails import GuardrailResult, apply_guardrails, reply_problems
from .loop import AgentError, AgentResult, TicketAgent
from agentcore.metabase import MetabaseError
from .normalize import LOCAL_ZONE, format_timestamp, now_local, parse_timestamp, today_local
from .rules import rules_decide
from .osticket import (
    ESCALATION_NOTE_MARKER,
    RECOMMENDATION_NOTE_MARKER,
    OsTicket,
    Ticket,
    closed_date,
)
from .tools import Toolbox, standard_reply
from .anomaly.features import attempt_facts
from .attachments import AttachmentReader, ReadResult
from .notify import Notifier
from .writer import TicketWriter

log = logging.getLogger(__name__)
REQUEST_EXPIRED = "request for information expired"
EXPIRED_CATEGORY = "request_info_timeout"


@dataclass
class RunSummary:
    run_id: str = ""
    seen: int = 0
    decided: int = 0
    errors: int = 0
    actions: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    emails: dict = field(default_factory=dict)

    def line(self) -> str:
        acts = ", ".join(f"{k}={v}" for k, v in sorted(self.actions.items())) or "none"
        skips = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items())) or "none"
        mails = ", ".join(f"{k}={v}" for k, v in self.emails.items() if v) or "none"
        return (f"run {self.run_id}: seen={self.seen} decided={self.decided} errors={self.errors} "
                f"actions[{acts}] skipped[{skips}] emails[{mails}]")


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


def _finding_lines(result: AgentResult) -> list[str]:
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


def escalation_note(ticket: Ticket, result: AgentResult, guard: GuardrailResult, context: Optional[str] = None) -> str:
    """Internal note for the person picking up the ticket. Starts with the marker."""
    p = guard.proposed
    lines = [ESCALATION_NOTE_MARKER, "Analysed by the EcoCash AI agent."]
    if guard.changed:
        lines.append(f"The agent proposed '{p.action}', but its safety rules stopped it: {'; '.join(guard.notes)}")
    lines += ["", "Findings:", p.reason, ""]
    if p.evidence:
        lines += [f"- {item}" for item in p.evidence] + [""]
    lines += _finding_lines(result)
    if p.reply_text:
        lines += ["Draft reply (not sent):", p.reply_text, ""]
    if context:
        lines += [f"Open EcoCash incident at the time: {context}", ""]
    step = NEXT_STEPS.get(result.evidence.status)
    lines.append("Next step: " + (step or "review the attempts above and follow up with the branch."))
    return result.pseudo.unmask("\n".join(lines).strip())


def recommendation_note(result: AgentResult, guard: GuardrailResult, posted_text: Optional[str]) -> str:
    """notes mode: what the agent would do, for staff to action."""
    p, f = guard.proposed, guard.final
    lines = [RECOMMENDATION_NOTE_MARKER, f"Recommended action: {f.action} (confidence {p.confidence:.0%})"]
    if guard.changed:
        lines.append(f"Model proposed '{p.action}'; safety rules changed it: {'; '.join(guard.notes)}")
    if posted_text:
        lines += ["", "Reply it would post:", posted_text]
    lines += ["", "Reasoning:", p.reason, ""]
    if p.evidence:
        lines += [f"- {item}" for item in p.evidence] + [""]
    lines += _finding_lines(result)
    return result.pseudo.unmask("\n".join(lines).strip())


def _same_text(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return re.sub(r"\s+", " ", a).strip() == re.sub(r"\s+", " ", b).strip()


class Runner:
    def __init__(
        self,
        settings: Settings,
        store: OsTicket,
        agent: TicketAgent,
        toolbox: Toolbox,
        writer: TicketWriter,
        audit: AuditLog,
        mode: str,
        notifier: Optional[Notifier] = None,
        anomaly: Optional[Any] = None,
        reader: Optional[AttachmentReader] = None,
    ) -> None:
        self.s = settings
        self.store = store
        self.agent = agent
        self.toolbox = toolbox
        self.writer = writer
        self.audit = audit
        self.mode = mode
        self.closes = 0
        self.notifier = notifier
        self.anomaly = anomaly
        self.reader = reader
        self.approvals = ApprovalStore(audit.db)

    # ---- live / shadow pass ----------------------------------------------------------
    def run(self, limit: Optional[int] = None, ticket_number: Optional[str] = None, force: bool = False) -> RunSummary:
        summary = RunSummary(run_id=self.audit.start_run(self.mode, self.s.model))
        queue: list[dict[str, Any]] = []
        full_run = not ticket_number
        self.audit.live_start(summary.run_id)
        self.agent.on_step = self.audit.live_step
        if self.anomaly is not None:
            self.agent.context = self.anomaly.context()
            if self.agent.context:
                log.info("Operations context for this pass: %s", self.agent.context)
        try:
            tickets = self.store.open_tickets()
            if ticket_number:
                tickets = [t for t in tickets if t.number == ticket_number.strip()]
                if not tickets:
                    log.error("Ticket #%s is not an open EcoCash ticket", ticket_number)
                force = True
            all_open = tickets
            tickets = tickets[: limit or self.s.max_tickets_per_run]
            threads = self.store.thread_ids(tickets)
            for ticket in tickets:
                item = {"ticket_id": ticket.ticket_id, "ticket_number": ticket.number,
                        "help_topic": ticket.help_topic, "opened": ticket.created}
                queue.append(item)
                summary.seen += 1
                if self.s.kill_switch_file.exists():
                    log.warning("Kill switch %s appeared; stopping", self.s.kill_switch_file)
                    item.update(state="not reached", detail="kill switch")
                    break
                ticket.thread_id = threads.get(ticket.number)
                self.audit.live_ticket(ticket.number, ticket.help_topic or "(no topic)")
                try:
                    self.store.load_thread(ticket)
                except MetabaseError as exc:
                    log.error("#%s: could not read thread: %s", ticket.number, exc)
                    summary.errors += 1
                    item.update(state="error", detail=str(exc)[:300])
                    continue
                waiting = self._approval_step(ticket, summary.run_id)
                if waiting is None:
                    reason = self._skip_reason(ticket, force)
                    if reason is None and not ticket.topic_match:
                        reason = self._content_precheck(ticket, summary.run_id)
                else:
                    reason = waiting[1] if waiting[0] == "skip" else None
                if waiting is not None and waiting[0] == "done":
                    outcome = waiting[1]
                elif reason == REQUEST_EXPIRED:
                    outcome = self._follow_up_expired_request(ticket, summary.run_id)
                elif reason:
                    log.debug("#%s skipped: %s", ticket.number, reason)
                    summary.skipped[reason] += 1
                    last = self.audit.last_for_ticket(ticket.ticket_id)
                    item.update(state="skipped", detail=reason, decision_id=last["id"] if last else None)
                    continue
                else:
                    outcome = self._process(ticket, summary.run_id)
                if outcome is None:
                    summary.errors += 1
                    item.update(state="error", detail="see the decision log")
                else:
                    action, decision_id = outcome
                    summary.decided += 1
                    summary.actions[action] += 1
                    item.update(state=action, detail=None, decision_id=decision_id)
            for ticket in all_open[len(tickets):]:
                queue.append({"ticket_id": ticket.ticket_id, "ticket_number": ticket.number, "help_topic": ticket.help_topic,
                              "opened": ticket.created, "state": "not reached", "detail": "over MAX_TICKETS_PER_RUN"})
            if full_run:
                self.approvals.drop_missing(t.ticket_id for t in all_open)
            if self.notifier and not self.s.kill_switch_file.exists():
                summary.emails = self.notifier.dispatch(self.s.notify_max_attempts)
            if self.anomaly is not None and full_run and not self.s.kill_switch_file.exists():
                self.anomaly.maybe_run()
        finally:
            self.audit.live_end()
            self.audit.finish_run(summary.run_id, summary.seen, summary.decided, summary.errors)
            if full_run:
                for item in queue:
                    item.setdefault("state", "not reached")
                self.audit.replace_queue(summary.run_id, queue)
        log.info(summary.line())
        return summary

    # ---- replies waiting for approval ---------------------------------------------------
    def _approval_step(self, ticket: Ticket, run_id: str) -> Optional[tuple[str, Any]]:
        """("skip", reason) while a reply waits, ("done", outcome) once a verdict was carried out, None otherwise."""
        if ticket.thread_id is None:
            return None
        row = self.approvals.open_for(ticket.ticket_id)
        if row is None:
            return None
        if row["last_entry_id"] != ticket.last_entry_id:
            self.approvals.finish(row["id"], "dropped", "the ticket changed before the reply was posted")
            return None  # decide again on the new thread
        if row["status"] == "pending":
            return "skip", "awaiting approval"
        if self.mode != "live":
            return "skip", f"{row['status']} in the console; carried out when the agent runs live"
        if row["status"] == "approved" and row["action"] == REPLY_AND_CLOSE and self.closes >= self.s.max_closes_per_run:
            return "skip", f"approved; close limit of {self.s.max_closes_per_run} per run reached"
        fresh = self.store.state(ticket)
        if fresh is None or not fresh.is_open or fresh.last_entry_id != ticket.last_entry_id:
            self.approvals.finish(row["id"], "dropped", "the ticket changed or closed before the reply was posted")
            return "skip", "ticket changed while the reply waited"
        if row["status"] == "approved":
            return "done", self._post_approved(ticket, row, run_id)
        return "done", self._note_rejected(ticket, row, run_id)

    def _post_approved(self, ticket: Ticket, row: Any, run_id: str) -> Optional[tuple[str, int]]:
        action = row["action"]
        pseudo = Pseudonymizer.from_export(token_map(row))
        text = pseudo.unmask(row["reply_masked"]) if row["edited"] else (row["reply_text"] or "")
        problems = reply_problems(row["reply_masked"] or "", pseudo) if row["edited"] else []
        executed: list[str] = []
        error = None
        if problems or not text.strip():
            error = "approved reply not posted: " + ("; ".join(problems) or "no text")
        else:
            try:
                self.writer.post_reply(ticket, text)
                executed.append("reply")
                if action == REPLY_AND_CLOSE:
                    self.writer.close(ticket)
                    executed.append("close")
                    self.closes += 1
                else:
                    self.writer.mark_answered(ticket)
                    executed.append("answered")
            except Exception as exc:  # noqa: BLE001 - recorded, never crashes the run
                error = f"write failed after {executed or 'nothing'}: {exc}"[:1000]
        if executed and self.notifier and self.notifier.wants(action):
            executed.append("email queued")
        who = row["decided_by"] or "a reviewer"
        decision_id = self.audit.record(
            run_id=run_id, mode=self.mode, ticket_id=ticket.ticket_id, ticket_number=ticket.number,
            last_entry_id=ticket.last_entry_id, last_message_id=ticket.last_message_id, proposed_action=action,
            final_action=action if not error else ESCALATE, confidence=1.0, category="approved_reply",
            reason=f"Approved in the console by {who}" + (f" (edited)" if row["edited"] else "")
                   + (f": {row['comment']}" if row["comment"] else "."),
            reply_text=row["reply_masked"], executed=executed, error=error,
            approval=f"approved by {who}; reply from decision {row['decision_id']}")
        if "email queued" in executed:
            self._queue_email(ticket, decision_id, action, text)
        self.approvals.finish(row["id"], "posted" if not error else "failed", error or ", ".join(executed))
        if error:
            log.error("#%s: %s", ticket.number, error)
            return None
        log.info("#%s: approved %s posted (%s)", ticket.number, action, who)
        return action, decision_id

    def _note_rejected(self, ticket: Ticket, row: Any, run_id: str) -> Optional[tuple[str, int]]:
        who = row["decided_by"] or "a reviewer"
        pseudo = Pseudonymizer.from_export(token_map(row))
        draft = pseudo.unmask(row["reply_masked"] or "")
        note = "\n".join([ESCALATION_NOTE_MARKER,
                           f"The AI EcoCash agent proposed a reply that {who} rejected in the agent console"
                           + (f": {row['comment']}" if row["comment"] else ".") + " Please handle this ticket.",
                           "", "Proposed reply (not sent):", draft])
        executed: list[str] = []
        error = None
        try:
            if self.writer.note(ticket, note, ESCALATION_NOTE_MARKER):
                executed.append("note")
        except Exception as exc:  # noqa: BLE001
            error = f"note failed: {exc}"[:1000]
        decision_id = self.audit.record(
            run_id=run_id, mode=self.mode, ticket_id=ticket.ticket_id, ticket_number=ticket.number,
            last_entry_id=ticket.last_entry_id, last_message_id=ticket.last_message_id, proposed_action=row["action"],
            final_action=ESCALATE, confidence=1.0, category="approval_rejected",
            reason=f"Rejected in the console by {who}" + (f": {row['comment']}" if row["comment"] else "."),
            executed=executed, error=error, approval=f"rejected by {who}")
        self.approvals.finish(row["id"], "escalated" if not error else "failed", error or "escalation note posted")
        return (ESCALATE, decision_id) if not error else None

    def _skip_reason(self, ticket: Ticket, force: bool) -> Optional[str]:
        if ticket.thread_id is None:
            return "no thread"
        if force:
            return None
        staff = self.s.reply_staff_id
        if ticket.escalated_by_agent(staff):
            return "already escalated"
        if self.mode == "notes" and ticket.escalated_by_agent(staff, RECOMMENDATION_NOTE_MARKER):
            return "recommendation already posted"
        last = self.audit.last_for_ticket(ticket.ticket_id, self.mode)
        if ticket.answered_since_last_message():
            if self._request_expired(ticket) and not (
                    last is not None and last["category"] == EXPIRED_CATEGORY
                    and last["last_entry_id"] == ticket.last_entry_id):
                return REQUEST_EXPIRED
            return "waiting on ticket owner"
        if last is not None and last["last_entry_id"] == ticket.last_entry_id \
                and not is_stale(last["created_at"], self.s.recheck_after_hours):
            return "unchanged since last decision"
        return None

    def _content_precheck(self, ticket: Ticket, run_id: str) -> Optional[str]:
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

    def _request_expired(self, ticket: Ticket) -> bool:
        """The newest reply is the agent's own request for information, unanswered for too long."""
        hours = self.s.request_info_timeout_hours
        if not hours or self.s.reply_staff_id is None:
            return False
        replies = [e for e in ticket.entries if e.type == "R"]
        if not replies or replies[-1].staff_id != self.s.reply_staff_id:
            return False
        posted = parse_timestamp(replies[-1].created)
        return posted is not None and (now_local() - posted).total_seconds() >= hours * 3600

    def _follow_up_expired_request(self, ticket: Ticket, run_id: str) -> Optional[tuple[str, int]]:
        """No answer to the agent's question: hand the ticket to a person (no model call)."""
        hours = self.s.request_info_timeout_hours
        decision = Decision(ESCALATE, 1.0, EXPIRED_CATEGORY,
                            f"The agent asked the ticket owner for information and has had no reply for over {hours:g} "
                            "hours. A person should follow up with the branch or close the ticket.")
        result = AgentResult(decision=decision)
        guard = GuardrailResult(decision, decision)
        executed, error = self._execute(ticket, result, guard, None)
        decision_id = self._record(
            {"run_id": run_id, "mode": self.mode, "ticket_id": ticket.ticket_id, "ticket_number": ticket.number,
             "last_entry_id": ticket.last_entry_id, "last_message_id": ticket.last_message_id},
            ticket, result, guard, executed=executed, error=error)
        log.info("#%s: escalate (request for information unanswered for %gh)", ticket.number, hours)
        return ESCALATE, decision_id

    def _process(self, ticket: Ticket, run_id: str) -> Optional[tuple[str, int]]:
        base = {
            "run_id": run_id, "mode": self.mode, "ticket_id": ticket.ticket_id, "ticket_number": ticket.number,
            "last_entry_id": ticket.last_entry_id, "last_message_id": ticket.last_message_id,
        }
        try:
            self.toolbox.as_of = None
            files = self._read_attachments(ticket)
            try:
                result = self.agent.decide(ticket)
            except AgentError as exc:
                self.audit.live_step("Claude unreachable")
                if self.s.model_fallback != "rules":
                    raise
                log.warning("#%s: %s -> using the rules fallback", ticket.number, exc)
                result = rules_decide(ticket, self.toolbox, why=str(exc)[:200])
            fresh = self.store.state(ticket)
        except (AgentError, MetabaseError) as exc:
            log.error("#%s: %s", ticket.number, exc)
            self.audit.record(**base, error=str(exc)[:1000])
            return None

        guard = self._guard(ticket, result, fresh, live=True)
        posted = self._posted_text(ticket, result, guard)
        why = None
        if guard.final.action in REPLY_ACTIONS and not guard.deferred and posted:
            why = approval_reason(self.s.approval_mode, guard.final, standard_action(result.evidence, ticket))
        if why and self.mode == "live":
            executed, error = ["awaiting approval"], None
        else:
            executed, error = self._execute(ticket, result, guard, posted)
        if guard.final.action == REPLY_AND_CLOSE and self.mode == "live" and "close" in executed:
            self.closes += 1
        elif guard.final.action == REPLY_AND_CLOSE and self.mode != "live":
            self.closes += 1  # count would-be closes so shadow mirrors the live rate limit

        if self.mode == "live" and "reply" in executed and self.notifier and self.notifier.wants(guard.final.action):
            executed.append("email queued")
        decision_id = self._record(base, ticket, result, guard, files, executed=executed, error=error,
                                   approval=(f"waiting: {why}" if self.mode == "live" else f"would wait: {why}") if why else None)
        if "email queued" in executed:
            self._queue_email(ticket, decision_id, guard.final.action, posted or "")
        if "awaiting approval" in executed:
            masked = result.pseudo.mask(posted or "")
            self.approvals.create(decision_id=decision_id, ticket_id=ticket.ticket_id, ticket_number=ticket.number,
                                  last_entry_id=ticket.last_entry_id, action=guard.final.action, why=why or "",
                                  reply_masked=masked, reply_text=posted or "", token_map=result.pseudo.export())
            log.info("#%s: %s waits for approval (%s)", ticket.number, guard.final.action, why)
            return "awaiting approval", decision_id
        p, f = guard.proposed, guard.final
        log.info("#%s: %s%s (confidence %.2f, accounts %s)%s", ticket.number, f.action,
                 f" [proposed {p.action}: {'; '.join(guard.notes)}]" if guard.changed else "",
                 p.confidence, result.evidence.statuses or "-", f" executed={executed}" if executed else "")
        return f.action, decision_id

    def _queue_email(self, ticket: Ticket, decision_id: int, action: str, posted: str) -> None:
        """Tell the ticket owner: osTicket sends no alert for replies the agent writes directly."""
        assert self.notifier is not None
        skip = None
        try:
            name, email = self.store.owner_contact(ticket)
        except MetabaseError as exc:
            name, email, skip = None, None, f"owner email lookup failed: {exc}"[:300]
        self.notifier.enqueue(decision_id=decision_id, ticket_id=ticket.ticket_id, number=ticket.number, kind=action,
                              recipient=email, owner_name=name, reply=posted, skip_reason=skip)

    def _guard(self, ticket: Ticket, result: AgentResult, fresh: Any, live: bool) -> GuardrailResult:
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

    def _posted_text(self, ticket: Ticket, result: AgentResult, guard: GuardrailResult) -> Optional[str]:
        """The exact text that would be posted to the ticket owner (unmasked), or None."""
        final = guard.final
        if final.action in (REPLY_AND_CLOSE, REQUEST_INFO) and self.s.reply_source == "template":
            action, template = standard_reply(result.evidence.latest, ticket, result.evidence.link_records)
            if template and action == final.action:
                return template
        if final.action in (REPLY_AND_CLOSE, REQUEST_INFO):
            return result.pseudo.unmask(final.reply_text)
        return None

    def _execute(self, ticket: Ticket, result: AgentResult, guard: GuardrailResult,
                 posted: Optional[str]) -> tuple[list[str], Optional[str]]:
        action = guard.final.action
        done: list[str] = []
        if self.mode == "shadow" or action == NO_ACTION:
            return done, None
        try:
            if self.mode == "notes":
                if self.writer.note(ticket, recommendation_note(result, guard, posted), RECOMMENDATION_NOTE_MARKER):
                    done.append("note")
            elif action == ESCALATE:
                if self.writer.note(ticket, escalation_note(ticket, result, guard, getattr(self.agent, "context", None)), ESCALATION_NOTE_MARKER):
                    done.append("note")
            elif action == REQUEST_INFO and posted:
                self.writer.post_reply(ticket, posted)
                done.append("reply")
                self.writer.mark_answered(ticket)
                done.append("answered")
            elif action == REPLY_AND_CLOSE and posted:
                self.writer.post_reply(ticket, posted)
                done.append("reply")
                self.writer.close(ticket)
                done.append("close")
            return done, None
        except Exception as exc:  # noqa: BLE001 - a failed write must be recorded, never crash the run
            log.error("#%s: write failed after %s: %s", ticket.number, done or "nothing", exc)
            if done:
                try:
                    self.writer.note(ticket, f"{ESCALATION_NOTE_MARKER}\nThe EcoCash AI agent completed {done} but then "
                                             f"failed: {exc}. Please check and finish this ticket manually.")
                except Exception:  # noqa: BLE001
                    log.exception("#%s: could not post the failure note", ticket.number)
            return done, f"write failed: {exc}"[:1000]

    def _read_attachments(self, ticket: Ticket) -> ReadResult:
        """Read what is attached to the ticket; the agent then sees it with the thread."""
        if self.reader is None:
            return ReadResult()
        files = self.reader.read(ticket)
        if files.attachments:
            done = Counter(a.status for a in files.attachments)
            log.info("#%s: %d attachment(s): %s", ticket.number, len(files.attachments),
                     ", ".join(f"{n} {s}" for s, n in sorted(done.items())))
        return files

    def _record(self, base: dict[str, Any], ticket: Ticket, result: AgentResult, guard: GuardrailResult,
                files: Optional[ReadResult] = None, **extra: Any) -> int:
        p, f = guard.proposed, guard.final
        latest = result.evidence.latest
        action, template = standard_reply(latest, ticket, result.evidence.link_records) \
            if "find_ecocash_attempts" in result.evidence.tools_used else ("", "")
        facts = {"latest": attempt_facts(latest, ticket)} if latest is not None else None
        return self.audit.record(
            **base,
            facts=facts,
            branch=ticket.org_name,
            proposed_action=p.action,
            final_action=f.action,
            confidence=p.confidence,
            category=p.category,
            reason=p.reason,
            evidence=p.evidence,
            account_statuses=result.evidence.statuses,
            reply_text=p.reply_text,
            template_reply=result.pseudo.mask(template) if template else None,
            guardrail_notes=guard.notes,
            tools_used=result.evidence.tools_used,
            deferred=int(guard.deferred),
            transcript=result.transcript,
            attachments=result.pseudo.mask_obj(files.log_rows()) if files and files.attachments else None,
            input_tokens=result.input_tokens + (files.input_tokens if files else 0),
            output_tokens=result.output_tokens + (files.output_tokens if files else 0),
            **extra,
        )

    # ---- closed-ticket replay: what would the agent have said, against what staff posted ---
    def audit_closed(self, since: date, limit: Optional[int], window_today: bool, out_dir: Path) -> Path:
        run_id = self.audit.start_run("audit", self.s.model)
        tickets = self.store.closed_tickets(since)[: limit or None]
        threads = self.store.thread_ids(tickets)
        log.info("Audit: %d closed EcoCash ticket(s) closed on/after %s", len(tickets), since)
        rows: list[dict[str, Any]] = []
        decided = errors = 0
        for ticket in tickets:
            ticket.thread_id = threads.get(ticket.number)
            try:
                self.store.load_thread(ticket)
            except MetabaseError as exc:
                log.error("#%s: %s", ticket.number, exc)
                errors += 1
                continue
            actual = self.store.last_staff_reply(ticket)
            # Show the agent only what existed before anyone from the desk responded.
            first_response = next((i for i, e in enumerate(ticket.entries) if e.type in ("R", "N")), None)
            if first_response is not None:
                ticket.entries = ticket.entries[:first_response]
            as_of = None if window_today else closed_date(ticket)
            self.toolbox.as_of = datetime.combine(as_of, dtime(23, 59, 59), tzinfo=LOCAL_ZONE) if as_of else None
            files = self._read_attachments(ticket)
            try:
                result = self.agent.decide(ticket, as_of=(as_of or today_local()).isoformat())
            except (AgentError, MetabaseError) as exc:
                log.error("#%s: %s", ticket.number, exc)
                errors += 1
                continue
            guard = self._guard(ticket, result, None, live=False)
            posted = self._posted_text(ticket, result, guard)
            _, template = standard_reply(result.evidence.latest, ticket, result.evidence.link_records)
            decided += 1
            self._record(
                {"run_id": run_id, "mode": "audit", "ticket_id": ticket.ticket_id, "ticket_number": ticket.number,
                 "last_entry_id": ticket.last_entry_id, "last_message_id": ticket.last_message_id},
                ticket, result, guard, files, actual_reply=result.pseudo.mask(actual) if actual else None,
            )
            rows.append({
                "ticket_num": ticket.number,
                "closed_on": ticket.closed_on,
                "help_topic": ticket.help_topic,
                "status": result.evidence.status,
                "agent_action": guard.final.action,
                "agent_proposed": guard.proposed.action,
                "guardrail_notes": "; ".join(guard.notes),
                "agent_reply": posted or "",
                "template_reply": template or "",
                "actual_last_reply": actual or "",
                "agent_same_as_actual": _same_text(posted, actual),
                "template_same_as_actual": _same_text(template, actual),
                "agent_reason": result.pseudo.unmask(guard.proposed.reason),
            })
        self.toolbox.as_of = None
        self.audit.finish_run(run_id, len(tickets), decided, errors)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"closed_audit_since_{since.isoformat()}.csv"
        with out.open("w", newline="", encoding="utf-8") as handle:
            fields = list(rows[0]) if rows else ["ticket_num"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        actions = Counter(r["agent_action"] for r in rows)
        log.info("Audit done: %d decided, %d errors, actions %s -> %s", decided, errors, dict(actions), out)
        return out
