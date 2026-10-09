"""Help topic names as osTicket keeps them: stray spaces and a trailing slash must not hide a topic."""
from __future__ import annotations

from agentcore.osticket import fold_topic, topic_condition, topic_matches


def test_names_with_a_trailing_slash_still_match():
    # osTicket keeps this one as 'Ecocash / Failed Transactions / '; people configure it without the slash
    assert topic_matches("Ecocash / Failed Transactions / ", ["Ecocash / Failed Transactions"])
    assert topic_matches("NMB Connect / USSD / Failed Transactions / Airtime", ["NMB Connect / USSD / Failed Transactions"])
    # as in SQL: spaces inside a name count (the router's own lookup of what the model writes is looser)
    assert topic_matches("Prime /  Other Issues", ["prime /  other issues"])
    assert not topic_matches("Prime /  Other Issues", ["prime / other issues"])
    assert not topic_matches("Ecocash-Registration", ["EcoCash"]) and not topic_matches("", ["SMS Alerts"])
    assert fold_topic(" Ecocash / Failed Transactions / ") == "ECOCASH / FAILED TRANSACTIONS"


def test_the_sql_compares_the_same_way():
    cond = topic_condition(["Ecocash / Failed Transactions /"])
    assert "= 'ECOCASH / FAILED TRANSACTIONS'" in cond and "TRIM(TRAILING '/'" in cond
    assert "LIKE 'ECOCASH / FAILED TRANSACTIONS / %'" in cond
