"""query_data on the SMS alerts desk: only masked numbers this ticket issued. All data invented."""
from __future__ import annotations

import json

from agentcore.dataquery import DataQuery, Source
from agentcore.pii import Pseudonymizer
from sms_alerts.tools import Evidence, Toolbox


class Mb:
    def __init__(self):
        self.sql = []

    def query(self, db, sql):
        self.sql.append(sql)
        return [{"dispatch_status": "FAILED", "failure_reason": "Number barred"}]


def box(mb):
    sms = Source(key="sms_dispatch_messages", database="SMS", table="public.sms_message", about="x",
                 columns=["created_at", "dispatch_status", "failure_reason"], identifiers={"recipient_mobile": "mobile"},
                 via={"recipient_mobile": {"column": "recipient_id", "table": "public.sms_recipient", "key": "id",
                                           "lookup": "mobile_number"}},
                 time_column="created_at", max_days=14, order_by="created_at", db_id=133)
    return Toolbox(checks=None, data=DataQuery(mb, {"sms_dispatch_messages": sms}))


def test_a_mobile_from_the_ticket_is_matched_and_others_are_refused():
    mb, pseudo, ev = Mb(), Pseudonymizer(), Evidence()
    token = pseudo.mask("0772000111")
    text, error = box(mb).run("query_data", {"source": "sms_dispatch_messages", "match_column": ["recipient_mobile"],
                                             "match_value": token}, pseudo, ev)
    assert not error and json.loads(text)["rows"][0]["failure_reason"] == "Number barred"
    assert "'263772000111'" in mb.sql[0] and ev.queries["Q1"]["source"] == "sms_dispatch_messages"
    text, error = box(mb).run("query_data", {"source": "sms_dispatch_messages", "match_column": ["recipient_mobile"],
                                             "match_value": "0772999888"}, pseudo, ev)
    assert error and text.startswith("Refused") and ev.refused == ["0772999888"] and len(mb.sql) == 1
