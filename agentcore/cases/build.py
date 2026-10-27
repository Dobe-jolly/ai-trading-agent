"""Building a desk's cases from its closed tickets (docs/retrieval.md, section 1). No model call, no writes outside the
case store.

osTicket is the source of truth: every ticket closed under the desk's topics in the window, once it has settled. The
decisions log adds what the agent knew. If the agent closed the ticket, the case is the agent's: its reason and its
reply. If a person resolved it, the case is theirs: their last reply and note. When the agent escalated first, its
reason is the case's "found", because it says what the lookups showed. Tags (log signatures, response codes, channel,
reading) come from the decision's facts and transcript, and from codes written in the staff's text.

A new ticket has its thread read once (masked as it is stored). On later builds, stored cases are only re-checked:
reopened, or the agent's decision reviewed as incorrect, and the case is suppressed.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from ..decision import REPLY_AND_CLOSE
from ..pii import TOKEN_RE
from .mask import CaseMasker
from .store import CaseDraft, CaseStore

log = logging.getLogger(__name__)

SETTLE_DAYS = 14             # a closed ticket becomes a case only after this long without a reopen
TEXT_CHARS = 600             # each of asked, found and resolution
MAX_SIGNATURES = 5
SIGNATURE_RE = re.compile(r'"signature"\s*:\s*"([0-9a-f]{12})"')
# Codes as staff write them: "response code 91", "rsp 05", "error code E102", "result code: 1001".
CODE_RE = re.compile(r"\b(?:response|rsp|error|err|result|decline|status)\s*(?:code)?\s*[:#=]?\s*([A-Z]{0,3}\d{2,4})\b",
                     re.I)
NAME_LABEL = re.compile(r"\bname\b", re.I)

LATEST_DECISION_SQL = """SELECT d.*, (SELECT r.verdict FROM reviews r WHERE r.decision_id = d.id ORDER BY r.id DESC LIMIT 1)
    AS review_verdict
FROM decisions d
WHERE d.ticket_id = ? AND d.deferred = 0 AND d.mode != 'audit'
  AND (d.error IS NULL OR (d.executed IS NOT NULL AND d.executed != '[]'))
ORDER BY d.id DESC LIMIT 1"""


class CaseBuilder:
    """osticket: the desk's OsTicket (closed_tickets, load_thread, reopen_states). db: the desk's state database."""

    def __init__(self, store: CaseStore, db: sqlite3.Connection, osticket: Any, agent_staff_id: Optional[int],
                 days: int = 365, settle_days: int = SETTLE_DAYS, today: Optional[date] = None) -> None:
        self.store = store
        self.db = db
        self.osticket = osticket
        self.agent_staff_id = agent_staff_id
        self.days = days
        self.settle_days = settle_days
        self.today = today or date.today()

    def build(self, full: bool = False) -> dict[str, int]:
        """Add new cases and re-check stored ones. full: read every thread again (after a masking change)."""
        counts = {"added": 0, "updated": 0, "suppressed": 0, "restored": 0, "skipped": 0, "not_settled": 0}
        settled_by = (self.today - timedelta(days=self.settle_days)).isoformat()
        tickets = self.osticket.closed_tickets(self.today - timedelta(days=self.days))
        known = self.store.keys()
        reopened = self._reopened([t.ticket_id for t in tickets])
        for ticket in tickets:
            closed_on = str(ticket.closed_on or "")[:10]
            if not closed_on or closed_on > settled_by:
                counts["not_settled"] += 1
                continue
            decision = self.db.execute(LATEST_DECISION_SQL, (ticket.ticket_id,)).fetchone()
            problem = problem_of(decision, ticket.ticket_id in reopened)
            key = f"ticket:{ticket.ticket_id}"
            if key in known and not full:
                change = self.store.mark_problem(key, problem)
                if change:
                    counts[change] += 1
                continue
            try:
                draft = self.draft(ticket, decision, closed_on, problem)
            except Exception:  # noqa: BLE001 - one unreadable ticket must not stop the build
                log.warning("#%s: case not built", ticket.number, exc_info=True)
                counts["skipped"] += 1
                continue
            outcome = self.store.save(draft) if draft is not None else "skipped"
            counts[outcome] += 1
        return counts

    def _reopened(self, ticket_ids: list[int]) -> set[int]:
        reader = getattr(self.osticket, "reopen_states", None)
        out: set[int] = set()
        for start in range(0, len(ticket_ids), 200) if reader else ():
            for ticket_id, state in reader(ticket_ids[start:start + 200]).items():
                if state.get("reopened") or str(state.get("status_state") or "").lower() == "open":
                    out.add(int(ticket_id))
        return out

    def draft(self, ticket: Any, decision: Optional[sqlite3.Row], closed_on: str,
              problem: Optional[str]) -> Optional[CaseDraft]:
        """The case for one ticket, masked, or None when it says nothing about a resolution."""
        self.osticket.load_thread(ticket)
        entries = list(ticket.entries)
        customers = [e.poster for e in entries if e.type == "M"]
        customers += [v for k, v in (getattr(ticket, "fields", None) or {}).items() if NAME_LABEL.search(str(k))]
        staff = [e.poster for e in entries if e.type in ("R", "N")]
        masker = CaseMasker(customers=customers, staff=staff)
        first = next((e.body for e in entries if e.type == "M"), "")
        asked = _cut(masker.mask("\n".join(p for p in (ticket.subject or "", first) if p)))
        if not asked:
            return None
        marker = getattr(ticket, "escalation_marker", None)
        if decision is not None and decision["review_verdict"] == "incorrect" and not closed_by_agent(decision):
            decision = None  # a person resolved it after a wrong call: their case stands, the agent's reading doesn't
        tags: set[tuple[str, str]] = set()
        if decision is not None:
            tags |= decision_tags(decision)
        if closed_by_agent(decision):
            source = "agent"
            found = decision["reason"] or ""
            resolution = decision["actual_reply"] or decision["reply_text"] or ""
            action, category = decision["final_action"], decision["category"]
        else:
            people = [e for e in entries if e.staff_id is not None and e.staff_id != self.agent_staff_id]
            replies = [e.body for e in people if e.type == "R"]
            notes = [e.body for e in people if e.type == "N" and not (marker and marker in e.body)]
            if not replies:
                return None  # closed without a word to anyone: nothing to learn from
            source = "staff"
            found = (decision["reason"] if decision is not None and decision["reason"] else (notes[-1] if notes else ""))
            resolution = replies[-1]
            action, category = "staff_resolved", (decision["category"] if decision is not None else None)
            tags.add(("action", "staff_resolved"))
        found, resolution = _cut(masker.mask(found)), _cut(masker.mask(resolution))
        tags |= {("code", c) for c in CODE_RE.findall(" ".join((asked, found, resolution)))}
        if any(TOKEN_RE.search(t) or re.search(r"\d{8,}", t) for t in (asked, found, resolution)):
            log.warning("#%s: identifier left after masking; case not built", ticket.number)
            return None
        return CaseDraft(source=source, ticket_id=int(ticket.ticket_id), source_ticket=str(ticket.number),
                         topic=ticket.help_topic or None, closed_on=closed_on, asked=asked, found=found,
                         resolution=resolution, action=action, category=category, tags=tags, problem=problem)


def closed_by_agent(decision: Optional[sqlite3.Row]) -> bool:
    return (decision is not None and decision["final_action"] == REPLY_AND_CLOSE
            and "close" in _json_list(decision["executed"]))


def problem_of(decision: Optional[sqlite3.Row], reopened: bool) -> Optional[str]:
    """Why a case must not be offered: the ticket was reopened, or the agent closed it and that was reviewed
    incorrect."""
    if reopened or (decision is not None and decision["reopened_at"]):
        return "ticket reopened"
    if closed_by_agent(decision) and decision["review_verdict"] == "incorrect":  # type: ignore[index]
        return "decision reviewed incorrect"
    return None


def decision_tags(decision: sqlite3.Row) -> set[tuple[str, str]]:
    """Tags from what the agent's lookups established: facts the desk recorded, and the log signatures of the
    client's own trail (log_search results; log_errors lists are service-wide and say nothing about this ticket)."""
    tags: set[tuple[str, str]] = set()
    facts = _json_obj(decision["facts"])
    confirmed = facts.get("confirmed") if isinstance(facts.get("confirmed"), dict) else {}
    latest = facts.get("latest") if isinstance(facts.get("latest"), dict) else {}
    for kind, value in (("channel", confirmed.get("channel")), ("reading", confirmed.get("status")),
                        ("code", confirmed.get("code")), ("channel", latest.get("via")),
                        ("reading", latest.get("status"))):
        if value:
            tags.add((kind, str(value)))
    if decision["final_action"]:
        tags.add(("action", decision["final_action"]))
    if decision["category"]:
        tags.add(("action", decision["category"]))
    for sig in log_signatures(decision["transcript"])[:MAX_SIGNATURES]:
        tags.add(("signature", sig))
    return tags


def log_signatures(transcript: Any) -> list[str]:
    """Signatures in log_search results, in the order they came back."""
    messages = transcript if isinstance(transcript, list) else _json_list(transcript)
    searches: set[str] = set()
    found: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "log_search":
                searches.add(str(block.get("id")))
            elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in searches:
                text = block.get("content") if isinstance(block.get("content"), str) else json.dumps(block.get("content"))
                found += [s for s in SIGNATURE_RE.findall(text) if s not in found]
    return found


def _cut(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS].rsplit(" ", 1)[0] + " ..."


def _json_obj(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return value if isinstance(value, list) else []
