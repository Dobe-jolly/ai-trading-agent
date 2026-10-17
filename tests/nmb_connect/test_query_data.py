"""query_data on the NMB Connect desk: only the client's CIF, account or mobile; results recorded. All data invented."""
from __future__ import annotations

import pytest

from agentcore.dataquery import DataQuery, Source
from nmb_connect.osticket import Details
from nmb_connect.tools import Evidence, Toolbox


class Mb:
    def __init__(self):
        self.sql = []

    def query(self, db, sql):
        self.sql.append(sql)
        return [{"channel": "APP", "active": True, "login_attempts": 3}]


def box(mb):
    channels = Source(key="connect_user_channels", database="CONECT 2.0 REG", table="public.user_channels", about="x",
                      columns=["channel", "active", "login_attempts"],
                      identifiers={"customer_cif": "cif", "customer_mobile": "mobile"},
                      via={"customer_cif": {"column": "customer_id", "table": "public.customers", "key": "id",
                                            "lookup": "customer_number"},
                           "customer_mobile": {"column": "customer_id", "table": "public.customers", "key": "id",
                                               "lookup": "mobile_number"}}, order_by="id", db_id=128)
    return Toolbox(data=DataQuery(mb, {"connect_user_channels": channels}))


def test_the_clients_cif_is_matched_through_the_profile_table():
    mb, ev = Mb(), Evidence()
    ev.known = Details(cifs={"191565"})
    out = box(mb)._query_data({"source": "connect_user_channels", "match_column": ["customer_cif"],
                               "match_value": "191565"}, ev, enforce=True)
    assert out["query_id"] == "Q1" and out["rows"][0]["login_attempts"] == 3
    assert 'WHERE "customer_number" IN (\'191565\')' in mb.sql[0] and ev.queries["Q1"]["kind"] == "cif"
    assert any(s["tool"] == "query_data" for s in ev.searches)


def test_another_clients_number_is_refused():
    mb, ev = Mb(), Evidence()
    ev.known = Details(cifs={"191565"})
    with pytest.raises(ValueError, match="not on this ticket"):
        box(mb)._query_data({"source": "connect_user_channels", "match_column": ["customer_mobile"],
                             "match_value": "0772000111"}, ev, enforce=True)
    assert ev.refused == ["query_data mobile"] and mb.sql == []
    assert any(s["name"] == "query_data" for s in box(mb).schemas())
