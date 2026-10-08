"""Test doubles: a scripted model and a fake Metabase that answers the ported SQL."""
from __future__ import annotations

import base64
import html
import re
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sms_alerts.config import Settings  # noqa: E402
from agentcore.metabase import MetabaseError  # noqa: E402

TICKET_DB, STANDBY_DB, CUSTOMERS_DB, SOCKET_DB, DISPATCH_DB = 121, 2, 3, 4, 5
AGENT_STAFF = 525


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


def check(account: str = "<NUMBER_1>", alert_types: str = "debits", phone: Optional[str] = None, id_: str = "c") -> SimpleNamespace:
    args = {"account_number": account, "alert_types": alert_types}
    if phone:
        args["ticket_phone"] = phone
    return response(tool_block("check_sms_alert_account", args, id_))


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


class FakeMetabase:
    """Answers the SQL the agent sends, from in-memory data. Records every statement."""

    def __init__(self) -> None:
        self.sql_log: list[tuple[int, str]] = []
        self.writes: list[str] = []
        self.tickets: dict[int, dict[str, Any]] = {}
        self.entries: dict[int, list[dict[str, Any]]] = {}  # thread_id -> entries
        self.accounts: dict[str, dict[str, Any]] = {}  # actual account (14 digits) -> core data
        self.salaries: dict[str, str] = {}  # account -> cellphone
        self.socket_rows: list[dict[str, Any]] = []
        self.missing_tables: set[str] = set()
        self.fail_patterns: list[str] = []
        self.staff = {AGENT_STAFF: {"staff_id": AGENT_STAFF, "username": "smsagent", "firstname": "SMS", "lastname": "Agent"}}
        self.volume_rows: list[dict[str, Any]] = []   # osTicket tickets for the volume query
        self.sends: list[dict[str, Any]] = []         # transaction_activity rows for aggregates and samples
        self.dispatch_daily: dict[str, dict[str, int]] = {}
        self.cae_daily: dict[str, dict[str, int]] = {}
        self.next_entry_id = 1000
        self.files: dict[int, dict[str, Any]] = {}       # ost_file + content
        self.attachments: list[dict[str, Any]] = []      # ost_attachment rows
        self.chunk_size = 4096
        self.no_to_base64 = False
        self.pending_changes: list[dict[str, Any]] = []

    # databases
    def resolve(self, name: str) -> int:
        return {"OSTICKETS_NEW": TICKET_DB, "STANDBY DB": STANDBY_DB, "CUSTOMERS": CUSTOMERS_DB,
                "SOCKET SMS DB": SOCKET_DB, "SMS": DISPATCH_DB}[name]

    def try_resolve(self, name: str) -> Optional[int]:
        try:
            return self.resolve(name)
        except KeyError:
            return None

    # fixtures
    def add_ticket(self, ticket_id: int, number: str, form: dict[str, Any], messages: list[str],
                   topic: str = "SMS Alerts / Transactional", state: str = "open", closed: Optional[str] = None,
                   created: Optional[str] = None, subject: Optional[str] = None) -> None:
        created = created or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 09:00:00")
        self.tickets[ticket_id] = {"ticket_id": ticket_id, "ticket_num": number, "help_topic": topic,
                                   "created": created, "state": state, "closed": closed, "subject": subject,
                                   "status_id": 1 if state == "open" else 3, "staff_id": 0, "isanswered": 0,
                                   "thread_id": ticket_id + 50000, **form}
        self.entries[ticket_id + 50000] = []
        for body in messages:
            self.add_entry(ticket_id, "M", body)

    def add_entry(self, ticket_id: int, kind: str, body: str, staff_id: Optional[int] = None) -> int:
        self.next_entry_id += 1
        thread = self.tickets[ticket_id]["thread_id"]
        self.entries[thread].append({"id": self.next_entry_id, "type": kind, "staff_id": staff_id,
                                     "poster": "Branch" if kind == "M" else "SMS Agent",
                                     "body": f"<p>{html.escape(body)}</p>", "created": f"2026-10-07 10:{len(self.entries[thread]):02d}:00"})
        return self.next_entry_id

    def add_attachment(self, entry_id: int, name: str, data: bytes, mime: str = "image/png", inline: bool = False,
                       bk: str = "D", signature: Optional[str] = None, size: Optional[int] = None) -> int:
        file_id = len(self.files) + 1
        self.files[file_id] = {"name": name, "type": mime, "data": data, "bk": bk,
                               "signature": signature if signature is not None else f"sig{file_id}",
                               "size": len(data) if size is None else size}
        self.attachments.append({"attach_id": len(self.attachments) + 1, "entry_id": entry_id, "file_id": file_id,
                                 "inline": 1 if inline else 0, "name": name})
        return file_id

    def add_account(self, account: str, **data: Any) -> None:
        defaults = {"internal": "IA" + account[-6:], "currency": "USD", "sms_oper": "1",
                    "cob": [{"COBICADDR_MOB_NO": "0772123456", "COBICADDR_SMS_ALERT": "1"}],
                    "tel_res": "263772123456", "email": "client@example.com", "triggers": []}
        defaults.update(data)
        self.accounts[account] = defaults

    @staticmethod
    def trigger(when: str, amount: str = "150.00", currency: str = "840", phone: str = "263772123456",
                account: str = "00012345678901", event: str = "DEBIT ALERT") -> dict[str, Any]:
        return {"UPTD_ON": when, "EVENT_CODE": "D1", "CMNAEC_EVENT_NAME": event, "IACLINK_ACTUAL_ACNUM": account,
                "TRAN_CURR_CODE": currency, "TRAN_AMOUNT": amount, "CLIENTS_CODE": "C1", "INDCLIENT_TEL_RES": phone}

    @staticmethod
    def socket_row(when: str, status: str = "DELIVERED_SUCCESSFULLY", phone: str = "263772123456",
                   account_suffix: str = "5678901", amount: str = "150.00", currency: str = "USD") -> dict[str, Any]:
        return {"id": 1, "time_stamp": when, "service_provider_name": "ECONET", "external_status": status,
                "transaction_reference": "REF1",
                "logged_responses": f"to={phone} msg=Account ****{account_suffix} debited {currency} {amount}"}

    # API
    def query(self, database_id: int, sql: str) -> list[dict[str, Any]]:
        self.sql_log.append((database_id, sql))
        for pattern in self.fail_patterns:
            if pattern in sql:
                raise MetabaseError(f"simulated failure for {pattern}")
        if database_id == TICKET_DB:
            return self._ticket_query(sql)
        if database_id == STANDBY_DB:
            return self._standby_query(sql)
        if database_id == CUSTOMERS_DB:
            acct = re.search(r"account_number = '(\d+)'", sql).group(1)
            return [{"account_number": acct, "cellphone": self.salaries[acct]}] if acct in self.salaries else []
        if database_id == SOCKET_DB:
            return self._socket_query(sql)
        if database_id == DISPATCH_DB:
            m = re.search(r"created_at >= DATE '([\d-]+)'", sql)
            if "GROUP BY dispatch_status" in sql and m:
                return [{"dispatch_status": k, "n": v} for k, v in self.dispatch_daily.get(m.group(1), {}).items()]
            return []
        raise AssertionError(f"unknown database {database_id}")

    def write(self, database_id: int, sql: str) -> None:
        assert database_id == TICKET_DB
        self.writes.append(sql)
        if sql.startswith("INSERT INTO ost_thread_entry"):
            m = re.search(r"VALUES \(\s*0, (\d+), (\d+), 0, '(\w)', 0, '((?:[^']|'')*)', '',\s*NULL, '((?:[^']|'')*)'", sql)
            thread, staff, kind, poster, body = m.groups()
            self.next_entry_id += 1
            self.entries[int(thread)].append({"id": self.next_entry_id, "type": kind, "staff_id": int(staff),
                                              "poster": _unsql(poster), "body": _unsql(body), "created": "2026-10-07 12:00:00"})
        elif sql.startswith("UPDATE ost_ticket"):
            tid = int(re.search(r"WHERE ticket_id = (\d+)", sql).group(1))
            ticket = self.tickets[tid]
            status = re.search(r"status_id = (\d+)", sql)
            if status:
                ticket.update(status_id=int(status.group(1)), state="closed", closed="2026-10-07 12:00:00",
                              staff_id=int(re.search(r"staff_id = (\d+)", sql).group(1)))
            ticket["isanswered"] = 1
        else:
            raise AssertionError(f"unexpected write: {sql[:60]}")

    def thread_entries(self, ticket_id: int) -> list[dict[str, Any]]:
        return self.entries[self.tickets[ticket_id]["thread_id"]]

    # ---- ticket DB routing
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
            if "TO_BASE64" in sql:
                if self.no_to_base64:
                    raise MetabaseError("FUNCTION osticket.TO_BASE64 does not exist")
                b64 = base64.b64encode(chunk).decode()
                return [{"data": "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))}]  # as MySQL wraps it
            return [{"data": chunk.hex().upper()}]
        if "ost_form_entry" in sql:
            state = "open" if "s.state = 'open'" in sql else "closed"
            return [self._ticket_row(t) for t in sorted(self.tickets.values(), key=lambda t: t["ticket_num"], reverse=True)
                    if t["state"] == state]
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
        if "AS last_message_id" in sql:
            ids = [int(x) for x in re.search(r"object_id IN \(([\d, ]+)\)", sql).group(1).split(",")]
            rows = []
            for tid in ids:
                own = [e["id"] for e in self.thread_entries(tid) if e["type"] == "M"]
                if own:
                    rows.append({"ticket_id": tid, "last_message_id": max(own)})
            return rows
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
        if "t.created >=" in sql and "ost_form_entry" not in sql:
            since = re.search(r"t\.created >= '([^']+)'", sql).group(1)
            return [r for r in self.volume_rows if r["created"] >= since]
        if "JOIN ost_user u" in sql and "ost_form_entry" not in sql:
            t = self.tickets[int(re.search(r"ticket_id = (\d+)", sql).group(1))]
            return [{"user_name": t.get("owner_name", "Tendai Moyo"), "email": t.get("owner_email", "t.moyo@branch.example")}]
        if sql.startswith("SELECT status_id, staff_id, closed FROM ost_ticket"):
            t = self.tickets[int(re.search(r"ticket_id = (\d+)", sql).group(1))]
            return [{"status_id": t["status_id"], "staff_id": t["staff_id"], "closed": t["closed"]}]
        if "AS escalation_notes" in sql:
            ids = [int(x) for x in re.search(r"IN \(([\d, ]+)\)", sql).group(1).split(",")]
            rows = []
            for tid in ids:
                t = self.tickets[tid]
                entries = self.thread_entries(tid)
                replies = [e["body"] for e in entries if e["type"] == "R"]
                notes = [e for e in entries if e["type"] == "N" and "did not auto-close" in e["body"]]
                rows.append({"ticket_id": tid, "ticket_num": t["ticket_num"], "status_state": t["state"],
                             "closed": t["closed"], "last_reply": replies[-1] if replies else None,
                             "escalation_notes": len(notes)})
            return rows
        raise AssertionError(f"unrouted ticket SQL: {sql[:80]}")

    def _ticket_row(self, t: dict[str, Any]) -> dict[str, Any]:
        keys = ("ticket_id", "ticket_num", "created", "help_topic", "phone_number", "cif", "account_number",
                "account_number_2", "currency", "type", "subject")
        row = {k: t.get(k) for k in keys}
        row["closed_on"] = t.get("closed")
        return row

    # ---- core banking routing
    def _by_internal(self, sql: str) -> Optional[dict[str, Any]]:
        internal = re.search(r"ACNTS_INTERNAL_ACNUM = '(\w+)'", sql).group(1)
        return next((a for a in self.accounts.values() if a["internal"] == internal), None)

    def _standby_query(self, sql: str) -> list[dict[str, Any]]:
        if "LEFT JOIN NMBPRODIDC.EVTSLOG" in sql:  # account activity: every transaction, alert or not
            acct = re.search(r"IACLINK_ACTUAL_ACNUM = '(\d+)'", sql).group(1)
            start, end = (datetime.strptime(d, "%d-%m-%Y").date() for d in re.findall(r"TO_DATE\('([\d-]+)'", sql))
            a = self.accounts.get(acct) or {}
            rows = [{**r, "TRAN_DATE_OF_TRAN": r["UPTD_ON"][:10]} for r in a.get("triggers", [])]
            rows += [{"TRAN_DATE_OF_TRAN": d, "TRAN_CURR_CODE": "840", "TRAN_AMOUNT": amt, "UPTD_ON": None,
                      "CMNAEC_EVENT_NAME": None, "IACLINK_ACTUAL_ACNUM": acct, "INDCLIENT_TEL_RES": None}
                     for d, amt in a.get("plain_txns", [])]
            return sorted([r for r in rows if start <= date.fromisoformat(r["TRAN_DATE_OF_TRAN"][:10]) <= end],
                          key=lambda r: r["TRAN_DATE_OF_TRAN"], reverse=True)
        if "SELECT ACNTS_CLIENT_NUM" in sql:
            a = self._by_internal(sql)
            return [{"ACNTS_CLIENT_NUM": a.get("client", "4410021")}] if a else []
        if "FROM DEMO_PENDING_CHANGES" in sql:
            client = re.search(r"CLIENT_NUM = '(\w*)'", sql).group(1)
            return [r for r in self.pending_changes if r["CLIENT_NUM"] == client]
        if "FROM NMBPRODIDC.IACLINK\n" in sql:
            acct = re.search(r"IACLINK_ACTUAL_ACNUM = '(\d+)'", sql).group(1)
            a = self.accounts.get(acct)
            return [{"IACLINK_INTERNAL_ACNUM": a["internal"]}] if a else []
        if "SELECT ACNTS_SMS_OPERN" in sql:
            a = self._by_internal(sql)
            return [{"ACNTS_SMS_OPERN": a["sms_oper"]}] if a else []
        if "SELECT ACNTS_CURR_CODE" in sql:
            a = self._by_internal(sql)
            return [{"ACNTS_CURR_CODE": a["currency"]}] if a else []
        if "COBICADDR_MOB_NO, A.COBICADDR_SMS_ALERT" in sql:
            a = self._by_internal(sql)
            return list(a["cob"]) if a else []
        if "SELECT C.INDCLIENT_TEL_RES" in sql:
            a = self._by_internal(sql)
            return [{"INDCLIENT_TEL_RES": a["tel_res"]}] if a else []
        if "SELECT C.INDCLIENT_EMAIL_ADDR1" in sql:
            a = self._by_internal(sql)
            return [{"INDCLIENT_EMAIL_ADDR1": a["email"]}] if a else []
        if "K.UPTD_ON" in sql:
            table = re.search(r"FROM (\S+) S\n", sql).group(1)
            if table in self.missing_tables:
                raise MetabaseError(f"ORA-00942: table or view does not exist ({table})")
            acct = re.search(r"IACLINK_ACTUAL_ACNUM = '(\d+)'", sql).group(1)
            start, end = (datetime.strptime(d, "%d-%m-%Y").date() for d in re.findall(r"TO_DATE\('([\d-]+)'", sql))
            year = int(table[-4:])
            a = self.accounts.get(acct)
            rows = [r for r in (a["triggers"] if a else [])
                    if start <= date.fromisoformat(r["UPTD_ON"][:10]) <= end and int(r["UPTD_ON"][:4]) == year]
            return sorted(rows, key=lambda r: r["UPTD_ON"], reverse=True)
        if "GROUP BY REQ_STATUS" in sql:
            day = re.search(r"CREATED_ON_DT = DATE '([\d-]+)'", sql).group(1)
            return [{"REQ_STATUS": k, "N": v} for k, v in self.cae_daily.get(day, {}).items()]
        if "NMBPRODCAE" in sql or "COBICADDR a" in sql:
            return []
        raise AssertionError(f"unrouted standby SQL: {sql[:80]}")

    def _socket_query(self, sql: str) -> list[dict[str, Any]]:
        if "COUNT(*) AS n" in sql or "LEFT(logged_responses" in sql:
            return self._socket_aggregate(sql)
        likes = re.findall(r"logged_responses LIKE '%(\d+)%'", sql)
        rows = self.socket_rows
        if likes:
            rows = [r for r in rows if any(x in r["logged_responses"] for x in likes)]
        return list(rows)


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _socket_aggregate(self, sql: str) -> list[dict[str, Any]]:
    lo = _ts(re.search(r"time_stamp >= TIMESTAMPTZ '([^']+)'", sql).group(1))
    hi_match = re.search(r"time_stamp < TIMESTAMPTZ '([^']+)'", sql)
    hi = _ts(hi_match.group(1)) if hi_match else None
    rows = [r for r in self.sends if _ts(r["time_stamp"]) >= lo and (hi is None or _ts(r["time_stamp"]) < hi)]
    status_in = re.search(r"external_status IN \(([^)]*)\)", sql)
    status_eq = re.search(r"external_status = '([A-Z_]+)'", sql)
    if status_in:
        allowed = re.findall(r"'([A-Z_]+)'", status_in.group(1))
        rows = [r for r in rows if r["external_status"] in allowed]
    if status_eq:
        rows = [r for r in rows if r["external_status"] == status_eq.group(1)]
    if "LEFT(logged_responses" in sql:
        limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
        return sorted(rows, key=lambda r: r["time_stamp"], reverse=True)[:limit]
    from collections import Counter as _C
    by_hour = "date_trunc('hour'" in sql
    counts = _C()
    for r in rows:
        hour = _ts(r["time_stamp"]).replace(minute=0, second=0).isoformat() if by_hour else None
        counts[(hour, r["service_provider_name"], r["external_status"])] += 1
    out = []
    for (hour, provider, status), n in counts.items():
        row = {"provider": provider, "external_status": status, "n": n}
        if by_hour:
            row["hour"] = hour
        out.append(row)
    return out


FakeMetabase._socket_aggregate = _socket_aggregate


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
        pipeline_trace_enabled=True, kill_switch_file=tmp_path / "STOP", state_db=tmp_path / "sms_alerts.sqlite",
        reply_staff_id=AGENT_STAFF, close_status_id=3, metabase_host="http://mb.test", metabase_api_key="k",
        socket_check_enabled=True, salaries_require_delivery_proof=False, recheck_after_hours=24.0,
        no_close_statuses=frozenset({"invalid_account_number", "invalid_ticket_phone"}), max_info_requests=2,
        tran_table_pattern="NMBPRODIDC.TRAN{year}", lookback_days=15,
    )
