"""Test doubles for the failed-transactions desk: the scripted model, and a fake Metabase that answers the desk's
SQL for osTicket, Postilion, CONECT 2.0, BILLERS, IB and core banking. All customer data here is invented."""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

from failed_txn.config import Settings
from failed_txn.normalize import account_key, now_local
from tests.ecocash.conftest import (  # noqa: F401 - the shared doubles, re-exported for these tests
    AGENT_STAFF,
    FakeMetabase as _TicketFake,
    ScriptedModel,
    decision,
    response,
    text_block,
    tool_block,
)

TICKET_DB, POSTOFFICE_DB, CONECT_DB, BILLERS_DB, IB_DB, STANDBY_DB = 121, 31, 32, 33, 34, 2
ACCOUNT = "86000013101001"
OTHER_ACCOUNT = "86000099999999"
RRN = "612345678901"


def call(name: str, args: dict[str, Any], id_: str = "c") -> Any:
    return response(tool_block(name, args, id_))


def decide(action: str, reply: str = "", category: str = "x", conf: float = 0.95, standard: Optional[bool] = None) -> Any:
    args = {"action": action, "confidence": conf, "category": category, "reason": f"Test reason for {action}.",
            "evidence": ["test evidence"], "reply_text": reply}
    if standard is not None:
        args["follows_standard_outcome"] = standard
    return response(tool_block("submit_decision", args, "d"))


def _between(sql: str) -> Optional[tuple[float, float]]:
    m = re.search(r"BETWEEN ([\d.]+) AND ([\d.]+)", sql)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _accounts(sql: str) -> set:
    found = set()
    for group in re.findall(r"IN \(([^)]*)\)", sql):
        found |= {account_key(x) for x in re.findall(r"'(\d+)'", group)}
    return found


class FakeMetabase(_TicketFake):
    def __init__(self) -> None:
        super().__init__()
        self.post_tran: list[dict[str, Any]] = []
        self.conect: dict[str, list[dict[str, Any]]] = {}
        self.billers: dict[str, list[dict[str, Any]]] = {}
        self.ib: dict[str, list[dict[str, Any]]] = {}
        self.core_accounts: dict[str, dict[str, Any]] = {ACCOUNT: {"currency": "USD"}}
        self.postings: list[dict[str, Any]] = []

    def resolve(self, name: str) -> int:
        return {"OSTICKETS_NEW": TICKET_DB, "POST OFFICE": POSTOFFICE_DB, "CONECT 2.0": CONECT_DB,
                "BILLERS": BILLERS_DB, "IB": IB_DB, "STANDBY DB": STANDBY_DB}[name]

    # ---- fixtures ------------------------------------------------------------------
    def add_ticket(self, ticket_id: int, number: str, messages: list[str], *, topic: str = "Failed ZIPIT Transaction",
                   subject: str = "Failed transaction", **kwargs: Any) -> None:
        super().add_ticket(ticket_id, number, messages, topic=topic, subject=subject, **kwargs)

    def add_switch(self, amount: float, *, rsp: str = "00", reversed_flag: str = "0", completed: int = 1,
                   when: Optional[datetime] = None, account: str = ACCOUNT, rrn: str = RRN, stan: str = "123456",
                   legs: int = 2, message_type: str = "0200", tran_type: str = "40", currency: str = "840") -> None:
        when = when or now_local() - timedelta(days=1, hours=2)
        for leg in range(legs):
            self.post_tran.append({
                "post_tran_id": len(self.post_tran) + 1, "message_type": message_type, "tran_type": tran_type,
                "tran_completed": completed, "rsp_code_rsp": rsp, "abort_rsp_code": None, "tran_reversed": reversed_flag,
                "prev_post_tran_id": None, "tran_amount_req": f"{int(round(amount * 100))}",
                "tran_currency_code": currency, "datetime_tran_local": when.strftime("%Y-%m-%d %H:%M:%S"),
                "retrieval_reference_nr": rrn, "system_trace_audit_nr": stan, "from_account_id": account.zfill(14),
                "to_account_id": "0000000000"})

    def add_connect(self, table: str, row: dict[str, Any], db: str = "conect") -> None:
        getattr(self, db).setdefault(table, []).append(row)

    def add_posting(self, amount: float, flag: str, narration: str, when: Optional[datetime] = None,
                    account: str = ACCOUNT, currency: str = "USD") -> None:
        when = when or now_local() - timedelta(days=1, hours=2)
        self.postings.append({"account": account, "TRAN_DATE_OF_TRAN": when.strftime("%Y-%m-%d 00:00:00"),
                              "TRAN_AMOUNT": f"{amount:.2f}", "TRAN_CURR_CODE": currency, "TRAN_DB_CR_FLG": flag,
                              "TRAN_NARR_DTL1": narration, "TRAN_BATCH_NUMBER": 77, "TRAN_BATCH_SL_NUM": 1})

    # ---- the API ---------------------------------------------------------------------
    def query(self, database_id: int, sql: str) -> list[dict[str, Any]]:
        if database_id in (POSTOFFICE_DB, CONECT_DB, BILLERS_DB, IB_DB) or (database_id == STANDBY_DB and "NMBPRODIDC" in sql):
            self.sql_log.append((database_id, sql))
            for pattern in self.fail_patterns:
                if pattern in sql:
                    from agentcore.metabase import MetabaseError
                    raise MetabaseError(f"simulated failure for {pattern}")
            if database_id == POSTOFFICE_DB:
                return self._post_tran(sql)
            if database_id in (CONECT_DB, BILLERS_DB):
                return self._connect(sql, self.conect if database_id == CONECT_DB else self.billers)
            if database_id == IB_DB:
                return self._ib(sql)
            return self._core(sql)
        return super().query(database_id, sql)

    def _ticket_query(self, sql: str) -> list[dict[str, Any]]:
        if "1 AS topic_match" in sql:
            state = "closed" if "s.state = 'closed'" in sql else "open"
            where = sql.split("WHERE", 1)[1]
            topics = re.findall(r"\) = '([^']+)' OR UPPER", where.split("AND NOT")[0])
            excluded = re.findall(r"\) = '([^']+)' OR UPPER", where.split("AND NOT")[1]) if "AND NOT" in where else []
            rows = []
            for t in sorted(self.tickets.values(), key=lambda x: x["ticket_num"], reverse=True):
                if t["state"] != state or not self._topic_match(t["help_topic"], topics) \
                        or self._topic_match(t["help_topic"], excluded):
                    continue
                rows.append({"ticket_id": t["ticket_id"], "ticket_num": t["ticket_num"], "created": t["created"],
                             "closed_on": t["closed"], "subject": t["subject"], "help_topic": t["help_topic"],
                             "org_name": t["org_name"], "topic_match": 1})
            return rows
        return super()._ticket_query(sql)

    @staticmethod
    def _in_window(value: str, sql: str, column: str) -> bool:
        lo = re.search(rf"{column} >= '([^']+)'", sql)
        hi = re.search(rf"{column} < '([^']+)'", sql)
        day = value[:10]
        return (lo is None or day >= lo.group(1)) and (hi is None or day < hi.group(1))

    def _post_tran(self, sql: str) -> list[dict[str, Any]]:
        rrn = re.search(r"retrieval_reference_nr = '(\w+)'", sql)
        accounts = _accounts(sql)
        band = _between(sql)
        rows = []
        for r in self.post_tran:
            if rrn and r["retrieval_reference_nr"] != rrn.group(1):
                continue
            if not rrn:
                if account_key(r["from_account_id"]) not in accounts and account_key(r["to_account_id"]) not in accounts:
                    continue
                if not self._in_window(r["datetime_tran_local"], sql, "datetime_tran_local"):
                    continue
                if r["tran_type"] in ("30", "31"):
                    continue
            if band and not band[0] <= float(r["tran_amount_req"]) <= band[1]:
                continue
            rows.append(dict(r))
        limit = int(re.search(r"TOP (\d+)", sql).group(1))
        return sorted(rows, key=lambda r: r["datetime_tran_local"], reverse=True)[:limit]

    def _connect(self, sql: str, tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        table = re.search(r"FROM (\w+)", sql).group(1)
        accounts = _accounts(sql)
        cols = re.findall(r"(\w+) IN \(", sql)
        kind = re.search(r"transaction_type = '(\w+)'", sql)
        date_col = re.search(r"AND (\w+) >= '", sql)
        rows = []
        for r in tables.get(table, []):
            if not any(account_key(r.get(c)) in accounts for c in cols):
                continue
            if kind and r.get("transaction_type") != kind.group(1):
                continue
            if date_col and not self._in_window(str(r.get(date_col.group(1))), sql, date_col.group(1)):
                continue
            rows.append(dict(r))
        return rows

    def _ib(self, sql: str) -> list[dict[str, Any]]:
        table = re.search(r"FROM (\w+)", sql).group(1)
        accounts = _accounts(sql)
        band = _between(sql)
        return [dict(r) for r in self.ib.get(table, [])
                if any(account_key(r.get(c)) in accounts for c in
                       ("account_identification1", "account_identification2", "beneficiary_account_number"))
                and self._in_window(r["date_created"], sql, "date_created")
                and (band is None or band[0] <= float(r["transaction_value"]) <= band[1])]

    def _core(self, sql: str) -> list[dict[str, Any]]:
        acct = account_key(re.search(r"IACLINK_ACTUAL_ACNUM = '(\d+)'", sql).group(1))
        if "ACNTS_CURR_CODE" in sql and "TRAN_DATE_OF_TRAN" not in sql:
            data = self.core_accounts.get(acct.zfill(14)) or self.core_accounts.get(acct)
            return [] if data is None else [{"IACLINK_INTERNAL_ACNUM": "IA" + acct[-6:], "ACNTS_CURR_CODE": data["currency"]}]
        year = re.search(r"TRAN(\d{4})", sql).group(1)
        start, end = re.findall(r"TO_DATE\('([\d-]+)'", sql)
        lo, hi = (datetime.strptime(x, "%d-%m-%Y").strftime("%Y-%m-%d") for x in (start, end))
        band = _between(sql)
        return [{k: v for k, v in p.items() if k != "account"} for p in self.postings
                if account_key(p["account"]) == acct and p["TRAN_DATE_OF_TRAN"][:4] == year
                and lo <= p["TRAN_DATE_OF_TRAN"][:10] <= hi
                and (band is None or band[0] <= float(p["TRAN_AMOUNT"]) <= band[1])]


@pytest.fixture
def mb() -> FakeMetabase:
    return FakeMetabase()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env(env_file=tmp_path / "missing.env")
    return replace(
        base, anthropic_api_key="test", model="test-model", mode="shadow", confidence_threshold=0.85,
        max_tickets_per_run=25, max_closes_per_run=20, allow_request_info=True, reply_source="model",
        kill_switch_file=tmp_path / "STOP", state_db=tmp_path / "failed_txn.sqlite", reply_staff_id=AGENT_STAFF,
        close_status_id=3, metabase_host="http://mb.test", metabase_api_key="k", recheck_after_hours=24.0,
        max_info_requests=2, attachment_mode="local", portal_users_file=tmp_path / "users.json",
    )
