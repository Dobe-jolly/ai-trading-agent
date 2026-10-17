"""Test doubles for the NMB Connect desk: the scripted model, and a fake Metabase that answers the desk's SQL for
osTicket, CONECT 2.0 REG (profiles), CONECT 2.0 (activity), core banking, the registration log analyzer and the SMS
gateway. All customer data here is invented."""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

from agentcore.metabase import MetabaseError
from nmb_connect.config import Settings
from nmb_connect.normalize import mobile_key, now_local
from tests.ecocash.conftest import (  # noqa: F401 - the shared doubles, re-exported for these tests
    AGENT_STAFF,
    FakeMetabase as _TicketFake,
    ScriptedModel,
    response,
    text_block,
    tool_block,
)

TICKET_DB, REG_DB, CONECT_DB, STANDBY_DB, FAILURES_DB, SOCKET_DB = 121, 128, 127, 81, 140, 85
CIF, OTHER_CIF = "8600013", "8600099"
ACCOUNT, ZIG_ACCOUNT = "86000013101001", "86000013102002"
MOBILE, NEW_MOBILE, OTHER_MOBILE = "0771234567", "0782345678", "0719998888"
NATIONAL_ID = "58-7654321 F 09"


def call(name: str, args: dict[str, Any], id_: str = "c") -> Any:
    return response(tool_block(name, args, id_))


def decide(action: str, reply: str = "", category: str = "x", conf: float = 0.95, standard: Optional[bool] = None) -> Any:
    args = {"action": action, "confidence": conf, "category": category, "reason": f"Test reason for {action}.",
            "evidence": ["test evidence"], "reply_text": reply}
    if standard is not None:
        args["follows_standard_outcome"] = standard
    return response(tool_block("submit_decision", args, "d"))


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S")


class FakeMetabase(_TicketFake):
    def __init__(self) -> None:
        super().__init__()
        self.profiles: list[dict[str, Any]] = []
        self.channels: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.activity: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.sms: list[dict[str, Any]] = []
        self.core: dict[str, dict[str, Any]] = {}     # 14-digit account -> {internal, cif, currency, ...flags}
        self.clients: dict[str, dict[str, Any]] = {}  # CIF -> {mobile, email, idccob}
        self.missing_tables: set = set()
        self.staff[AGENT_STAFF] = {"staff_id": AGENT_STAFF, "username": "connectagent", "firstname": "NMB Connect",
                                   "lastname": "Agent"}
        self.add_account(ACCOUNT, CIF, "USD")
        self.add_account(ZIG_ACCOUNT, CIF, "ZWG")
        self.clients[CIF] = {"mobile": MOBILE, "email": "client@example.com", "idccob": [MOBILE]}

    def resolve(self, name: str) -> int:
        return {"OSTICKETS_NEW": TICKET_DB, "CONECT 2.0 REG": REG_DB, "CONECT 2.0": CONECT_DB, "STANDBY DB": STANDBY_DB,
                "registration_failures": FAILURES_DB, "SOCKET SMS DB": SOCKET_DB}[name]

    # ---- fixtures ------------------------------------------------------------------
    def add_ticket(self, ticket_id: int, number: str, messages: list[str], *, topic: str = "NMB Connect / OTP",
                   subject: str = "NMB Connect", **kwargs: Any) -> None:
        super().add_ticket(ticket_id, number, messages, topic=topic, subject=subject, **kwargs)

    def add_profile(self, *, cif: str = CIF, mobile: str = MOBILE, created: Optional[datetime] = None,
                    status: str = "ACTIVE", **extra: Any) -> dict[str, Any]:
        row = {"id": len(self.profiles) + 1, "customer_number": cif, "mobile_number": mobile,
               "email": "client@example.com", "first_name": "TENDAI", "last_name": "MOYO",
               "password": "$2a$10$notarealhashnotarealhash", "pin": "9999", "username": "tmoyo77",
               "status": status, "created_at": _stamp(created or now_local() - timedelta(days=200)),
               "device_id": "abc-123", "login_attempts": 0, **extra}
        self.profiles.append(row)
        return row

    def add_account(self, account: str, cif: str, currency: str = "USD", *, dormant: str = "0",
                    closed_on: Optional[str] = None) -> None:
        self.core[account] = {"internal": f"9{account[-8:]}", "cif": cif, "currency": currency, "dormant": dormant,
                              "closed_on": closed_on}

    def add_activity(self, status: str = "SUCCESS", operation: str = "BALANCE_INQUIRY", *, cif: str = CIF,
                     mobile: str = MOBILE, message: Optional[str] = None, when: Optional[datetime] = None) -> None:
        self.activity.append({"customer_number": cif, "customer_mobile": mobile, "operation": operation,
                              "transaction_type": operation, "channel": "MOBILE_APP", "status": status,
                              "response_code": "200" if status == "SUCCESS" else "400", "response_message": message,
                              "created_date": _stamp(when or now_local() - timedelta(days=1))})

    def add_failure(self, root_cause: str, *, mobile: str = "263771234567", id_normalized: str = "587654321F09",
                    when: Optional[datetime] = None, message: str = "registration failed") -> None:
        self.failures.append({"mobile_number": mobile, "id_number_normalized": id_normalized, "root_cause": root_cause,
                              "error_message": message, "failure_type": "SERVLET_ERROR", "channel": "APP",
                              "log_timestamp": _stamp(when or now_local() - timedelta(days=2))})

    def add_sms(self, kind: str = "otp", status: str = "DELIVERED_SUCCESSFULLY", *, mobile: str = MOBILE,
                when: Optional[datetime] = None) -> None:
        text = {"otp": "Your NMB Connect OTP is 482913", "alert": "Acc debited USD 20.00"}.get(kind, "Hello")
        self.sms.append({"time_stamp": _stamp(when or now_local() - timedelta(hours=5)), "service_provider_name": "ECONET",
                         "external_status": status, "logged_responses": f'{{"to": "263{mobile_key(mobile)}", "text": "{text}"}}'})

    # ---- the API ---------------------------------------------------------------------
    def query(self, database_id: int, sql: str) -> list[dict[str, Any]]:
        if database_id in (REG_DB, CONECT_DB, STANDBY_DB, FAILURES_DB, SOCKET_DB):
            self.sql_log.append((database_id, sql))
            for pattern in self.fail_patterns:
                if pattern in sql:
                    raise MetabaseError(f"simulated failure for {pattern}")
            return {REG_DB: self._reg, CONECT_DB: self._conect, STANDBY_DB: self._core, FAILURES_DB: self._failures,
                    SOCKET_DB: self._socket}[database_id](sql)
        return super().query(database_id, sql)

    def _ticket_query(self, sql: str) -> list[dict[str, Any]]:
        if "1 AS connect_scope" in sql:
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
                             "org_name": t["org_name"], "connect_scope": 1})
            return rows
        return super()._ticket_query(sql)

    def _reg(self, sql: str) -> list[dict[str, Any]]:
        if "information_schema.tables" in sql:
            return [{"table_name": t} for t in ("customer", "history", "user_channels", "otp_requests")]
        if "information_schema.columns" in sql:
            cols = list(self.profiles[0]) if self.profiles else ["id", "customer_number", "mobile_number"]
            return [{"table_name": "customer", "column_name": c} for c in cols] + \
                   [{"table_name": "history", "column_name": c} for c in ("id", "customer_id", "created_at", "action")]
        m = re.search(r"FROM (\w+) WHERE", sql)
        table = m.group(1) if m else ""
        if table in self.missing_tables:
            raise MetabaseError(f'relation "{table}" does not exist')
        cif = re.search(r"LTRIM\(CAST\(customer_number AS TEXT\), '0'\) = '(\d+)'", sql)
        if cif:
            return [dict(r) for r in self.profiles if str(r["customer_number"]).lstrip("0") == cif.group(1)]
        mobile = re.search(r"'g'\), 9\) = '(\d{9})'", sql)
        if mobile:
            return [dict(r) for r in self.profiles if mobile_key(r["mobile_number"]) == mobile.group(1)]
        ids = re.findall(r"'(\d+)'", sql.split(" IN (", 1)[1]) if " IN (" in sql else []
        rows = self.channels if table == "user_channels" else self.history
        return [dict(r) for r in rows if str(r["customer_id"]) in ids]

    def _conect(self, sql: str) -> list[dict[str, Any]]:
        assert "FROM transactions" in sql
        cif = re.search(r"customer_number AS TEXT\), '0'\) = '(\d+)'", sql)
        mobile = re.search(r"customer_mobile AS TEXT\), '\[\^0-9\]', '', 'g'\), 9\) = '(\d{9})'", sql)
        since = re.search(r"created_date >= '([\d-]+)'", sql).group(1)
        rows = [r for r in self.activity if r["created_date"][:10] >= since and (
            (cif and str(r["customer_number"]).lstrip("0") == cif.group(1))
            or (mobile and mobile_key(r["customer_mobile"]) == mobile.group(1)))]
        return sorted(rows, key=lambda r: r["created_date"], reverse=True)

    def _core(self, sql: str) -> list[dict[str, Any]]:
        m = re.search(r"IACLINK_ACTUAL_ACNUM = '(\d+)'", sql)
        if m and "IACLINK_INTERNAL_ACNUM FROM" in sql:
            row = self.core.get(m.group(1))
            return [{"IACLINK_INTERNAL_ACNUM": row["internal"]}] if row else []
        m = re.search(r"ACNTS_INTERNAL_ACNUM = '(\d+)'", sql)
        if m and "SELECT * FROM NMBPRODIDC.ACNTS" in sql:
            return [self._acnts(acct, row) for acct, row in self.core.items() if row["internal"] == m.group(1)]
        m = re.search(r"A.ACNTS_CLIENT_NUM = (\d+)", sql)
        if m:
            return [{"IACLINK_ACTUAL_ACNUM": acct, **self._acnts(acct, row)} for acct, row in self.core.items()
                    if row["cif"] == m.group(1)]
        m = re.search(r"INDCLIENT_CODE = (\d+)", sql)
        if m:
            client = self.clients.get(m.group(1))
            return [{"INDCLIENT_TEL_RES": client["mobile"], "INDCLIENT_EMAIL_ADDR1": client["email"]}] if client else []
        m = re.search(r"COBICINIT_CIF_NO = '(\d+)'", sql)
        if m:
            client = self.clients.get(m.group(1)) or {}
            return [{"COBICADDR_MOB_NO": x} for x in client.get("idccob", [])]
        if "DEMO_PENDING" in sql:
            return [r for r in getattr(self, "pending_rows", []) if r["CLIENT"] in sql]
        raise AssertionError(f"unexpected core banking SQL: {sql[:80]}")

    @staticmethod
    def _acnts(account: str, row: dict[str, Any]) -> dict[str, Any]:
        return {"ACNTS_INTERNAL_ACNUM": row["internal"], "ACNTS_CLIENT_NUM": int(row["cif"]),
                "ACNTS_CURR_CODE": row["currency"], "ACNTS_DORMANT_ACNT": row["dormant"], "ACNTS_INOP_ACNT": "0",
                "ACNTS_CLOSURE_DATE": row["closed_on"], "ACNTS_OPENING_DATE": "2020-01-01", "ACNTS_AC_BAL": 1}

    def _failures(self, sql: str) -> list[dict[str, Any]]:
        mobile = re.search(r"9\) = '(\d{9})'", sql)
        id_ = re.search(r"id_number_normalized = '(\w+)'", sql)
        rows = [r for r in self.failures if (mobile and mobile_key(r["mobile_number"]) == mobile.group(1))
                or (id_ and r["id_number_normalized"] == id_.group(1))]
        return sorted(rows, key=lambda r: r["log_timestamp"], reverse=True)

    def _socket(self, sql: str) -> list[dict[str, Any]]:
        sub = re.search(r"LIKE '%(\d+)%'", sql).group(1)
        since = re.search(r"TIMESTAMP '([\d-]+) 00:00:00'", sql).group(1)
        rows = [r for r in self.sms if sub in r["logged_responses"] and r["time_stamp"][:10] >= since]
        return sorted(rows, key=lambda r: r["time_stamp"], reverse=True)


@pytest.fixture
def mb() -> FakeMetabase:
    return FakeMetabase()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    base = Settings.from_env(env_file=tmp_path / "missing.env")
    return replace(
        base, anthropic_api_key="test", model="test-model", mode="shadow", confidence_threshold=0.85,
        max_tickets_per_run=25, max_closes_per_run=20, allow_request_info=True, reply_source="model",
        kill_switch_file=tmp_path / "STOP", state_db=tmp_path / "nmb_connect.sqlite", reply_staff_id=AGENT_STAFF,
        close_status_id=3, metabase_host="http://mb.test", metabase_api_key="k", recheck_after_hours=24.0,
        max_info_requests=2, attachment_mode="local", portal_users_file=tmp_path / "users.json",
        reg_failures_db_name="registration_failures", reg_customer_table="customer",
    )
