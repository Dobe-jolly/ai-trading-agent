"""The help topics a ticket can be filed under: read from osTicket once per pass, with what each one is for and
which desk, if any, works it."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from agentcore import sql
from agentcore.metabase import MetabaseClient
from agentcore.osticket import fold_topic, html_to_text, topic_matches
from .normalize import now_local

log = logging.getLogger(__name__)

# osTicket 1.10+: ost_help_topic.flags bit 2 = active, bit 4 = archived
FLAG_ACTIVE, FLAG_ARCHIVED = 0x2, 0x4
TOPICS_SQL = "SELECT topic_id, topic_pid, topic, flags, notes FROM ost_help_topic"
VOLUME_SQL = """SELECT t.topic_id, COUNT(*) AS n
FROM ost_ticket t
WHERE t.created >= '{since}'
GROUP BY t.topic_id"""
VOLUME_DAYS = 90


@dataclass
class Topic:
    topic_id: int
    path: str            # as osTicket builds it (parents ' / ' name), stray spaces and all
    active: bool
    description: str = ""
    recent: int = 0      # tickets opened under it in the last VOLUME_DAYS days
    desk: Optional[str] = None  # the desk that works it, if any

    @property
    def name(self) -> str:
        """The name as people write it: single spaces, no trailing slash."""
        name = " ".join(self.path.split())
        while name.endswith("/"):
            name = name[:-1].rstrip()
        return name

    @property
    def key(self) -> str:
        return fold_topic(self.path)


def _flags(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def load_descriptions(path: Optional[Path]) -> dict[str, str]:
    """{folded topic name: what it is for} from the descriptions file (missing file: none)."""
    if path is None or not Path(path).exists():
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Topic descriptions %s not read: %s", path, exc)
        return {}
    return {fold_topic(k): str(v).strip() for k, v in raw.items() if not k.startswith("_") and str(v).strip()}


class Catalogue:
    """The active help topics, and the rules about where tickets may go."""

    def __init__(self, topics: Iterable[Topic], *, catch_alls: Iterable[str] = (), never_to: Iterable[str] = (),
                 move_to: Iterable[str] = ()) -> None:
        self.all = list(topics)
        self.catch_alls = [t for t in catch_alls if t.strip()]
        self.never_to = [t for t in never_to if t.strip()]
        self.move_to = [t for t in move_to if t.strip()]
        self.active = sorted((t for t in self.all if t.active), key=lambda t: (-t.recent, t.name.lower()))
        self._by_key = {t.key: t for t in self.active}

    # ---- looking a topic up ------------------------------------------------------------------
    def find(self, name: Optional[str]) -> Optional[Topic]:
        """The active topic with this name (case, spacing and a trailing slash do not matter)."""
        return self._by_key.get(fold_topic(name))

    def is_catch_all(self, name: Optional[str]) -> bool:
        return topic_matches(name, self.catch_alls)

    def is_never_to(self, name: Optional[str]) -> bool:
        return topic_matches(name, self.never_to)

    def may_move_to(self, topic: Topic) -> bool:
        """Moved without a person: a topic a desk works, or one listed in ROUTER_MOVE_TO."""
        return topic.desk is not None or topic_matches(topic.path, self.move_to)

    def targets(self) -> list[Topic]:
        """What the model may choose from: active topics that are neither catch-alls nor excluded."""
        return [t for t in self.active if not self.is_catch_all(t.path) and not self.is_never_to(t.path)]

    # ---- how the model sees them --------------------------------------------------------------
    def prompt_list(self) -> str:
        lines = []
        for t in self.targets():
            extra = []
            if t.desk:
                extra.append(f"worked by the {t.desk} agent")
            extra.append(f"{t.recent} ticket(s) in the last {VOLUME_DAYS} days")
            lines.append(f"- {t.name}" + (f" — {t.description}" if t.description else "") + f" ({'; '.join(extra)})")
        return "\n".join(lines) or "(no active help topics found)"


def load(mb: MetabaseClient, db: int, *, descriptions: Optional[dict[str, str]] = None,
         owners: Optional[list[tuple[str, list, list]]] = None, catch_alls: Iterable[str] = (),
         never_to: Iterable[str] = (), move_to: Iterable[str] = ()) -> Catalogue:
    """Read every help topic, build full names as osTicket does, and mark the ones a desk works.
    owners: (desk title, topics it works, subtopics it leaves alone) for each desk that is on."""
    rows = mb.query(db, TOPICS_SQL)
    by_id = {int(r["topic_id"]): r for r in rows}

    def path_of(row: dict[str, Any]) -> tuple[str, bool]:
        parts, active, seen = [], True, set()
        while row is not None and int(row["topic_id"]) not in seen:
            seen.add(int(row["topic_id"]))
            parts.append(str(row.get("topic") or ""))
            flags = _flags(row.get("flags"))
            active = active and bool(flags & FLAG_ACTIVE) and not flags & FLAG_ARCHIVED
            row = by_id.get(int(row.get("topic_pid") or 0))
        return " / ".join(reversed(parts)), active

    since = (now_local() - timedelta(days=VOLUME_DAYS)).strftime("%Y-%m-%d 00:00:00")
    volume = {int(r["topic_id"]): int(r.get("n") or 0)
              for r in mb.query(db, VOLUME_SQL.replace("{since}", sql.mysql_text(since))) if r.get("topic_id") is not None}
    descriptions = descriptions or {}
    topics = []
    for tid, row in by_id.items():
        path, active = path_of(row)
        topic = Topic(topic_id=tid, path=path, active=active, recent=volume.get(tid, 0))
        topic.description = descriptions.get(topic.key) or html_to_text(row.get("notes"))[:240]
        for title, works, leaves in owners or []:
            if topic_matches(path, works) and not topic_matches(path, leaves):
                topic.desk = title
                break
        topics.append(topic)
    return Catalogue(topics, catch_alls=catch_alls, never_to=never_to, move_to=move_to)
