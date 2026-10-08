"""Test doubles: a scripted model and a fake Metabase that answers the EcoCash agent's SQL.

All customer data here is invented."""
from __future__ import annotations

import base64
import html
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import Settings  # noqa: E402
from agent.metabase import MetabaseError  # noqa: E402
from agent.normalize import LOCAL_ZONE, account_key, id_key, mobile_key, now_local  # noqa: E402

TICKET_DB, ECOCASH_DB, POSTILION_DB, STANDBY_DB = 121, 7, 8, 2
AGENT_STAFF = 525
MOBILE, ACCOUNT, NATIONAL_ID = "263771234567", "86000013101001", "587654321F09"
MESSAGES = {
    "already_linked": ("20503", "During bank account creation, the entered bank account already exists in the identity."),
    "kyc_mismatch": ("BCS50646", "KYC verification failed."),
    "number_not_ecocash": ("TP40288", "The specified number does not exist"),
    "wallet_closed": ("TP40050", "The 263771234567 - TENDAI MOYO receiver is in the Closed state which does not support the current service."),
    "ecocash_system_error": ("TP40024", "Internal System error. Please try later."),
    "account_rejected": ("TC30174", "The value 778***367 of the AccountNumber parameter is incorrect, failed to authenticate the parameter."),
    "unknown": ("XX99999", "Something new went wrong."),
}


# ---- model ----------------------------------------------------------------------
def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict[str, Any], id_: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def response(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def decision(action: str, reply: str = "", confidence: float = 0.95, category: str = "x", id_: str = "d") -> SimpleNamespace:
    return response(tool_block("submit_decision", {
        "action": action, "confidence": confidence, "category": category,
        "reason": f"Test reason for {action}.", "evidence": ["test evidence"], "reply_text": reply}, id_))


def find(id_: str = "f", **args: str) -> SimpleNamespace:
    return response(tool_block("find_ecocash_attempts", args or {"mobile": "<MOBILE_1>"}, id_))


class ScriptedModel:
    """Stands in for anthropic.Anthropic(); returns queued responses in order."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self.queue = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        if not self.queue:
            raise AssertionError("ScriptedModel ran out of responses")
        return self.queue.pop(0)


# ---- Metabase -----------------------------------------------------------------
def _unsql(value: str) -> str:
    return value.replace("''", "'").replace("\\\\", "\\")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=LOCAL_ZONE)


class FakeMetabase:
    """Answers the SQL the agent sends, from in-memory data. Records every statement."""

    def __init__(self) -> None:
        self.sql_log: list[tuple[int, str]] = []
        self.writes: list[str] = []
        self.tickets: dict[int, dict[str, Any]] = {}
        self.entries: dict[int, list[dict[str, Any]]] = {}  # thread_id -> entries
        self.attempts: list[dict[str, Any]] = []
        self.legacy: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []  # Postilion eftc_ecocash
        self.accounts: dict[str, dict[str, Any]] = {}  # core banking, by 14-digit account
        self.files: dict[int, dict[str, Any]] = {}
        self.attachments: list[dict[str, Any]] = []
        self.chunk_size = 4096
        self.fail_patterns: list[str] = []
        self.staff = {AGENT_STAFF: {"staff_id": AGENT_STAFF, "username": "ecoagent", "firstname": "EcoCash", "lastname": "Agent"}}
        self.next_entry_id = 1000
        self.closed: list[str] = []

    # databases
    def resolve(self, name: str) -> int:
        return {"OSTICKETS_NEW": TICKET_DB, "ecocash_monitor": ECOCASH_DB, "POSTILION": POSTILION_DB,
                "STANDBY DB": STANDBY_DB}[name]

    def try_resolve(self, name: str) -> Optional[int]:
        try:
            return self.resolve(name)
        except KeyError:
            return None

    def close(self) -> None:
        pass

    # fixtures
    def add_ticket(self, ticket_id: int, number: str, messages: list[str], *, topic: str = "EcoCash",
                   subject: str = "EcoCash link failing", fields: Optional[dict[str, str]] = None,
                   created: Optional[datetime] = None, state: str = "open", org: str = "Harare Main") -> None:
        created = created or now_local() - timedelta(hours=3)
        self.tickets[ticket_id] = {"ticket_id": ticket_id, "ticket_num": number, "help_topic": topic, "subject": subject,
                                   "created": created.strftime("%Y-%m-%d %H:%M:%S"), "state": state,
                                   "closed": None if state == "open" else created.strftime("%Y-%m-%d %H:%M:%S"),
                                   "status_id": 1 if state == "open" else 3, "staff_id": 0,
                                   "thread_id": ticket_id + 50000, "fields": fields or {}, "org_name": org}
        self.entries[ticket_id + 50000] = []
        for body in messages:
            self.add_entry(ticket_id, "M", body)

    def add_entry(self, ticket_id: int, kind: str, body: str, staff_id: Optional[int] = None,
                  created: Optional[datetime] = None) -> int:
        self.next_entry_id += 1
        thread = self.tickets[ticket_id]["thread_id"]
        created = created or now_local() - timedelta(hours=2, minutes=60 - len(self.entries[thread]))
        self.entries[thread].append({"id": self.next_entry_id, "type": kind, "staff_id": staff_id,
                                     "poster": "Branch" if kind == "M" else "EcoCash Agent",
                                     "body": f"<p>{html.escape(body)}</p>", "created": created.strftime("%Y-%m-%d %H:%M:%S")})
        return self.next_entry_id

    def add_attempt(self, kind: str, when: Optional[datetime] = None, *, mobile: str = MOBILE, account: str = ACCOUNT,
                    id_number: str = NATIONAL_ID, operation: str = "LINK", via: str = "USSD",
                    first: str = "TENDAI", last: str = "MOYO") -> dict[str, Any]:
        when = when or now_local() - timedelta(hours=5)
        if kind in ("linked", "unlinked"):
            outcome, code, message = kind, "00", "Bank account linked successfully" if kind == "linked" else "Bank account unlinked"
        elif kind == "no_response":
            outcome, code, message = "no_response", None, "no response was logged within the timeout"
        else:
            outcome, (code, message) = "failed", MESSAGES[kind]
        row = {"id": len(self.attempts) + 1, "operation": operation, "outcome": outcome, "response_code": code,
               "message": message, "via": via, "channel": "IAPPROVE" if via == "IAPPROVE" else "NMBCONNECT",
               "mobile_number": mobile, "account_number": account, "id_number": id_number, "currency": "USD",
               "first_name": first, "last_name": last, "ecocash_name": f"{first} {last}" if kind == "linked" else None,
               "requested_at": when.strftime("%Y-%m-%d %H:%M:%S")}
        self.attempts.append(row)
        return row

    def add_file(self, entry_id: int, name: str, data: bytes, mime: str = "image/png", inline: bool = False) -> int:
        file_id = len(self.files) + 1
        self.files[file_id] = {"name": name, "type": mime, "data": data, "bk": "D", "signature": f"sig{file_id}",
                               "size": len(data)}
        self.attachments.append({"attach_id": len(self.attachments) + 1, "entry_id": entry_id, "file_id": file_id,
                                 "inline": 1 if inline else 0, "name": name})
        return file_id

    def thread_entries(self, ticket_id: int) -> list[dict[str, Any]]:
        return self.entries[self.tickets[ticket_id]["thread_id"]]

    # ---- the API ----------------------------------------------------------------
    def query(self, database_id: int, sql: str) -> list[dict[str, Any]]:
        self.sql_log.append((database_id, sql))
        for pattern in self.fail_patterns:
            if pattern in sql:
                raise MetabaseError(f"simulated failure for {pattern}")
        if database_id == TICKET_DB:
            return self._ticket_query(sql)
        if database_id == ECOCASH_DB:
            return self._ecocash_query(sql)
        if database_id == POSTILION_DB:
            return self._postilion_query(sql)
        if database_id == STANDBY_DB:
            return self._standby_query(sql)
        raise AssertionError(f"unknown database {database_id}")

    def write(self, database_id: int, sql: str) -> None:
        assert database_id == TICKET_DB
        self.writes.append(sql)
        if sql.startswith("INSERT INTO ost_thread_entry"):
            m = re.search(r"VALUES \(\s*0, (\d+), (\d+), 0, '(\w)', 0, '((?:[^']|'')*)', '',\s*NULL, '((?:[^']|'')*)'", sql)
            thread, staff, kind, poster, body = m.groups()
            self.next_entry_id += 1
            self.entries[int(thread)].append({"id": self.next_entry_id, "type": kind, "staff_id": int(staff),
                                              "poster": _unsql(poster), "body": _unsql(body),
                                              "created": now_local().strftime("%Y-%m-%d %H:%M:%S")})
        elif sql.startswith("UPDATE ost_ticket"):
            tid = int(re.search(r"WHERE ticket_id = (\d+)", sql).group(1))
            ticket = self.tickets[tid]
            status = re.search(r"status_id = (\d+)", sql)
            if status:
                ticket.update(status_id=int(status.group(1)), state="closed", closed=now_local().strftime("%Y-%m-%d %H:%M:%S"),
                              staff_id=int(re.search(r"staff_id = (\d+)", sql).group(1)))
                self.closed.append(ticket["ticket_num"])
        else:
            raise AssertionError(f"unexpected write: {sql[:60]}")

    # ---- osTicket ---------------------------------------------------------------
    @staticmethod
    def _topic_match(topic: str, topics: list[str]) -> bool:
        topic = (topic or "").upper()
        return any(topic == t or topic.startswith(t + " / ") for t in topics)

    def _ticket_query(self, sql: str) -> list[dict[str, Any]]:
        if "FROM ost_attachment a" in sql:
            ids = {int(x) for x in re.search(r"object_id IN \(([\d, ]+)\)", sql).group(1).split(",")}
            rows = []
            for a in self.attachments:
                if a["entry_id"] in ids:
                    f = self.files[a["file_id"]]
                    rows.append({"attach_id": a["attach_id"], "entry_id": a["entry_id"], "inline": a["inline"],
                                 "name": a["name"], "file_id": a["file_id"], "mime": f["type"], "size": f["size"],
                                 "bk": f["bk"], "signature": f["signature"]})
            return rows
        if "FROM ost_file_chunk" in sql:
            f = self.files[int(re.search(r"file_id = (\d+)", sql).group(1))]
            chunks = [f["data"][i:i + self.chunk_size] for i in range(0, len(f["data"]), self.chunk_size)]
            if "LENGTH(filedata)" in sql:
                return [{"chunk_id": i, "n": len(c)} for i, c in enumerate(chunks)]
            chunk = chunks[int(re.search(r"chunk_id = (\d+)", sql).group(1))]
            return [{"data": base64.b64encode(chunk).decode()}]
        if "FROM ost_form_entry e" in sql:
            ids = {int(x) for x in re.search(r"object_id IN \(([\d, ]+)\)", sql).group(1).split(",")}
            return [{"ticket_id": t["ticket_id"], "label": k, "name": k.lower(), "value": v}
                    for t in self.tickets.values() if t["ticket_id"] in ids for k, v in t["fields"].items()]
        if "CASE WHEN" in sql and "AS topic_match" in sql:
            state = "closed" if "s.state = 'closed'" in sql else "open"
            topics = re.findall(r"\) = '([^']+)' OR UPPER", sql.split("AS topic_match")[0])
            excluded = re.findall(r"\) = '([^']+)' OR UPPER", sql.split("AND NOT")[1]) if "AND NOT" in sql else []
            keywords = re.findall(r"LOWER\(cd\.subject\) LIKE '%([^%]+)%'", sql)
            since = re.search(r"t\.created >= '([^']+)'", sql)
            rows = []
            for t in sorted(self.tickets.values(), key=lambda x: x["ticket_num"], reverse=True):
                if t["state"] != state:
                    continue
                by_topic = self._topic_match(t["help_topic"], topics)
                text = (t["subject"] + " " + " ".join(e["body"] for e in self.thread_entries(t["ticket_id"])
                                                      if e["type"] == "M")).lower()
                by_content = bool(keywords) and any(k in text for k in keywords) and \
                    (since is None or t["created"] >= since.group(1))
                if not (by_topic or by_content) or self._topic_match(t["help_topic"], excluded):
                    continue
                rows.append({"ticket_id": t["ticket_id"], "ticket_num": t["ticket_num"], "created": t["created"],
                             "closed_on": t["closed"], "subject": t["subject"], "help_topic": t["help_topic"],
                             "org_name": t["org_name"], "topic_match": 1 if by_topic else 0})
            return rows
        if "object_type = 'C'" in sql:
            numbers = re.findall(r"'(\d+)'", sql.split("IN (")[1])
            return [{"ticket_id": t["ticket_id"], "ticket_num": t["ticket_num"], "thread_id": t["thread_id"], "object_type": "T"}
                    for t in self.tickets.values() if t["ticket_num"] in numbers]
        if sql.startswith("SELECT id, type, staff_id, poster, body, created"):
            thread = int(re.search(r"thread_id = (\d+)", sql).group(1))
            return [dict(e) for e in self.entries.get(thread, [])]
        if "s.state AS status_state, s.name AS status_name" in sql:
            tid = int(re.search(r"ticket_id = (\d+)", sql).group(1))
            t = self.tickets.get(tid)
            return [] if t is None else [{"ticket_id": tid, "ticket_num": t["ticket_num"], "status_state": t["state"],
                                          "status_name": t["state"].title(), "help_topic": t["help_topic"]}]
        if "MAX(id) AS last_id" in sql:
            thread = int(re.search(r"thread_id = (\d+)", sql).group(1))
            ids = [e["id"] for e in self.entries.get(thread, [])]
            return [{"last_id": max(ids) if ids else None}]
        if "FROM ost_staff" in sql:
            sid = int(re.search(r"staff_id = (\d+)", sql).group(1))
            return [self.staff[sid]] if sid in self.staff else []
        if sql.startswith("SELECT id FROM ost_thread_entry"):
            thread = int(re.search(r"thread_id = (\d+)", sql).group(1))
            staff = int(re.search(r"staff_id = (\d+)", sql).group(1))
            kind = re.search(r"type = '(\w)'", sql).group(1)
            exact = re.search(r"AND body = '((?:[^']|'')*)'", sql)
            like = re.search(r"AND body LIKE '%((?:[^']|'')*)%'", sql)
            for e in self.entries.get(thread, []):
                if e["staff_id"] != staff or e["type"] != kind:
                    continue
                if exact and e["body"] == _unsql(exact.group(1)):
                    return [{"id": e["id"]}]
                if like and _unsql(like.group(1)) in e["body"]:
                    return [{"id": e["id"]}]
            return []
        if "JOIN ost_user u" in sql and "ue.address" in sql:
            return [{"user_name": "Tendai Branch", "email": "t.branch@branch.example"}]
        if sql.startswith("SELECT status_id, staff_id, closed FROM ost_ticket"):
            t = self.tickets[int(re.search(r"ticket_id = (\d+)", sql).group(1))]
            return [{"status_id": t["status_id"], "staff_id": t["staff_id"], "closed": t["closed"]}]
        if "SELECT t.ticket_id, t.created" in sql:  # anomaly watch: ticket volume
            since = re.search(r"t\.created >= '([^']+)'", sql).group(1)
            return [{"ticket_id": t["ticket_id"], "created": t["created"], "help_topic": t["help_topic"],
                     "org_name": t["org_name"]} for t in self.tickets.values()
                    if t["created"] >= since and t["help_topic"].upper().startswith("ECOCASH")]
        if "AS escalation_notes" in sql:
            ids = [int(x) for x in re.search(r"IN \(([\d, ]+)\)", sql).group(1).split(",")]
            return [{"ticket_id": tid, "ticket_num": self.tickets[tid]["ticket_num"], "status_state": self.tickets[tid]["state"],
                     "closed": self.tickets[tid]["closed"], "last_reply": None, "escalation_notes": 0} for tid in ids]
        raise AssertionError(f"unrouted ticket SQL: {sql[:90]}")

    # ---- ecocash_monitor -------------------------------------------------------------
    @staticmethod
    def _matches(row: dict[str, Any], sql: str) -> bool:
        m = re.search(r"RIGHT\(regexp_replace\(mobile_number, '\\D', '', 'g'\), 9\) = '(\d+)'", sql)
        a = re.search(r"ltrim\(regexp_replace\(account_number, '\\D', '', 'g'\), '0'\) = '(\d+)'", sql)
        i = re.search(r"upper\(regexp_replace\(id_number, '\[\^A-Za-z0-9\]', '', 'g'\)\) = '(\w+)'", sql)
        return bool((m and mobile_key(row["mobile_number"]) == m.group(1)) or
                    (a and account_key(row["account_number"]) == a.group(1)) or
                    (i and id_key(row["id_number"]) == i.group(1)))

    def _ecocash_query(self, sql: str) -> list[dict[str, Any]]:
        if "MIN(requested_at)" in sql:
            return [{"first_at": min((r["requested_at"] for r in self.attempts), default=None)}]
        stamps = re.findall(r"TIMESTAMP '([^']+)'", sql)
        if "FROM ecocash_link_attempts" in sql and "GROUP BY" in sql:
            start, end = _ts(stamps[0]), _ts(stamps[1])
            hourly = "'hour'" in sql
            out: dict[tuple, int] = {}
            from agent.ecocash import classify
            for r in self.attempts:
                at = _ts(r["requested_at"])
                if not start <= at < end:
                    continue
                bucket = at.replace(minute=0, second=0) if hourly else at.replace(hour=0, minute=0, second=0)
                kind = r["outcome"] if r["outcome"] in ("linked", "unlinked", "no_response") else \
                    classify(r["response_code"], r["message"]).key
                key = (bucket.strftime("%Y-%m-%d %H:%M:%S"), r["operation"], r["via"], kind)
                out[key] = out.get(key, 0) + 1
            return [{"bucket": k[0], "operation": k[1], "via": k[2], "kind": k[3], "n": n} for k, n in out.items()]
        if "FROM ecocash_link_attempts" in sql and "outcome IN" in sql or "outcome = 'linked'" in sql:
            start, end = _ts(stamps[0]), _ts(stamps[1])
            failed = "outcome IN" in sql
            return [r for r in self.attempts if start <= _ts(r["requested_at"]) < end and r["operation"] == "LINK"
                    and ((r["outcome"] in ("failed", "no_response")) if failed else r["outcome"] == "linked")]
        if "FROM ecocash_link_attempts" in sql:
            start, end = _ts(stamps[0]), _ts(stamps[1])
            rows = [r for r in self.attempts if self._matches(r, sql) and start <= _ts(r["requested_at"]) <= end]
            return sorted(rows, key=lambda r: (r["requested_at"], r["id"]), reverse=True)
        if "FROM failed_ecocash_registrations" in sql:
            start, end = _ts(stamps[0]), _ts(stamps[1])
            rows = [r for r in self.legacy if self._matches(r, sql) and start <= _ts(r["failure_timestamp"]) < end]
            return sorted(rows, key=lambda r: r["failure_timestamp"], reverse=True)
        raise AssertionError(f"unrouted ecocash SQL: {sql[:90]}")

    # ---- Postilion and core banking ---------------------------------------------------
    def _postilion_query(self, sql: str) -> list[dict[str, Any]]:
        if "TOP 1 date_added" in sql:
            return self.links[:1]
        m = re.search(r"msisdn LIKE '%(\d+)'", sql)
        a = re.search(r"account_id LIKE '%(\d+)'", sql)
        return [r for r in self.links if (m and str(r["msisdn"]).endswith(m.group(1)))
                or (a and str(r["account_id"]).endswith(a.group(1)))]

    def _standby_query(self, sql: str) -> list[dict[str, Any]]:
        if "FROM DEMO_PENDING_CHANGES" in sql:
            client = re.search(r"CLIENT_NUM = '(\w*)'", sql).group(1)
            return [r for r in getattr(self, "pending_changes", []) if r["CLIENT_NUM"] == client]
        if "FROM NMBPRODIDC.IACLINK WHERE IACLINK_ACTUAL_ACNUM" in sql:
            acct = re.search(r"= '(\d+)'", sql).group(1)
            return [{"IACLINK_INTERNAL_ACNUM": "IA" + acct[-6:]}] if acct in self.accounts else []
        internal = re.search(r"ACNTS_INTERNAL_ACNUM = '(\w+)'", sql)
        acct = next((a for a in self.accounts if internal and "IA" + a[-6:] == internal.group(1)), None)
        if acct is None:
            return []
        data = self.accounts[acct]
        if "ACNTS_CLIENT_NUM, ACNTS_CURR_CODE" in sql:
            return [{"ACNTS_CLIENT_NUM": data.get("client", "8600001"), "ACNTS_CURR_CODE": data.get("currency", "USD")}]
        if "INDCLIENT_TEL_RES" in sql:
            return [{"INDCLIENT_TEL_RES": data.get("mobile", MOBILE)}]
        if "ROWNUM" in sql:
            return [{"OK": 1}]
        raise AssertionError(f"unrouted standby SQL: {sql[:90]}")


@pytest.fixture
def mb() -> FakeMetabase:
    return FakeMetabase()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env(env_file=tmp_path / "missing.env")
    return replace(
        base,
        anthropic_api_key="test", model="test-model", mode="shadow", confidence_threshold=0.85,
        max_tickets_per_run=25, max_closes_per_run=20, allow_request_info=True, reply_source="model",
        kill_switch_file=tmp_path / "STOP", state_db=tmp_path / "agent.sqlite", reply_staff_id=AGENT_STAFF,
        close_status_id=3, metabase_host="http://mb.test", metabase_api_key="k", recheck_after_hours=24.0,
        max_info_requests=2, anomaly_enabled=False, attachment_mode="local", portal_users_file=tmp_path / "users.json",
        monitor_position_file=tmp_path / "position.json",
    )
