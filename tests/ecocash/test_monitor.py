"""The journal monitor that replaces the Java ecocash-registration-monitor. All data invented."""
from __future__ import annotations

import io
import json
import threading
from datetime import datetime, timedelta

import pytest

from ecocash.monitor.follower import JournalFollower, load_position
from ecocash.monitor.parser import JournalParser, parse_text, request_fields
from ecocash.monitor.store import MemoryStore, MetabaseStore, PostgresStore, jdbc_to_conninfo, legacy_row, literal

HOST = "integrations.nmbz.co.zw java[943]: "


def app(ts: str, thread: str, text: str, level: str = "INFO") -> str:
    return f"{ts}  {level} 943 --- [{thread:>15}] c.n.e.s.i.ReadMessageConsumerServiceImpl : {text}"


def request(ts: str, thread: str, op: str = "ECOCASH_REGISTRATION", mobile: str = "263771234567",
            account: str = "86000013101001", channel: str = "NMBCONNECT", model: str = "USSD", imei: str = "263771234567"):
    body = (f"NMBZApiRequest{{operation='{op}', channel='{channel}', accessToken='tok', uuid='u-1', asyncRequest=false, "
            f"requestBody={{firstName=TENDAI, lastName=MOYO, mobileNumber={mobile}, latitude=23.900, imei={imei}, "
            f"currency=ZiG, model={model}, idNumber=587654321F09, accountNumber={account}, longitude=45.345}}}}")
    return [app(ts, thread, "............................. ECOCASH REGISTER REQUEST ................." + body),
            app(ts[:-3] + str(int(ts[-3:]) + 1).zfill(3), thread, "************* ECOCASHREGISTER *********" + body)]


def ecocash_body(ts: str, thread: str, ref: str, service: str = "BANKREG", account: str = "86000013101001"):
    # As in the real log, bankAccNumber carries the mobile number, not the account.
    return [app(ts, thread, f"******************** ECOCASH REG REQUEST *************************{ref}"),
            app(ts, thread, "******************** ECOCASH REG REQUEST  BODY *************************EcocashRegRequest{"
                f"serviceType='{service}', externalReference='{ref}', bankId='IND1', bankAccNumber='771234567', "
                "msisdn='263771234567', currency='ZIG', firstName='TENDAI', lastName='MOYO', nationalId='587654321F09'}")]


def success(ts: str, thread: str, ref: str):
    js = ('{"serviceType":"BANKREG","status":"200","statusMessage":"Transaction completed","responseCode":"00",'
          f'"requestId":"{ref}","responseMessage":"Bank account linked successfully","transactionId":"AG_1",'
          '"msisdn":"263771234567","name":"TENDAI MOYO","timestamp":"2026-10-06T09:00:03"}')
    return [app(ts, thread, "******** ECOCASH REG RESPONSE *********200 OK"),
            app(ts, thread, "******** ECOCASH REG RESPONSE ***********" + js),
            app(ts, thread, f"******************** 0000 RESPONSE *************************<200,{js},{{Date=[x]}}>")]


def failure(ts: str, thread: str, code: str = "20503",
            message: str = "During bank account creation, the entered bank account already exists in the identity."):
    js = f'{{"status":400,"responseCode":"{code}","exception":"ProcessingException","message":"{message}","timestamp":"x"}}'
    return [app(ts, thread, "************** 400 BAD REQUEST *****"),
            app(ts, thread, " ******  400 RESPONSE BODY ****** " + js)]


def journal(*groups, wrap: int = 0) -> list[str]:
    """As `journalctl` prints it: a syslog prefix per entry, optionally terminal-wrapped."""
    lines = []
    for group in groups:
        for msg in group:
            line = "Oct 06 09:00:01 " + HOST + msg
            if wrap:
                lines += [line[i:i + wrap] for i in range(0, len(line), wrap)]
            else:
                lines.append(line)
    return lines


def test_link_success_and_failure_from_a_wrapped_text_export():
    lines = journal(
        request("2026-10-06 09:00:01.100", "nio-9451-exec-1"),
        ecocash_body("2026-10-06 09:00:01.200", "nio-9451-exec-1", "REF0001"),
        request("2026-10-06 09:00:01.300", "nio-9451-exec-2", mobile="263782000111", account="09400000310100"),
        ecocash_body("2026-10-06 09:00:01.400", "nio-9451-exec-2", "REF0002", account="09400000310100"),
        failure("2026-10-06 09:00:01.900", "nio-9451-exec-2"),
        ["Message: During bank account creation, the entered bank account already exists in the identity."],
        success("2026-10-06 09:00:02.100", "nio-9451-exec-1", "REF0001"),
        wrap=160)
    lines.insert(0, "-- Logs begin at Mon 2026-10-06 08:00:00 CAT. --")
    done, parser = parse_text(lines)
    assert [(a.external_reference, a.outcome, a.response_code) for a in done] == [
        ("REF0002", "failed", "20503"), ("REF0001", "linked", "00")]
    linked = done[1]
    assert linked.via == "USSD" and linked.ecocash_name == "TENDAI MOYO" and linked.ecocash_transaction_id == "AG_1"
    assert linked.account_number == "86000013101001" and linked.first_name == "TENDAI" and linked.currency == "ZIG"
    assert linked.requested_at == datetime(2026, 10, 6, 9, 0, 1, 100000)
    assert parser.stats["requests"] == 2 and not parser.pending  # the duplicate request log was not a second attempt


def test_delink_from_the_branch_and_requests_with_no_answer():
    lines = journal(
        request("2026-10-06 10:00:00.000", "nio-9451-exec-3", op="ECOCASH_DEREGISTRATION", model="VBS-t.moyo@nmbz.co.zw",
                imei="t.moyo@nmbz.co.zw"),
        ecocash_body("2026-10-06 10:00:00.100", "nio-9451-exec-3", "REF0003", service="BANKDEREG"),
        failure("2026-10-06 10:00:00.700", "nio-9451-exec-3", "TC30174",
                "The value 778***367 of the AccountNumber parameter is incorrect, failed to authenticate the parameter."),
        request("2026-10-06 10:01:00.000", "nio-9451-exec-4"),
        ecocash_body("2026-10-06 10:01:00.100", "nio-9451-exec-4", "REF0004"),
        [app("2026-10-06 10:01:30.000", "nio-9451-exec-4", "java.net.SocketTimeoutException: Read timed out", "ERROR")],
        request("2026-10-06 10:02:00.000", "nio-9451-exec-4", mobile="263782000999"),
        request("2026-10-06 10:09:00.000", "nio-9451-exec-5"))
    done, parser = parse_text(lines, no_response_seconds=180)
    by_ref = {a.key: a for a in done}
    delink = by_ref["REF0003"]
    assert delink.operation == "UNLINK" and delink.via == "VBS" and delink.staff_user == "t.moyo@nmbz.co.zw"
    assert delink.outcome == "failed" and delink.response_code == "TC30174"
    timed_out = by_ref["REF0004"]
    assert timed_out.outcome == "no_response" and "Read timed out" in timed_out.message
    # The 10:02 request on exec-4 was never answered and is 7 minutes older than the last line: timed out too.
    assert sum(1 for a in done if a.outcome == "no_response") == 2
    assert list(parser.pending) == ["nio-9451-exec-5"]  # the last one may still be answered


def test_the_loose_message_line_is_used_only_when_one_attempt_waits():
    parser = JournalParser()
    for line in journal(request("2026-10-06 11:00:00.000", "exec-1"), ecocash_body("2026-10-06 11:00:00.100", "exec-1", "R1"),
                        request("2026-10-06 11:00:00.200", "exec-2"), ecocash_body("2026-10-06 11:00:00.300", "exec-2", "R2"),
                        [app("2026-10-06 11:00:00.900", "exec-1", "************** 400 BAD REQUEST *****"),
                         app("2026-10-06 11:00:00.950", "exec-2", "************** 400 BAD REQUEST *****"),
                         "Message: KYC verification failed."]):
        parser.feed_text_line(line)
    parser.flush()
    assert {a.outcome for a in parser.pending.values()} == {None}  # two waited: no guessing
    parser = JournalParser()
    out = []
    for line in journal(request("2026-10-06 11:00:00.000", "exec-1"), ecocash_body("2026-10-06 11:00:00.100", "exec-1", "R1"),
                        [app("2026-10-06 11:00:00.900", "exec-1", "************** 400 BAD REQUEST *****"),
                         "Response Code: BCS50646", "Message: KYC verification failed."]):
        out += parser.feed_text_line(line)
    out += parser.flush()
    assert [(a.outcome, a.response_code, a.message) for a in out] == [("failed", "BCS50646", "KYC verification failed.")]


def test_json_mode_and_request_fields():
    parser = JournalParser()
    out = []
    for group in (request("2026-10-06 12:00:00.000", "exec-9", channel="IAPPROVE", model="NOT CAPTURED"),
                  ecocash_body("2026-10-06 12:00:00.100", "exec-9", "R9"), success("2026-10-06 12:00:00.800", "exec-9", "R9")):
        for msg in group:
            out += parser.feed_message(msg, "integrations")
    assert len(out) == 1 and out[0].via == "IAPPROVE" and out[0].host == "integrations"
    assert request_fields("x requestBody={firstName=Rudo Ann, mobileNumber=0772, latitude=, model=VBS-a@b.zw}}") == {
        "firstName": "Rudo Ann", "mobileNumber": "0772", "latitude": "", "model": "VBS-a@b.zw"}


# ---- storage -------------------------------------------------------------------------
def attempts_sample():
    done, _ = parse_text(journal(
        request("2026-10-06 09:00:01.100", "e1"), ecocash_body("2026-10-06 09:00:01.200", "e1", "R1"),
        failure("2026-10-06 09:00:01.900", "e1"),
        request("2026-10-06 09:00:02.100", "e2", op="ECOCASH_DEREGISTRATION", model="VBS-a@nmbz.co.zw"),
        ecocash_body("2026-10-06 09:00:02.200", "e2", "R2", service="BANKDEREG"),
        failure("2026-10-06 09:00:02.900", "e2", "TC30174", "rejected")))
    return done


def test_memory_store_is_idempotent_and_keeps_the_java_table_for_link_failures():
    store, done = MemoryStore(), attempts_sample()
    assert store.save(done) == 2 and store.save(done) == 0
    assert len(store.legacy) == 1 and store.legacy[0]["error_message"].startswith("During bank account creation")
    assert legacy_row(done[1]) is None  # unlink failures were never in the Java table


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))
        self.last = sql

    def fetchone(self):
        if "ecocash_link_attempts" in self.last and "RETURNING" in self.last:
            self.conn.n += 1
            return (self.conn.n,) if self.conn.n <= self.conn.accept else None
        return None


class FakeConn:
    def __init__(self, accept=99):
        self.statements, self.n, self.accept, self.commits = [], 0, accept, 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def test_postgres_store_uses_parameters_and_creates_the_schema():
    conn = FakeConn(accept=1)
    store = PostgresStore("postgresql://x", connect=lambda dsn: conn)
    store.ensure_schema()
    ddl = [s for s, _ in conn.statements]
    assert any("CREATE TABLE IF NOT EXISTS ecocash_link_attempts" in s for s in ddl)
    assert any("CREATE TABLE IF NOT EXISTS failed_ecocash_registrations" in s for s in ddl)
    conn.statements.clear()
    assert store.save(attempts_sample()) == 1
    insert, params = conn.statements[0]
    assert "ON CONFLICT (attempt_key) DO NOTHING RETURNING id" in insert and "%s" in insert and params[0] == "R1"
    assert any("failed_ecocash_registrations" in s for s, _ in conn.statements)  # legacy row for the new link failure


def test_metabase_store_literals_and_jdbc_url():
    class MB:
        def __init__(self):
            self.sql = []

        def query(self, db, sql):
            self.sql.append(sql)
            return [{"id": 1}]

        def write(self, db, sql):
            self.sql.append(sql)

    mb = MB()
    MetabaseStore(mb, 7).save(attempts_sample()[:1])
    assert "VALUES ('R1', 'LINK', 'failed'" in mb.sql[0] and "TIMESTAMP '2026-10-06 09:00:01.100000'" in mb.sql[0]
    assert literal("O'Brien\x00") == "'O''Brien'" and literal(None) == "NULL"
    assert jdbc_to_conninfo("jdbc:postgresql://192.0.2.10:5422/ecocash_monitor?ssl=false", "user", "p@ss:w/rd") == \
        "postgresql://user:p%40ss%3Aw%2Frd@192.0.2.10:5422/ecocash_monitor?ssl=false"
    with pytest.raises(ValueError):
        jdbc_to_conninfo("jdbc:mysql://h/db", None, None)


# ---- following the journal ------------------------------------------------------------
class FakeProc:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        self.returncode = 0

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def entries(*groups):
    out, n = [], 0
    for group in groups:
        for msg in group:
            n += 1
            out.append({"__CURSOR": f"c{n}", "MESSAGE": msg, "_HOSTNAME": "integrations"})
    return out


def test_follower_saves_attempts_and_resumes_from_an_unfinished_request(tmp_path):
    lines = entries(request("2026-10-06 09:00:01.100", "e1"), ecocash_body("2026-10-06 09:00:01.200", "e1", "R1"),
                    success("2026-10-06 09:00:01.900", "e1", "R1"),
                    request("2026-10-06 09:00:05.000", "e2"))  # still waiting when the journal ends
    commands = []
    stop = threading.Event()

    def popen(cmd, **kw):
        commands.append(cmd)
        if len(commands) > 1:  # journalctl restarted after the first stream ended: stop the test there
            stop.set()
            return FakeProc([])
        return FakeProc(lines)

    store = MemoryStore()
    follower = JournalFollower(JournalParser(), store, unit="ecocashreg.service", position_file=tmp_path / "pos.json",
                               popen=popen, restart_delay=0, now=lambda: datetime(2026, 10, 6, 9, 0, 6))
    follower.run(stop)
    assert commands[0][-2:] == ["-n", "0"] and "-o" in commands[0] and "json" in commands[0]
    assert list(store.rows) == ["R1"]
    position = load_position(tmp_path / "pos.json")
    assert position["inclusive"] is True and position["cursor"] == "c8"  # the waiting request's first line
    assert commands[1][-1] == "--cursor=c8"  # the restart resumes at the waiting request


def test_follower_keeps_unsaved_attempts_and_does_not_move_on(tmp_path):
    class Flaky(MemoryStore):
        fail = True

        def save(self, attempts):
            if self.fail and attempts:
                raise RuntimeError("database down")
            return super().save(attempts)

    store = Flaky()
    follower = JournalFollower(JournalParser(), store, unit="u", position_file=tmp_path / "pos.json",
                               now=lambda: datetime(2026, 10, 6, 9, 1))
    for line in entries(request("2026-10-06 09:00:01.100", "e1"), ecocash_body("2026-10-06 09:00:01.200", "e1", "R1"),
                        failure("2026-10-06 09:00:01.900", "e1")):
        follower.handle(json.dumps(line))
    follower.save_position()
    assert follower.unsaved and not (tmp_path / "pos.json").exists()
    store.fail = False
    follower.tick(idle=True)
    follower.save_position()
    assert list(store.rows) == ["R1"] and load_position(tmp_path / "pos.json") == {
        **load_position(tmp_path / "pos.json"), "cursor": "c6", "inclusive": False}


def test_backfill_command_counts_a_saved_export(tmp_path, capsys, monkeypatch):
    from ecocash import __main__ as cli

    export = tmp_path / "ecoreg.txt"
    export.write_text("\n".join(journal(request("2026-10-06 09:00:01.100", "e1"),
                                        ecocash_body("2026-10-06 09:00:01.200", "e1", "R1"),
                                        failure("2026-10-06 09:00:01.900", "e1", "TP40050", "closed"))), encoding="utf-8")
    monkeypatch.setenv("METABASE_API_KEY", "")
    assert cli.main(["--env-file", str(tmp_path / "none.env"), "monitor", "--from-file", str(export), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1 attempt(s)" in out and "link failed TP40050" in out and "dry run" in out
