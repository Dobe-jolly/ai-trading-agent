"""One pass over a desk's open tickets, and the closed-ticket replay. Shared by every desk.

A desk subclasses DeskRunner and supplies what differs: which tickets it owns, what its notes say,
its guardrails, its standard (template) replies and what it records. The pass itself is the same
everywhere: replies waiting for approval first, then skip rules, then the reasoning loop, the
guardrails, the approval step, the writes, the e-mail to the ticket owner and the decision log.
"""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .approval import ApprovalStore, token_map
from .audit import AuditLog, is_stale
from .decision import ESCALATE, NO_ACTION, REPLY_ACTIONS, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from .guardrails import GuardrailResult, reply_problems
from .loop import AgentError, AgentResult
from .metabase import MetabaseError
from .normalize import now_local, parse_timestamp, today_local
from .osticket import closed_date
from .pii import Pseudonymizer

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
    triage: Counter = field(default_factory=Counter)  # tickets under other topics, by verdict
    desk: str = ""

    def line(self) -> str:
        acts = ", ".join(f"{k}={v}" for k, v in sorted(self.actions.items())) or "none"
        skips = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items())) or "none"
        mails = ", ".join(f"{k}={v}" for k, v in self.emails.items() if v) or "none"
        other = ", ".join(f"{k}={v}" for k, v in sorted(self.triage.items()))
        return ((f"{self.desk} " if self.desk else "")
                + f"run {self.run_id}: seen={self.seen} decided={self.decided} errors={self.errors} "
                f"actions[{acts}] skipped[{skips}] emails[{mails}]" + (f" other_topics[{other}]" if other else ""))


def same_text(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return re.sub(r"\s+", " ", a).strip() == re.sub(r"\s+", " ", b).strip()


class DeskRunner:
    # ---- what each desk sets -------------------------------------------------------------
    agent_name = "AI agent"                       # "Analysed by the <agent_name>."
    incident_label = "Open incident at the time"  # how the anomaly watch's context is introduced in notes
    escalation_marker = "AI agent did not auto-close this ticket."
    recommendation_marker = "AI agent recommendation (not posted to the ticket owner)."
    result_type: type = AgentResult
    audit_scope = "closed"                        # words for the replay log line

    def __init__(self, settings: Any, store: Any, agent: Any, toolbox: Any, writer: Any, audit: AuditLog,
                 mode: str, notifier: Optional[Any] = None, anomaly: Optional[Any] = None,
                 reader: Optional[Any] = None) -> None:
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

    # ---- hooks -------------------------------------------------------------------------------
    def discover(self, summary: RunSummary) -> list:
        """The open tickets this desk owns, newest first."""
        raise NotImplementedError

    def on_request(self, number: str) -> list:
        """run --ticket N: that ticket, if it is open and this desk may look at it."""
        raise NotImplementedError

    def origin(self, ticket: Any) -> Optional[str]:
        """Why a ticket under another topic is in this desk's queue (shown in the console), or None."""
        return None

    def other_topic_skip(self, ticket: Any, last: Any) -> Optional[str]:
        """Extra skip rules for tickets found under other topics."""
        return None

    def precheck(self, ticket: Any, run_id: str) -> Optional[str]:
        """A cheap check before the model for tickets under other topics: a skip reason, or None to go on."""
        return None

    def fallback(self, ticket: Any, why: str) -> Optional[AgentResult]:
        """The rules' decision when the model is unreachable, or None to record the error and retry later."""
        return None

    def guard(self, ticket: Any, result: AgentResult, fresh: Any, live: bool) -> GuardrailResult:
        raise NotImplementedError

    def standard_action(self, ticket: Any, result: AgentResult) -> Optional[str]:
        """The action the desk's standard outcome gives (for the approval step)."""
        return None

    @staticmethod
    def approval_reason(setting: str, decision: Decision, expected: Optional[str]) -> Optional[str]:
        return None

    def template(self, ticket: Any, result: AgentResult) -> Optional[str]:
        """The desk's standard reply text for what it found (unmasked), or None."""
        return None

    def template_for(self, ticket: Any, result: AgentResult, action: str) -> Optional[str]:
        """The standard reply to post instead of the model's when REPLY_SOURCE=template."""
        return None

    def finding_lines(self, result: AgentResult) -> list[str]:
        """What the lookups found, for the notes (masked; the caller unmasks)."""
        return []

    def next_step(self, ticket: Any, result: AgentResult) -> str:
        return "Next step: review the findings above and follow up with the branch."

    def found_line(self, ticket: Any) -> Optional[str]:
        """For a ticket found under another topic: why the desk picked it up."""
        return None

    def record_fields(self, ticket: Any, result: AgentResult) -> dict[str, Any]:
        """Desk-specific columns for the decision log (facts, account_statuses, template_reply)."""
        return {}

    def closed_tickets(self, since: date) -> list:
        raise NotImplementedError

    def toolbox_as_of(self, day: Optional[date]) -> Any:
        return day

    def audit_columns(self, ticket: Any, result: AgentResult) -> dict[str, Any]:
        return {}

    # ---- notes -------------------------------------------------------------------------------
    def escalation_note(self, ticket: Any, result: AgentResult, guard: GuardrailResult,
                        context: Optional[str] = None) -> str:
        """Internal note for the person picking up the ticket. Starts with the desk's marker."""
        p = guard.proposed
        lines = [self.escalation_marker, f"Analysed by the {self.agent_name}."]
        found = None if getattr(ticket, "topic_match", True) else self.found_line(ticket)
        if found:
            lines.append(found)
        if guard.changed:
            lines.append(f"The agent proposed '{p.action}', but its safety rules stopped it: {'; '.join(guard.notes)}")
        lines += ["", "Findings:", p.reason, ""]
        if p.evidence:
            lines += [f"- {item}" for item in p.evidence] + [""]
        lines += self.finding_lines(result)
        if p.reply_text:
            lines += ["Draft reply (not sent):", p.reply_text, ""]
        if context:
            lines += [f"{self.incident_label}: {context}", ""]
        lines.append(self.next_step(ticket, result))
        return result.pseudo.unmask("\n".join(lines).strip())

    def recommendation_note(self, result: AgentResult, guard: GuardrailResult, posted_text: Optional[str],
                            ticket: Optional[Any] = None) -> str:
        """notes mode: what the agent would do, for staff to action."""
        p, f = guard.proposed, guard.final
        lines = [self.recommendation_marker, f"Recommended action: {f.action} (confidence {p.confidence:.0%})"]
        found = None if ticket is None or getattr(ticket, "topic_match", True) else self.found_line(ticket)
        if found:
            lines.append(found)
        if guard.changed:
            lines.append(f"Model proposed '{p.action}'; safety rules changed it: {'; '.join(guard.notes)}")
        if posted_text:
            lines += ["", "Reply it would post:", posted_text]
        lines += ["", "Reasoning:", p.reason, ""]
        if p.evidence:
            lines += [f"- {item}" for item in p.evidence] + [""]
        lines += self.finding_lines(result)
        return result.pseudo.unmask("\n".join(lines).strip())

    # ---- live / shadow pass ----------------------------------------------------------------
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
            if ticket_number:
                tickets = self.on_request(ticket_number.strip())
                force = True
            else:
                tickets = self.discover(summary)
            all_open = tickets
            tickets = tickets[: limit or self.s.max_tickets_per_run]
            threads = self.store.thread_ids(tickets)
            for ticket in tickets:
                item = {"ticket_id": ticket.ticket_id, "ticket_number": ticket.number,
                        "help_topic": ticket.help_topic or "(no topic)", "opened": ticket.created}
                origin = self.origin(ticket)
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
                        reason = self.precheck(ticket, summary.run_id)
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
                    item.update(state="skipped", detail=reason if origin is None else f"{reason} ({origin})",
                                decision_id=last["id"] if last else None)
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
                    item.update(state=action, detail=origin, decision_id=decision_id)
            for ticket in all_open[len(tickets):]:
                queue.append({"ticket_id": ticket.ticket_id, "ticket_number": ticket.number,
                              "help_topic": ticket.help_topic or "(no topic)", "opened": ticket.created,
                              "state": "not reached", "detail": "over MAX_TICKETS_PER_RUN"})
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

    # ---- replies waiting for approval -----------------------------------------------------
    def _approval_step(self, ticket: Any, run_id: str) -> Optional[tuple[str, Any]]:
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

    def _post_approved(self, ticket: Any, row: Any, run_id: str) -> Optional[tuple[str, int]]:
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
            reason=f"Approved in the console by {who}" + (" (edited)" if row["edited"] else "")
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

    def _note_rejected(self, ticket: Any, row: Any, run_id: str) -> Optional[tuple[str, int]]:
        who = row["decided_by"] or "a reviewer"
        pseudo = Pseudonymizer.from_export(token_map(row))
        draft = pseudo.unmask(row["reply_masked"] or "")
        note = "\n".join([self.escalation_marker,
                          f"The {self.agent_name} proposed a reply that {who} rejected in the agent console"
                          + (f": {row['comment']}" if row["comment"] else ".") + " Please handle this ticket.",
                          "", "Proposed reply (not sent):", draft])
        executed: list[str] = []
        error = None
        try:
            if self.writer.note(ticket, note, self.escalation_marker):
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

    # ---- skip rules ----------------------------------------------------------------------------
    def _skip_reason(self, ticket: Any, force: bool) -> Optional[str]:
        if ticket.thread_id is None:
            return "no thread"
        if force:
            return None
        staff = self.s.reply_staff_id
        if ticket.escalated_by_agent(staff):
            return "already escalated"
        if self.mode == "notes" and ticket.escalated_by_agent(staff, self.recommendation_marker):
            return "recommendation already posted"
        last = self.audit.last_for_ticket(ticket.ticket_id, self.mode)
        if not ticket.topic_match:
            other = self.other_topic_skip(ticket, last)
            if other:
                return other
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

    def _request_expired(self, ticket: Any) -> bool:
        """The newest reply is the agent's own request for information, unanswered for too long."""
        hours = self.s.request_info_timeout_hours
        if not hours or self.s.reply_staff_id is None:
            return False
        replies = [e for e in ticket.entries if e.type == "R"]
        if not replies or replies[-1].staff_id != self.s.reply_staff_id:
            return False
        posted = parse_timestamp(replies[-1].created)
        return posted is not None and (now_local() - posted).total_seconds() >= hours * 3600

    def _follow_up_expired_request(self, ticket: Any, run_id: str) -> Optional[tuple[str, int]]:
        """No answer to the agent's question: hand the ticket to a person (no model call)."""
        hours = self.s.request_info_timeout_hours
        decision = Decision(ESCALATE, 1.0, EXPIRED_CATEGORY,
                            f"The agent asked the ticket owner for information and has had no reply for over {hours:g} "
                            "hours. A person should follow up with the branch or close the ticket.")
        result = self.result_type(decision=decision)
        guard = GuardrailResult(decision, decision)
        executed, error = self._execute(ticket, result, guard, None)
        decision_id = self._record(self._base(ticket, run_id), ticket, result, guard, executed=executed, error=error)
        log.info("#%s: escalate (request for information unanswered for %gh)", ticket.number, hours)
        return ESCALATE, decision_id

    # ---- deciding ---------------------------------------------------------------------------------
    def _base(self, ticket: Any, run_id: str, mode: Optional[str] = None) -> dict[str, Any]:
        return {"run_id": run_id, "mode": mode or self.mode, "ticket_id": ticket.ticket_id,
                "ticket_number": ticket.number, "last_entry_id": ticket.last_entry_id,
                "last_message_id": ticket.last_message_id}

    def _process(self, ticket: Any, run_id: str) -> Optional[tuple[str, int]]:
        base = self._base(ticket, run_id)
        try:
            self.toolbox.as_of = None
            files = self._read_attachments(ticket)
            try:
                result = self.agent.decide(ticket)
            except AgentError as exc:
                self.audit.live_step("Claude unreachable")
                result = self.fallback(ticket, str(exc)[:200]) if self.s.model_fallback == "rules" else None
                if result is None:
                    raise
                log.warning("#%s: %s -> using the rules fallback", ticket.number, exc)
            fresh = self.store.state(ticket)
        except (AgentError, MetabaseError) as exc:
            log.error("#%s: %s", ticket.number, exc)
            self.audit.record(**base, error=str(exc)[:1000])
            return None

        guard = self.guard(ticket, result, fresh, live=True)
        posted = self._posted_text(ticket, result, guard)
        why = None
        if guard.final.action in REPLY_ACTIONS and not guard.deferred and posted:
            why = self.approval_reason(self.s.approval_mode, guard.final, self.standard_action(ticket, result))
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
        log.info("#%s: %s%s (confidence %.2f, %s)%s", ticket.number, f.action,
                 f" [proposed {p.action}: {'; '.join(guard.notes)}]" if guard.changed else "",
                 p.confidence, _statuses(result), f" executed={executed}" if executed else "")
        return f.action, decision_id

    def _queue_email(self, ticket: Any, decision_id: int, action: str, posted: str) -> None:
        """Tell the ticket owner: osTicket sends no alert for replies the agent writes directly."""
        assert self.notifier is not None
        skip = None
        try:
            name, email = self.store.owner_contact(ticket)
        except MetabaseError as exc:
            name, email, skip = None, None, f"owner email lookup failed: {exc}"[:300]
        self.notifier.enqueue(decision_id=decision_id, ticket_id=ticket.ticket_id, number=ticket.number, kind=action,
                              recipient=email, owner_name=name, reply=posted, skip_reason=skip)

    def _posted_text(self, ticket: Any, result: AgentResult, guard: GuardrailResult) -> Optional[str]:
        """The exact text that would be posted to the ticket owner (unmasked), or None."""
        final = guard.final
        if final.action in (REPLY_AND_CLOSE, REQUEST_INFO) and self.s.reply_source == "template":
            template = self.template_for(ticket, result, final.action)
            if template:
                return template
        if final.action in (REPLY_AND_CLOSE, REQUEST_INFO):
            return result.pseudo.unmask(final.reply_text)
        return None

    def _execute(self, ticket: Any, result: AgentResult, guard: GuardrailResult,
                 posted: Optional[str]) -> tuple[list[str], Optional[str]]:
        action = guard.final.action
        done: list[str] = []
        if self.mode == "shadow" or action == NO_ACTION:
            return done, None
        try:
            if self.mode == "notes":
                if self.writer.note(ticket, self.recommendation_note(result, guard, posted, ticket),
                                    self.recommendation_marker):
                    done.append("note")
            elif action == ESCALATE:
                if self.writer.note(ticket, self.escalation_note(ticket, result, guard, getattr(self.agent, "context", None)),
                                    self.escalation_marker):
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
                    self.writer.note(ticket, f"{self.escalation_marker}\nThe {self.agent_name} completed {done} but "
                                             f"then failed: {exc}. Please check and finish this ticket manually.")
                except Exception:  # noqa: BLE001
                    log.exception("#%s: could not post the failure note", ticket.number)
            return done, f"write failed: {exc}"[:1000]

    def _read_attachments(self, ticket: Any) -> Any:
        """Read what is attached to the ticket; the agent then sees it with the thread."""
        if self.reader is None:
            return None
        files = self.reader.read(ticket)
        if files.attachments:
            done = Counter(a.status for a in files.attachments)
            log.info("#%s: %d attachment(s): %s", ticket.number, len(files.attachments),
                     ", ".join(f"{n} {s}" for s, n in sorted(done.items())))
        return files

    def _record(self, base: dict[str, Any], ticket: Any, result: AgentResult, guard: GuardrailResult,
                files: Optional[Any] = None, **extra: Any) -> int:
        p, f = guard.proposed, guard.final
        has_files = files is not None and files.attachments
        return self.audit.record(
            **base,
            branch=getattr(ticket, "org_name", None),
            proposed_action=p.action,
            final_action=f.action,
            confidence=p.confidence,
            category=p.category,
            reason=p.reason,
            evidence=p.evidence,
            guardrail_notes=guard.notes,
            tools_used=getattr(result.evidence, "tools_used", None),
            deferred=int(guard.deferred),
            transcript=result.transcript,
            attachments=result.pseudo.mask_obj(files.log_rows()) if has_files else None,
            input_tokens=result.input_tokens + (files.input_tokens if files is not None else 0),
            output_tokens=result.output_tokens + (files.output_tokens if files is not None else 0),
            reply_text=p.reply_text,
            **self.record_fields(ticket, result),
            **extra,
        )

    # ---- closed-ticket replay: what would the agent have said, against what staff posted ---------
    def audit_closed(self, since: date, limit: Optional[int], window_today: bool, out_dir: Path) -> Path:
        run_id = self.audit.start_run("audit", self.s.model)
        tickets = self.closed_tickets(since)[: limit or None]
        threads = self.store.thread_ids(tickets)
        log.info("Audit: %d %s ticket(s) closed on/after %s", len(tickets), self.audit_scope, since)
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
            self.toolbox.as_of = self.toolbox_as_of(as_of)
            files = self._read_attachments(ticket)
            try:
                result = self.agent.decide(ticket, as_of=(as_of or today_local()).isoformat())
            except (AgentError, MetabaseError) as exc:
                log.error("#%s: %s", ticket.number, exc)
                errors += 1
                continue
            guard = self.guard(ticket, result, None, live=False)
            posted = self._posted_text(ticket, result, guard)
            template = self.template(ticket, result)
            decided += 1
            self._record(self._base(ticket, run_id, "audit"), ticket, result, guard, files,
                         actual_reply=result.pseudo.mask(actual) if actual else None)
            rows.append({
                "ticket_num": ticket.number,
                "closed_on": ticket.closed_on,
                "help_topic": ticket.help_topic,
                **self.audit_columns(ticket, result),
                "agent_action": guard.final.action,
                "agent_proposed": guard.proposed.action,
                "guardrail_notes": "; ".join(guard.notes),
                "agent_reply": posted or "",
                "template_reply": template or "",
                "actual_last_reply": actual or "",
                "agent_same_as_actual": same_text(posted, actual),
                "template_same_as_actual": same_text(template, actual),
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


def _statuses(result: AgentResult) -> str:
    statuses = getattr(result.evidence, "statuses", None)
    return f"statuses {statuses}" if statuses else "no statuses"


