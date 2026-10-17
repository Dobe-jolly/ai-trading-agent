"""The log pipeline (agentcore.logs): parsing journald + Spring Boot lines with stack traces, keyed-hash masking,
signatures, the store (counts per minute, spikes, new, search) and the tools' input rules. All log data invented."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from agentcore.dataquery import identifier_forms
from agentcore.logs.mask import KeyedMasker
from agentcore.logs.parse import read_records
from agentcore.logs.profiles import Profile, load_profiles, profile_for
from agentcore.logs.signature import signature, template
from agentcore.logs.store import LogStore
from agentcore.logs.tools import LogTools, run_tool

KEY = "unit-test-key-0123456789"
ACCOUNT = "86000013101001"
OTHER = "86000099999999"
MOBILE = "0772123456"


def line(ts: str, level: str, logger: str, msg: str, mdc: str = "", unit: str = "java", pid: int = 4830) -> str:
    """One journalctl line carrying a Spring Boot logback line (ts is 'YYYY-MM-DD HH:MM:SS')."""
    clock = ts[11:19]
    return (f"Oct 17 {clock} integrations.nmbz.co.zw {unit}[{pid}]: {ts}.123 {level:>5} {mdc}{pid} --- [o-9080-exec-7] "
            f"com.nmbz.service.{logger:<24}: {msg}")


def write(path: Path, lines: list[str]) -> Path:
    path.write_text("-- Logs begin at Sat 2026-10-17 14:00:00 CAT, end at Sat 2026-10-17 16:00:00 CAT. --\n"
                    + "\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def masker():
    return KeyedMasker(KEY)


@pytest.fixture
def store(tmp_path):
    return LogStore(tmp_path / "logs.db")


# ---- parsing --------------------------------------------------------------------------------------------------
def test_records_carry_their_stack_trace_and_skip_the_journal_banner(tmp_path):
    p = write(tmp_path / "ussd.log", [
        line("2026-10-17 14:57:49", "INFO", "MessageProcessorImpl", "Handling existing session for 263772123456",
             mdc="[requestId=abc-123] [correlationId=abc-123] "),
        line("2026-10-17 14:57:50", "ERROR", "LoanMenuHandler", ""),
        "Oct 17 14:57:50 integrations.nmbz.co.zw java[4830]: org.springframework.transaction.CannotCreateTransactionException: nope",
        "Oct 17 14:57:50 integrations.nmbz.co.zw java[4830]: at com.nmbz.menu.handler.LoanMenuHandler.handle(LoanMenuHandler.java:41)",
        "Oct 17 14:57:50 integrations.nmbz.co.zw java[4830]: 6. Apply for a Loan",
        line("2026-10-17 14:57:51", "WARNING", "X", "later"),
    ])
    recs = list(read_records(p, Profile(service="ussd")))
    assert [r.level for r in recs] == ["INFO", "ERROR", "WARN"]  # WARNING is WARN
    assert recs[0].message == "Handling existing session for 263772123456" and recs[0].ts == "2026-10-17 14:57:49"
    assert recs[1].message == "" and len(recs[1].extra) == 3 and "CannotCreateTransactionException" in recs[1].extra[0]
    assert recs[1].logger.endswith("LoanMenuHandler")


def test_a_clock_offset_moves_a_services_times_and_a_plain_file_is_read_without_the_journal_prefix(tmp_path):
    plain = tmp_path / "plain.log"
    plain.write_text("2026-10-17 14:00:00.001  INFO 99 --- [main] c.n.App : started\n  at x\n", encoding="utf-8")
    recs = list(read_records(plain, Profile(service="s", format="plain", clock_offset=99)))
    assert recs[0].ts == "2026-10-17 14:01:39" and recs[0].extra == ["  at x"]


# ---- masking ---------------------------------------------------------------------------------------------------
def test_identifiers_become_stable_keyed_hashes_without_digits(masker):
    a = masker.mask(f"accountNumber={ACCOUNT} debit 0{ACCOUNT}")
    hashes = masker.hashes(a)
    assert len(hashes) == 1  # the account with and without a leading zero is one account
    assert ACCOUNT not in a and not re.search(r"\d{6,}", a)
    assert masker.hashes_for(ACCOUNT) == hashes == masker.hashes_for("00" + ACCOUNT)
    assert masker.hashes_for(OTHER) != hashes
    assert KeyedMasker("another-key-0123456789").hashes_for(ACCOUNT) != hashes  # the key matters
    with pytest.raises(ValueError):
        KeyedMasker("short")


def test_a_mobile_in_any_form_has_one_hash_and_names_and_secrets_are_removed(masker):
    forms = masker.hashes_for(MOBILE, "263772123456", "+263 77 212 3456")
    assert len(forms) == 1
    text = masker.mask("msisdn=263772123456 name=John pin=1234 rrn 612345678901 fullName=Mc Donald Chikozho amount=20 "
                       "token=abc123 Authorization: Bearer abc.def.ghi password=hunter2")
    assert "John" not in text and "Chikozho" not in text and "1234" not in text.replace("612345678901", "")
    assert "hunter2" not in text and "abc123" not in text and "amount=20" in text
    assert masker.hashes(text)[0] == forms[0] and len(masker.hashes(text)) == 2  # the mobile, and the RRN beside the pin


def test_email_and_json_names_are_masked_and_a_jwt_is_dropped(masker):
    text = masker.mask('{"firstName":"Tendai","amount":5,"email":"a@b.co"} eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig')
    assert "Tendai" not in text and "a@b.co" not in text and "eyJ" not in text and '"amount":5' in text


# ---- signatures --------------------------------------------------------------------------------------------------
def rec(msg, logger="LoanMenuHandler", extra=None, level="ERROR"):
    from agentcore.logs.parse import Record
    return Record("2026-10-17 15:00:00", level, "com.nmbz.x." + logger, "t", msg, extra or [])


def test_the_same_problem_has_one_signature_whatever_its_numbers():
    a = signature("ussd", rec("Request 123 to 10.1.2.3:8080 failed for <NUMBER:abcdefghijkl> id 6f1c1b0e-0000-4000-8000-000000000001"), [])
    b = signature("ussd", rec("Request 987 to 10.9.9.9:9090 failed for <NUMBER:mnopmnopmnop> id 11111111-2222-4333-8444-555555555555"), [])
    assert a[0] == b[0] and a[1] == b[1]
    assert signature("vbs", rec("Request 123 to 10.1.2.3:8080 failed"), [])[0] != a[0]  # another service
    assert signature("ussd", rec("Something else failed"), [])[0] != a[0]
    assert template("=============== LOGIN REQUEST ===============").startswith("~")


def test_an_id_field_is_one_variable_whatever_the_id_looks_like():
    one = template("IFT business failure requestId=MB<NUMBER:abcdefghijkl>D8B7EAD channel=BILLERS reason=Insufficient funds")
    two = template("IFT business failure requestId=MB<NUMBER:mnopmnopmnop>1FNCN channel=BILLERS reason=Insufficient funds")
    assert one == two == "IFT business failure requestId=<X> channel=BILLERS reason=Insufficient funds"
    assert template("x sessionId=ab-12 uuid=9f1c rrn=<NUMBER:abcdefghijkl> txnRef=T55 valid=true") ==         "x sessionId=<X> uuid=<X> rrn=<X> txnRef=<X> valid=true"
    # the business reason stays in the signature: another reason is another problem
    assert template("failure requestId=A1 reason=Account is dormant") != template("failure requestId=A1 reason=Insufficient funds")
    # a letter next to a masked number, outside an id field, is still one variable
    assert template("operation BALANCE_ENQUIRY_<NUMBER:abcdefghijkl> failed") == "operation BALANCE_ENQUIRY_<X> failed"


def test_exception_class_and_the_first_application_frame_distinguish_empty_messages():
    stack = ["org.springframework.transaction.CannotCreateTransactionException: Could not open JPA EntityManager",
             "\tat org.springframework.orm.jpa.JpaTransactionManager.doBegin(JpaTransactionManager.java:467)",
             "\tat com.nmbz.menu.handler.LoanMenuHandler.handle(LoanMenuHandler.java:41)"]
    sig, tmpl, exc, frame = signature("ussd", rec("", extra=stack), ["com.nmbz"])
    assert tmpl == "(no message)" and exc.endswith("CannotCreateTransactionException")
    assert frame == "com.nmbz.menu.handler.LoanMenuHandler.handle(LoanMenuHandler.java:41)"  # the app frame, not the framework's
    other = signature("ussd", rec("", extra=[stack[0], "\tat com.nmbz.menu.handler.PayeeHandler.handle(PayeeHandler.java:9)"]),
                      ["com.nmbz"])
    assert other[0] != sig


# ---- the store --------------------------------------------------------------------------------------------------
def build(tmp_path, masker, store, service="ussd", extra=(), name=None):
    lines = []
    for m in range(0, 60):  # an hour of traffic: one transaction a minute, an account in the line
        ts = f"2026-10-17 14:{m:02d}:10"
        lines.append(line(ts, "INFO", "MessageProcessorImpl", f"Handling request accountNumber={ACCOUNT} amount=5",
                          mdc=f"[requestId=req-{m:04d}-aaaa] [correlationId=req-{m:04d}-aaaa] "))
        lines.append(line(ts, "INFO", "Controller", f"Other client accountNumber={OTHER} request done",
                          mdc="[requestId=other-req-0001] "))
        if m % 5 == 0:  # the usual background error: one every five minutes
            lines.append(line(ts, "ERROR", "EconetServiceImpl", f"Connection refused to 10.1.2.{m}:8080 ref {m}"))
    lines += list(extra)
    return store.ingest(write(tmp_path / f"{service}.log", lines), Profile(service=service), masker)


def burst(ts_minute: str, n: int, msg: str, logger="Hikari") -> list[str]:
    return [line(f"2026-10-17 {ts_minute}:{s:02d}", "ERROR", logger, msg) for s in range(n)]


def test_ingest_counts_signatures_per_minute_and_keeps_events_only_where_they_help(tmp_path, masker, store):
    stats = build(tmp_path, masker, store)
    assert stats["errors"] == 12 and stats["warns"] == 0 and stats["first"].startswith("2026-10-17 14:00")
    (row,) = store.db.execute("SELECT * FROM signatures").fetchall()
    assert row["total"] == 12 and row["level"] == "ERROR" and "Connection refused" in row["template"]
    assert store.db.execute("SELECT COUNT(*) FROM sig_buckets").fetchone()[0] == 12
    vol = store.services()
    assert vol[0]["service"] == "ussd" and vol[0]["lines"] == 132 and vol[0]["errors"] == 12


def test_nothing_unmasked_is_stored(tmp_path, masker, store):
    build(tmp_path, masker, store)
    dump = "\n".join(str(tuple(r)) for t in ("events", "event_keys", "signatures", "sig_buckets", "volume", "ingests")
                     for r in store.db.execute(f"SELECT * FROM {t}"))
    assert ACCOUNT not in dump and OTHER not in dump and not re.search(r"\d{9,}", dump)
    assert "<NUMBER:" in dump  # replaced by keyed hashes


def test_reingesting_replaces_and_appending_adds_and_notes_survive(tmp_path, masker, store):
    build(tmp_path, masker, store)
    sig = store.db.execute("SELECT sig FROM signatures").fetchone()[0]
    assert store.set_note(sig, "Downstream refused: ask the platform team", by="ops") and not store.set_note("nope", "x")
    build(tmp_path, masker, store)  # same file again: replace
    assert store.db.execute("SELECT total FROM signatures").fetchone()[0] == 12
    assert store.errors()["signatures"][0]["known_cause"].startswith("Downstream refused")
    store.ingest(tmp_path / "ussd.log", Profile(service="ussd"), masker, replace=False)  # append: counted again
    assert store.db.execute("SELECT total FROM signatures").fetchone()[0] == 24


def test_errors_top_lists_signatures_with_counts_and_service_volume(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=burst("15:15", 30, "HikariPool-PostgreSQL - Connection is not available, request timed out after 5001ms."))
    out = store.errors()
    assert [s["count"] for s in out["signatures"]] == [30, 12] and out["signatures"][0]["logger"] == "Hikari"
    assert out["services"][0]["errors"] == 42
    window = store.errors(since="2026-10-17 15:00", until="2026-10-17 15:30")
    assert [s["count"] for s in window["signatures"]] == [30]  # the background errors are all before 15:00
    assert store.errors(service="vbs")["signatures"] == []


def test_spikes_are_found_against_each_signatures_own_baseline_and_clustered_across_services(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=burst("15:15", 30, "HikariPool-PostgreSQL - Connection is not available"))
    build(tmp_path, masker, store, service="intellect-idc",
          extra=burst("15:17", 25, "Intellect request completed with upstream error", logger="WebClientService"))
    out = store.errors(mode="spikes")
    names = {s["service"]: s for s in out["signatures"]}
    assert set(names) == {"ussd", "intellect-idc"}  # the steady background error is not a spike
    assert names["ussd"]["peak_at"] == "2026-10-17 15:15" and names["ussd"]["peak_count"] == 30
    assert out["clusters"][0]["services"] == ["intellect-idc", "ussd"] and "share a cause" in out["note"]
    assert store.errors(service="ussd", mode="spikes")["clusters"] == []


def test_new_signatures_are_those_first_seen_in_the_window(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=burst("15:15", 3, "A brand new failure appeared"))
    new = store.errors(mode="new", since="2026-10-17 15:00")
    assert [s["what"] for s in new["signatures"]] == ["A brand new failure appeared"] and "New relative to" in new["note"]


def test_search_finds_the_clients_lines_and_the_other_lines_of_the_same_requests(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=[
        line("2026-10-17 15:30:00", "INFO", "Controller", f"Debit started accountNumber={ACCOUNT} amount=50",
             mdc="[requestId=trail-0001-zzzz] "),
        line("2026-10-17 15:30:01", "INFO", "WebClientService", "Intellect request started operation=POST",
             mdc="[requestId=trail-0001-zzzz] "),  # same request, no account in it
        line("2026-10-17 15:30:02", "ERROR", "WebClientService", "Intellect request failed with 500",
             mdc="[requestId=trail-0001-zzzz] "),
        line("2026-10-17 15:30:02", "INFO", "Controller", f"Someone else accountNumber={OTHER}", mdc="[requestId=trail-9999-yyyy] "),
    ])
    hashes = masker.hashes_for(ACCOUNT)
    out = store.search(hashes, since="2026-10-17 15:29:00", until="2026-10-17 15:31:00")
    msgs = [e["message"] for e in out["events"]]
    assert any("Debit started" in m for m in msgs) and any("Intellect request failed" in m for m in msgs)
    assert not any("Someone else" in m for m in msgs)
    assert {e["via"] for e in out["events"]} == {"identifier", "same request"}
    assert [e["ts"] for e in out["events"]] == sorted(e["ts"] for e in out["events"])
    assert out["by_service"] == {"ussd": {"INFO": 1}}
    errs = store.search(hashes, levels=["ERROR"], related=True, since="2026-10-17 15:29:00")
    assert [e["level"] for e in errs["events"]] == ["ERROR"]
    assert store.search(masker.hashes_for("86000011111111"))["note"].startswith("Nothing in the ingested logs")


def test_a_session_id_finds_lines_but_only_a_request_id_joins_them_into_a_trail(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=[
        line("2026-10-17 15:40:00", "INFO", "Controller", f"Pay accountNumber={ACCOUNT} sessionId=sess-AAAA-1111",
             mdc="[requestId=pay-0001-aaaa] "),
        line("2026-10-17 15:40:01", "ERROR", "WebClientService", "Upstream refused", mdc="[requestId=pay-0001-aaaa] "),
        line("2026-10-17 15:41:00", "INFO", "Controller", "A later menu page sessionId=sess-AAAA-1111",
             mdc="[requestId=menu-0002-bbbb] "),  # same session, another request
    ])
    out = store.search(masker.hashes_for(ACCOUNT), since="2026-10-17 15:39", until="2026-10-17 15:41")
    msgs = [e["message"] for e in out["events"]]
    assert any("Upstream refused" in m for m in msgs)  # the same request: joined
    assert not any("later menu page" in m for m in msgs)  # the same session only: not joined
    assert any(c.startswith("sessionId=") for e in out["events"] for c in e.get("correlation", []))
    by_session = store.search([], correlation="sess-AAAA-1111")  # but the session id does find them, when searched for
    assert any("later menu page" in e["message"] for e in by_session["events"])


def test_an_id_on_dozens_of_lines_does_not_build_a_trail_but_stays_searchable(tmp_path, masker, store):
    wide = [line(f"2026-10-17 15:{m:02d}:00", "INFO", "Menu", f"menu page {m}", mdc="[requestId=caller-uuid-wide-0001] ")
            for m in range(0, 45)]  # a caller's id reused on 45 lines, named requestId all the same
    mine = [line("2026-10-17 15:50:00", "INFO", "Controller", f"Pay accountNumber={ACCOUNT} requestId=pay-own-id-0001",
                 mdc="[requestId=caller-uuid-wide-0001] "),
            line("2026-10-17 15:50:01", "ERROR", "WebClientService", "Upstream refused requestId=pay-own-id-0001",
                 mdc="[requestId=caller-uuid-wide-0001] ")]
    build(tmp_path, masker, store, extra=wide + mine)
    out = store.search(masker.hashes_for(ACCOUNT), since="2026-10-17 15:49", until="2026-10-17 15:51")
    msgs = [e["message"] for e in out["events"]]
    assert any("Upstream refused" in m for m in msgs)  # joined by the payment's own id
    assert not any(m.startswith("menu page") for m in msgs)  # not by the caller's id that is on 47 lines
    by_id = [e["message"] for e in store.search([], correlation="caller-uuid-wide-0001", limit=40)["events"]]
    assert any(m.startswith("menu page") for m in by_id)  # searched for directly, the id finds them all


def test_when_there_are_too_many_lines_the_errors_are_shown_first(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=[
        line("2026-10-17 15:59:30", "ERROR", "WebClientService", f"The one failure that matters accountNumber={ACCOUNT}",
             mdc="[requestId=late-0001-zzzz] ")])
    out = store.search(masker.hashes_for(ACCOUNT), limit=5)  # 61 matching lines, the error is the latest of them
    assert out["events_shown"] == 5 and any(e["level"] == "ERROR" for e in out["events"])
    assert [e["ts"] for e in out["events"]] == sorted(e["ts"] for e in out["events"])  # still in time order
    assert "warnings and errors first" in out["note"]


def test_a_failure_reason_stays_in_the_signature_so_reasons_are_counted_apart():
    base = "IFT business failure flow=BILLER_WA requestId=MB<NUMBER:abcdefghijkl>D8B7EAD channel=BILLERS operation=BILL_PAYMENTS transactionStatus=RH statusCode= statusDescription="
    assert template(base + "Insufficient funds ACNT.INSUFF_BAL") != template(base + "Account is dormant")
    assert template(base + "Insufficient funds ACNT.INSUFF_BAL").endswith("Insufficient funds ACNT.INSUFF_BAL")


def test_search_by_correlation_id_limit_and_service_filter(tmp_path, masker, store):
    build(tmp_path, masker, store)
    by_corr = store.search([], correlation="req-0007-aaaa")
    assert len(by_corr["events"]) == 1 and by_corr["events"][0]["via"] == "correlation id"
    many = store.search(masker.hashes_for(ACCOUNT), limit=5)
    assert many["events_shown"] == 5 and many["more_rows"] is True and "More lines exist" in many["note"]
    assert store.search(masker.hashes_for(ACCOUNT), service="vbs")["events"] == []


# ---- the tools --------------------------------------------------------------------------------------------------
@pytest.fixture
def tools(tmp_path, masker, store):
    build(tmp_path, masker, store, extra=burst("15:15", 30, "HikariPool-PostgreSQL - Connection is not available"))
    return LogTools(store, masker, {"ussd": Profile(service="ussd", about="USSD gateway")})


def accept(kind, value):
    return identifier_forms(kind, value)


def test_log_search_input_rules(tools):
    with pytest.raises(ValueError, match="either identifier"):
        tools.search({}, accept)
    with pytest.raises(ValueError, match="either identifier"):
        tools.search({"identifier": {"kind": "account", "value": ACCOUNT}, "correlation_id": "req-0001-aaaa"}, accept)
    with pytest.raises(ValueError, match="CIF is not logged"):
        tools.search({"identifier": {"kind": "cif", "value": "123456"}}, accept)
    with pytest.raises(ValueError, match="no long digits"):
        tools.search({"identifier": {"kind": "reference", "value": "ABC123"}}, accept)
    with pytest.raises(ValueError, match="correlation_id looks wrong"):
        tools.search({"correlation_id": "x; DROP TABLE events"}, accept)
    with pytest.raises(ValueError, match="service must be one of"):
        tools.search({"identifier": {"kind": "account", "value": ACCOUNT}, "service": "mystery"}, accept)
    with pytest.raises(ValueError, match="since must be before until"):
        tools.search({"identifier": {"kind": "account", "value": ACCOUNT}, "since": "2026-10-18", "until": "2026-10-17"}, accept)
    seen = []
    out, matched = tools.search({"identifier": {"kind": "account", "value": ACCOUNT}},
                                lambda k, v: seen.append((k, v)) or identifier_forms(k, v))
    assert seen == [("account", ACCOUNT)] and out["matched_directly"] == 60 and matched["kind"] == "account"


def test_a_correlation_id_must_pass_the_desks_check(tools):
    def refuse(corr):
        raise ValueError("correlation_id is not from this ticket")
    with pytest.raises(ValueError, match="not from this ticket"):
        tools.search({"correlation_id": "req-0001-aaaa"}, accept, refuse)
    assert tools.search({"correlation_id": "req-0001-aaaa"}, accept, lambda c: None)[0]["events"]


def test_log_errors_modes_and_the_tool_descriptions(tools):
    assert tools.errors({"mode": "top"})["signatures"][0]["count"] == 30
    assert tools.errors({"mode": "spikes", "service": "ussd"})["signatures"][0]["peak_count"] == 30
    with pytest.raises(ValueError, match="mode must be"):
        tools.errors({"mode": "everything"})
    schemas = {t["name"]: t for t in tools.tool_schemas()}
    assert set(schemas) == {"log_errors", "log_search"}
    assert "USSD gateway" in schemas["log_errors"]["description"] and "2026-10-17 14:00" in schemas["log_search"]["description"]


def test_run_tool_records_queries_a_close_could_cite(tools):
    queries: dict = {}
    out = run_tool(tools, "log_errors", {"mode": "top"}, accept, queries)
    assert out["query_id"] == "Q1" and out["_no_follow_ups"] is True
    assert queries["Q1"]["mode"] == "count" and queries["Q1"]["match_columns"] == []  # service-wide: never carries a close
    out = run_tool(tools, "log_search", {"identifier": {"kind": "account", "value": ACCOUNT}, "limit": 3}, accept, queries)
    assert out["query_id"] == "Q2" and queries["Q2"]["mode"] == "rows" and queries["Q2"]["match_columns"] == ["identifier"]
    assert queries["Q2"]["rows"] == 3 and queries["Q2"]["source"] == "logs: ussd"


# ---- profiles --------------------------------------------------------------------------------------------------
def test_the_shipped_profiles_load_and_a_bad_pattern_is_refused_at_load(tmp_path):
    shipped = load_profiles()
    assert {"ussd", "ussdpostilion", "intellect-idc", "registration", "vbs", "visactivation"} <= set(shipped)
    assert all(p.about for p in shipped.values())
    assert shipped["visactivation"].correlations('"RequestId":"IB-abc-12345"') == [("requestId", "IB-abc-12345")]
    assert shipped["ussd"].correlations("sessionId=sess-1234, x") == [("sessionId", "sess-1234")]
    assert shipped["vbs"].correlations(":6f1c1b0e-0000-4000-8000-000000000001 :USSD") == [("requestUuid", "6f1c1b0e-0000-4000-8000-000000000001")]
    (tmp_path / "bad.json").write_text(json.dumps({"service": "bad", "noise": ["("]}), encoding="utf-8")
    with pytest.raises(re.error):
        load_profiles(tmp_path)
    assert profile_for("brand-new-service").about == "(no profile yet)"
    assert Profile(service="x", noise=["Failed to set IP"]).is_noise("Failed to set IP address in private field: null")
