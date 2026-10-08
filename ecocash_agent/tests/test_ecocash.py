"""EcoCash errors, lookups, ticket discovery, the model's tools and the safety rules. All data invented."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agent.decision import ESCALATE, NO_ACTION, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from agent.ecocash import CLOSABLE, ESCALATING, AttemptStore, BankAccounts, LinkRecords, classify, clean_message, classify_sql
from agent.guardrails import apply_guardrails
from agent.normalize import LOCAL_ZONE, account_key, id_key, international_mobile, mobile_key, network_of, now_local
from agent.osticket import OsTicket, TicketState, field_value, find_identifiers, tickets_sql
from agentcore.pii import Pseudonymizer
from agent.tools import Evidence, Toolbox, standard_reply
from tests.conftest import ACCOUNT, AGENT_STAFF, ECOCASH_DB, MESSAGES, MOBILE, POSTILION_DB, STANDBY_DB, TICKET_DB


def store(mb):
    return AttemptStore(mb, ECOCASH_DB)


def osticket(mb):
    return OsTicket(mb, TICKET_DB, topics=["EcoCash"], keywords=["ecocash", "eco cash"], exclude_topics=["SMS Alerts"])


def toolbox(mb):
    return Toolbox(store(mb), LinkRecords(mb, POSTILION_DB), BankAccounts(mb, STANDBY_DB))


def loaded(mb, number="100001"):
    t = next(t for t in osticket(mb).open_tickets() if t.number == number)
    return osticket(mb).load_thread(t)


# ---- errors and normalising ------------------------------------------------------------
def test_every_error_from_the_registration_log_is_classified():
    for key, (code, message) in MESSAGES.items():
        assert classify(code, message).key == key
        assert classify(None, message).key == key or key == "unknown"
    assert CLOSABLE.isdisjoint(ESCALATING) and "already_linked" not in CLOSABLE
    assert "TENDAI" not in clean_message(MESSAGES["wallet_closed"][1], classify("TP40050", ""))
    assert "WHEN response_code = '20503' THEN 'already_linked'" in classify_sql()


def test_normalisers():
    assert mobile_key("0772 123 456") == mobile_key("+263772123456") == mobile_key("263772123456") == "772123456"
    assert mobile_key("12345") is None and international_mobile("0772123456") == "+263772123456"
    assert account_key("0086000013101001") == account_key("86000013101001")
    assert id_key("58-7654321 F 09") == "587654321F09" and id_key("hello") is None
    assert network_of("0712345678") == "NetOne" and network_of("0782345678") == "Econet"


# ---- lookups --------------------------------------------------------------------------
def test_attempts_by_any_identifier_with_java_history_before_the_monitor(mb):
    now = now_local()
    mb.add_attempt("already_linked", now - timedelta(hours=1))
    mb.add_attempt("linked", now - timedelta(minutes=10), operation="LINK")
    mb.add_attempt("kyc_mismatch", now, mobile="263782000111", account="999999999", id_number="631111111K11")
    first = min(r["requested_at"] for r in mb.attempts)
    mb.legacy.append({"id": 9, "mobile_number": MOBILE, "error_message": MESSAGES["wallet_closed"][1], "id_number": "",
                      "currency": "USD", "first_name": "TENDAI", "last_name": "MOYO", "account_number": ACCOUNT,
                      "failure_timestamp": (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")})
    lookup = store(mb).find(mobile="0771 234 567")
    assert [a.type_key for a in lookup.attempts] == ["linked", "already_linked", "wallet_closed"]
    assert lookup.status == "linked" and lookup.attempts[-1].source == "java_failures"
    assert [a.type_key for a in store(mb).find(id_number="63-1111111 K 11").attempts] == ["kyc_mismatch"]
    sql = next(q for db, q in mb.sql_log if "FROM ecocash_link_attempts WHERE" in q)
    assert "RIGHT(regexp_replace(mobile_number, '\\D', '', 'g'), 9) = '771234567'" in sql
    legacy_sql = next(q for db, q in mb.sql_log if "failed_ecocash_registrations" in q)
    assert f"failure_timestamp < TIMESTAMP '{first}'" in legacy_sql  # only history from before the new table
    with pytest.raises(ValueError):
        store(mb).find(mobile="12")
    with pytest.raises(ValueError):
        store(mb).find()


def test_describe_never_returns_names_but_says_whether_they_match(mb):
    mb.add_attempt("kyc_mismatch")
    a = store(mb).find(mobile=MOBILE).latest
    d = a.describe("Client Tendai Moyo, ID 58-7654321 F 09")
    assert d["name_sent_matches_ticket"] is True and d["id_sent_matches_an_id_on_ticket"] is True
    assert "TENDAI" not in json.dumps(d) and "MOYO" not in json.dumps(d) and d["requested_by"] == "client on USSD"
    other = a.describe("Client Rudo Banda, ID 63-1234567 K 12")
    assert other["name_sent_matches_ticket"] is False and other["id_sent_matches_an_id_on_ticket"] is False


def test_link_record_and_core_banking(mb):
    mb.links.append({"date_added": "2026-09-01 10:00:00", "created_by": "NMB_CONNECT", "msisdn": "263782999000",
                     "account_id": "0" + ACCOUNT, "currency_code": "840", "status": "1"})
    rows = LinkRecords(mb, POSTILION_DB).find(account=ACCOUNT, mobile=MOBILE)
    assert rows[0]["matches_account_searched"] is True and rows[0]["matches_mobile_searched"] is False
    assert "SELECT TOP 10" in mb.sql_log[-1][1]
    mb.accounts["86000013101001"] = {"currency": "USD", "mobile": "0771234567"}
    assert BankAccounts(mb, STANDBY_DB).check(ACCOUNT) == {
        "account_number": "86000013101001", "exists": True, "currency_code": "USD", "mobile_on_file": "0771234567"}
    assert BankAccounts(mb, STANDBY_DB).check("12345678901")["exists"] is False


# ---- tickets --------------------------------------------------------------------------
def test_ticket_sql_by_topic_and_by_content():
    sql = tickets_sql("open", ["EcoCash"], ["ecocash"], ["SMS Alerts"], "2026-09-07 00:00:00")
    assert "= 'ECOCASH' OR UPPER(" in sql and "LIKE 'ECOCASH / %'" in sql and "LOWER(cd.subject) LIKE '%ecocash%'" in sql
    assert "AND NOT (UPPER(" in sql and "'SMS ALERTS'" in sql and "t.created >= '2026-09-07 00:00:00'" in sql
    closed = tickets_sql("closed", ["EcoCash"], [], [], None, closed_since="2026-10-01")
    assert "cd.subject) LIKE" not in closed and "t.closed >= '2026-10-01 00:00:00'" in closed


def test_open_tickets_include_content_matches_but_not_excluded_topics(mb):
    mb.add_ticket(1, "100001", ["Client cannot link"], fields={"Mobile": "0771234567", "Branch": '{"3": "Harare Main"}'})
    mb.add_ticket(2, "100002", ["Client's ecocash link fails"], topic="General / Other", subject="Link issue")
    mb.add_ticket(3, "100003", ["Alerts for ecocash transfers missing"], topic="SMS Alerts / Transactional")
    mb.add_ticket(4, "100004", ["Password reset"], topic="General / Other", subject="Reset")
    tickets = {t.number: t for t in osticket(mb).open_tickets()}
    assert set(tickets) == {"100001", "100002"}
    assert tickets["100001"].topic_match and not tickets["100002"].topic_match
    assert tickets["100001"].fields == {"Mobile": "0771234567", "Branch": "Harare Main"}
    assert field_value('["a", "b"]') == "a, b" and field_value("plain") == "plain"


def test_identifiers_from_text_ignore_dates():
    found = find_identifiers("Acc 86000013101001 on 0772 123 456, ID 58-7654321 F 09, tried 2026-10-05 10:22 / 05/10/2026")
    assert found.mobiles == {"772123456"} and found.accounts == {"86000013101001"} and found.ids == {"587654321F09"}


# ---- the tool ------------------------------------------------------------------------------
def test_tool_masks_refuses_strangers_and_learns_from_results(mb):
    mb.add_attempt("already_linked")
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    ticket = loaded(mb)
    pseudo, evidence = Pseudonymizer(), Evidence()
    pseudo.mask(ticket.all_text())
    tb = toolbox(mb)
    text, is_error = tb.run("find_ecocash_attempts", {"mobile": "<MOBILE_1>"}, pseudo, evidence, ticket)
    data = json.loads(text)
    assert not is_error and data["found"] == 1 and "771234567" not in text and "86000013101001" not in text
    assert data["summary"]["status"] == "already_linked" and data["summary"]["suggested_action"] == "request_info"
    assert "Universal dashboard" in data["summary"]["standard_reply"] and "<NUMBER_1>" in data["summary"]["standard_reply"]
    # The account came back in the result, so it may now be checked; a number never seen may not.
    mb.accounts[ACCOUNT] = {}
    text, is_error = tb.run("check_bank_account", {"account_number": "<NUMBER_1>"}, pseudo, evidence, ticket)
    assert not is_error and json.loads(text)["exists"] is True
    text, is_error = tb.run("find_ecocash_attempts", {"mobile": "0782000111"}, pseudo, evidence, ticket)
    assert is_error and "not on this ticket" in text and evidence.refused == ["mobile"]
    text, is_error = tb.run("find_ecocash_attempts", {"mobile": "<MOBILE_9>"}, pseudo, evidence, ticket)
    assert is_error and "not a value from this ticket" in text


def test_standard_replies(mb):
    def ticket_with(text):
        mb.tickets.clear()
        mb.add_ticket(1, "100001", [text])
        return loaded(mb)

    mb.add_attempt("kyc_mismatch")
    a = store(mb).find(mobile=MOBILE).latest
    assert standard_reply(a, ticket_with("ID 58-7654321 F 09"))[0] == "reply_and_close"
    assert "EcoCash shop" in standard_reply(a, ticket_with("ID 58-7654321 F 09"))[1]
    action, reply = standard_reply(a, ticket_with("ID 63-1234567 K 12"))
    assert action == "reply_and_close" and "correct the client's ID number on the bank profile" in reply
    assert standard_reply(a, ticket_with("no id here"))[0] == "request_info"
    mb.attempts.clear()
    for kind, expected in (("account_rejected", "escalate"), ("wallet_closed", "reply_and_close"),
                           ("already_linked", "request_info"), ("linked", "reply_and_close"), ("unlinked", "request_info")):
        mb.attempts.clear()
        mb.add_attempt(kind, operation="UNLINK" if kind == "unlinked" else "LINK")
        assert standard_reply(store(mb).find(mobile=MOBILE).latest, None)[0] == expected, kind
    assert standard_reply(None, None)[0] == "request_info"


# ---- the rules in code --------------------------------------------------------------------
def guard(mb, kind, *, reply="Kindly advise the client to visit an EcoCash shop to reactivate the wallet.",
          text="Client 0771234567 cannot link", action=REPLY_AND_CLOSE, when=None, extra=(), **kw):
    mb.tickets.clear()
    mb.add_ticket(1, "100001", [text])
    ticket = loaded(mb)
    mb.attempts.clear()
    for k in extra:
        mb.add_attempt(k, now_local() - timedelta(hours=6))
    if kind:
        mb.add_attempt(kind, when or now_local() - timedelta(hours=1))
    pseudo, evidence = Pseudonymizer(), Evidence()
    pseudo.mask(ticket.all_text())
    toolbox(mb).run("find_ecocash_attempts", {"mobile": "<MOBILE_1>"}, pseudo, evidence, ticket)
    fresh = TicketState(1, "open", "Open", "EcoCash", ticket.last_entry_id)
    return apply_guardrails(Decision(action, 0.95, kind or "none", "r", [], reply), ticket, fresh, evidence, pseudo,
                            confidence_threshold=0.85, closes_so_far=0, max_closes=20, agent_staff_id=AGENT_STAFF,
                            topics=["EcoCash"], **kw)


def test_closes_only_on_an_attempt_the_branch_or_client_can_act_on(mb):
    assert guard(mb, "wallet_closed").final.action == REPLY_AND_CLOSE
    assert guard(mb, "linked").final.action == REPLY_AND_CLOSE
    linked = guard(mb, "already_linked")
    assert linked.final.action == REQUEST_INFO and "keeps the ticket open" in linked.notes[0]
    assert guard(mb, "account_rejected").final.action == ESCALATE
    assert guard(mb, "no_response").final.action == ESCALATE
    assert guard(mb, None).final.action == ESCALATE  # nothing found
    assert guard(mb, "kyc_mismatch").final.action == ESCALATE  # no ID on the ticket to compare
    assert guard(mb, "kyc_mismatch", text="Client 0771234567, ID 58-7654321 F 09").final.action == REPLY_AND_CLOSE
    assert guard(mb, "wallet_closed", when=now_local() - timedelta(days=40)).final.action == ESCALATE  # stale


def test_system_errors_repeating_or_during_an_incident_go_to_a_person(mb):
    assert guard(mb, "ecocash_system_error").final.action == REPLY_AND_CLOSE
    assert guard(mb, "ecocash_system_error", extra=("ecocash_system_error", "ecocash_system_error")).final.action == ESCALATE
    assert guard(mb, "ecocash_system_error", incident_open=True).final.action == ESCALATE


def test_attempt_of_someone_else_and_repeat_after_advice(mb):
    mb.tickets.clear()
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    r = guard(mb, "wallet_closed")
    assert r.final.action == REPLY_AND_CLOSE
    # The same failure after the agent already advised: the advice did not work.
    mb.tickets.clear()
    mb.add_ticket(1, "100001", ["Client 0771234567 cannot link"])
    mb.add_entry(1, "R", "Kindly advise the client to visit an EcoCash shop.", staff_id=AGENT_STAFF,
                 created=now_local() - timedelta(hours=3))
    mb.add_entry(1, "M", "Client went to the shop, still failing", created=now_local() - timedelta(minutes=30))
    ticket = loaded(mb)
    mb.attempts.clear()
    mb.add_attempt("wallet_closed", now_local() - timedelta(minutes=20))
    pseudo, evidence = Pseudonymizer(), Evidence()
    pseudo.mask(ticket.all_text())
    toolbox(mb).run("find_ecocash_attempts", {"mobile": "<MOBILE_1>"}, pseudo, evidence, ticket)
    result = apply_guardrails(Decision(REPLY_AND_CLOSE, 0.95, "wallet_closed", "r", [], "Kindly advise the client again."),
                              ticket, TicketState(1, "open", "Open", "EcoCash", ticket.last_entry_id), evidence, pseudo,
                              confidence_threshold=0.85, closes_so_far=0, max_closes=20, agent_staff_id=AGENT_STAFF,
                              topics=["EcoCash"])
    assert result.final.action == ESCALATE and "happened again after the agent's advice" in result.notes[0]


def test_live_state_and_reply_rules(mb):
    r = guard(mb, "wallet_closed", reply="Your PIN is needed to finish linking this account please.")
    assert r.final.action == ESCALATE and "PIN" in r.notes[0]
    mb.tickets.clear()
    mb.add_ticket(1, "100001", ["Client 0771234567"])
    ticket = loaded(mb)
    moved = apply_guardrails(Decision(REPLY_AND_CLOSE, 0.95, "x", "r", [], "x" * 30), ticket,
                             TicketState(1, "open", "Open", "General / Other", ticket.last_entry_id), Evidence(),
                             Pseudonymizer(), confidence_threshold=0.85, closes_so_far=0, max_closes=20, topics=["EcoCash"])
    assert moved.final.action == NO_ACTION and moved.deferred
