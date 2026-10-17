"""The NMB Connect lookups against the fakes: what reaches the model (masked, no secrets), which numbers may be looked
up, and the assessment of each kind of problem. All data invented."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from agentcore.pii import Pseudonymizer
from nmb_connect.normalize import now_local
from nmb_connect.osticket import OsTicket
from nmb_connect.sources import AppActivity, CoreBanking, Registration, RegistrationFailures, SmsGateway, column_kind
from nmb_connect.tools import Evidence, Toolbox
from tests.nmb_connect.conftest import (
    ACCOUNT, CIF, CONECT_DB, FAILURES_DB, MOBILE, NEW_MOBILE, OTHER_CIF, OTHER_MOBILE, REG_DB, SOCKET_DB, STANDBY_DB,
    TICKET_DB, ZIG_ACCOUNT,
)


def toolbox(mb, pending_sql=None):
    return Toolbox(Registration(mb, REG_DB), CoreBanking(mb, STANDBY_DB, pending_sql), AppActivity(mb, CONECT_DB),
                   RegistrationFailures(mb, FAILURES_DB), SmsGateway(mb, SOCKET_DB))


def ticket(mb, text, topic="NMB Connect / OTP", **kw):
    mb.add_ticket(1, "300001", [text], topic=topic, **kw)
    store = OsTicket(mb, TICKET_DB, topics=["NMB Connect"], exclude_topics=[])
    t = store.open_tickets()[0]
    t.thread_id = store.thread_ids([t])[t.number]
    store.load_thread(t)
    return t


class Session:
    """One ticket's lookups, as the loop runs them (masked in, masked out)."""

    def __init__(self, mb, t, box=None):
        self.box, self.t = box or toolbox(mb), t
        self.pseudo, self.ev = Pseudonymizer(), Evidence()
        self.pseudo.mask(t.all_text())

    def run(self, name, **args):
        text, error = self.box.run(name, {k: self.pseudo.mask(v) for k, v in args.items()}, self.pseudo, self.ev, self.t)
        return text if error else json.loads(text)


OTP_TICKET = f"Client CIF {CIF} cell {MOBILE} is not receiving the OTP on the app"


# ---- what the model sees ------------------------------------------------------------------------------------------
def test_profile_shows_no_secrets_names_or_device_data(mb):
    mb.add_profile()
    s = Session(mb, ticket(mb, OTP_TICKET))
    out = s.run("get_connect_profile", cif=CIF, mobile=MOBILE)
    text = json.dumps(out)
    for secret in ("notarealhash", "9999", "tmoyo77", "TENDAI", "MOYO", "abc-123", "password", "device", "username"):
        assert secret not in text, secret
    assert "0771234567" not in text and "<MOBILE_1>" in text and "<EMAIL_1>" in text
    p = out["profiles"][0]
    assert p["mobile_matches_ticket"] == "yes" and p["belongs_to_ticket_cif"] == "yes" and p["active"] is True
    assert p["found_by"] == ["cif", "mobile"] and p["state"]["login_attempts"] == 0
    assert out["active_profiles_for_cif"] == {CIF: 1}


def test_column_rules():
    assert column_kind("pin_tries") == "state" and column_kind("pin") == "secret"
    assert column_kind("password_expired") == "state" and column_kind("password_hash") == "secret"
    assert column_kind("otp_sent_at") == "state" and column_kind("otp") == "secret"
    assert column_kind("first_name") == "personal" and column_kind("date_of_birth") == "personal"
    assert column_kind("mobile_number") == "identifier" and column_kind("some_flag_xyz") == "other"


def test_only_the_clients_numbers_may_be_looked_up(mb):
    mb.add_profile(cif=OTHER_CIF, mobile=OTHER_MOBILE)
    s = Session(mb, ticket(mb, OTP_TICKET))
    out = s.run("get_connect_profile", mobile=OTHER_MOBILE)
    assert "not on this ticket" in out and s.ev.refused == ["mobile"]
    out = s.run("check_core_banking", cif=OTHER_CIF)
    assert "not on this ticket" in out


def test_the_cif_of_the_clients_account_and_their_numbers_become_usable(mb):
    mb.add_profile(mobile=NEW_MOBILE)
    s = Session(mb, ticket(mb, f"Client acc {ACCOUNT} gets 'your mobile number does not match'"))
    out = s.run("get_connect_profile", account_number=ACCOUNT)
    assert out["active_profiles_for_cif"] == {CIF: 1} and CIF in s.ev.client_cifs
    core = s.run("check_core_banking", cif=CIF)  # the CIF came from the client's account
    accounts = {a["account_number"]: a for a in core["customer"]["accounts"]}
    assert len(accounts) == 2 and all(a["state"] == "active" for a in accounts.values())
    assert core["customer"]["mobile_on_file_matches_profile"] is False
    # the number on the client's own profile may now be checked at the SMS gateway
    assert isinstance(s.run("get_sms_to_number", mobile=NEW_MOBILE), dict)


def test_activity_failures_and_sms_never_return_codes(mb):
    mb.add_activity("SUCCESS", "BALANCE_INQUIRY", when=now_local() - timedelta(days=2))
    mb.add_activity("FAILED", "IFT", message="Wrong destination account")
    mb.add_failure("javax.persistence.NonUniqueResultException: query did not return a unique result")
    mb.add_sms("otp")
    mb.add_sms("alert")
    s = Session(mb, ticket(mb, OTP_TICKET + ", ID 58-7654321 F 09"))
    act = s.run("get_app_activity", cif=CIF)
    assert act["found"] == 2 and act["last_failure"]["message"] == "Wrong destination account"
    assert act["last_success"]["operation"] == "BALANCE_INQUIRY"
    fails = s.run("find_registration_failures", mobile=MOBILE, id_number="58-7654321 F 09")
    assert fails["failures"][0]["reason_code"] == "DUPLICATE_PROFILE"
    sms = s.run("get_sms_to_number", mobile=MOBILE)
    assert sms["otp_messages"] == 1 and sms["last_otp"]["status"] == "delivered" and "482913" not in json.dumps(sms)


def test_optional_tables_missing_do_not_fail_the_lookup(mb):
    mb.add_profile()
    mb.missing_tables = {"user_channels", "history"}
    s = Session(mb, ticket(mb, OTP_TICKET))
    out = s.run("get_connect_profile", cif=CIF)
    assert out["not_available"] == ["channels", "recent_logins"] and not s.ev.check_errors


def test_bad_table_names_are_refused(mb):
    with pytest.raises(ValueError):
        Registration(mb, REG_DB, customer_table="customer; drop table x")


# ---- assessments ----------------------------------------------------------------------------------------------------
def assess(mb, text, problem, *, lookups=("profile",), topic="NMB Connect / OTP", **args):
    s = Session(mb, ticket(mb, text, topic=topic))
    if "profile" in lookups:
        s.run("get_connect_profile", cif=CIF, mobile=MOBILE if MOBILE in text else NEW_MOBILE if NEW_MOBILE in text
              else None) if (MOBILE in text or NEW_MOBILE in text) else s.run("get_connect_profile", cif=CIF)
    if "core" in lookups:
        s.run("check_core_banking", cif=CIF)
    if "sms" in lookups:
        s.run("get_sms_to_number", mobile=MOBILE)
    if "failures" in lookups:
        s.run("find_registration_failures", mobile=MOBILE)
    out = s.run("assess_access_issue", problem=problem, **args)
    return out, s


def test_otp_registered_with_the_right_number_gets_the_handset_steps(mb):
    mb.add_profile()
    mb.add_sms("otp")
    out, s = assess(mb, OTP_TICKET, "otp_not_received", lookups=("profile", "sms"))
    assert out["where_the_client_stands"] == "registered"
    assert out["suggested_outcome"]["action"] == "reply_and_close"
    reply = s.pseudo.unmask(out["suggested_outcome"]["reply"])
    assert "blocked or spam SMS folder" in reply and "the last OTP was delivered on" in reply


def test_otp_failing_at_the_gateway_needs_a_person(mb):
    mb.add_profile()
    mb.add_sms("otp", "FAILED_TO_DELIVER")
    out, _ = assess(mb, OTP_TICKET, "otp_not_received", lookups=("profile", "sms"))
    assert out["where_the_client_stands"] == "otp_not_delivered" and out["needs_a_person"]


def test_otp_to_another_number_keeps_the_ticket_open(mb):
    mb.add_profile(mobile=NEW_MOBILE)
    out, s = assess(mb, OTP_TICKET, "otp_not_received", lookups=("profile", "core"))
    assert out["where_the_client_stands"] == "mobile_mismatch" and out["suggested_outcome"]["action"] == "request_info"
    reply = s.pseudo.unmask(out["suggested_outcome"]["reply"])
    assert NEW_MOBILE in reply and "ICT Data Science" in reply and "core banking already shows 0771234567" in reply


def test_not_registered_is_closed_with_the_registration_advice(mb):
    out, _ = assess(mb, OTP_TICKET, "otp_not_received")
    assert out["where_the_client_stands"] == "not_registered" and out["suggested_outcome"]["action"] == "reply_and_close"
    assert "07…, 7… or 263…" in out["suggested_outcome"]["reply"]


def test_already_registered_asks_the_branch_to_delete_then_confirms_the_new_profile(mb):
    old = mb.add_profile(created=now_local() - timedelta(days=300))
    text = f"CIF {CIF} cell {MOBILE}: 'Already registered, kindly log in' but cannot log in"
    out, _ = assess(mb, text, "cannot_register", topic="NMB Connect / Registration")
    assert out["where_the_client_stands"] == "already_registered" and out["suggested_outcome"]["action"] == "request_info"
    assert "delete the profile on the dashboard" in out["suggested_outcome"]["reply"]
    mb.profiles.remove(old)
    mb.add_profile(created=now_local() - timedelta(minutes=30))  # the client registered again
    mb.tickets.clear()
    out, _ = assess(mb, text, "cannot_register", topic="NMB Connect / Registration")
    assert out["where_the_client_stands"] == "reregistered" and out["suggested_outcome"]["action"] == "reply_and_close"


@pytest.mark.parametrize("root,status,action", [
    ("EMAIL/PASSWORD REQUEST FAILED for client", "registration_failed_email", "reply_and_close"),
    ("java.net.SocketTimeoutException: Read timed out", "registration_failed_temporary", "reply_and_close"),
    ("javax.persistence.NonUniqueResultException", "registration_failed_duplicate", "escalate"),
    ("java.lang.NullPointerException", "registration_failed_unknown", "escalate"),
])
def test_failed_registrations_by_reason(mb, root, status, action):
    mb.add_failure(root)
    text = f"CIF {CIF} cell {MOBILE}: registration fails with an error"
    out, s = assess(mb, text, "cannot_register", lookups=("profile", "core", "failures"), topic="NMB Connect / Registration")
    assert out["where_the_client_stands"] == status and out["suggested_outcome"]["action"] == action
    if status == "registration_failed_email":
        assert "client@example.com" in s.pseudo.unmask(out["suggested_outcome"]["reply"])


def test_no_profile_and_no_failure_asks_for_the_screenshot(mb):
    text = f"CIF {CIF} cell {MOBILE}: registration fails"
    out, _ = assess(mb, text, "cannot_register", lookups=("profile", "failures"), topic="NMB Connect / Registration")
    assert out["where_the_client_stands"] == "no_attempt_found" and "screenshot" in out["suggested_outcome"]["reply"]


def test_accounts_not_visible(mb):
    mb.add_profile()
    text = f"CIF {CIF} cell {MOBILE}: ZiG account {ZIG_ACCOUNT} not showing on the app"
    out, s = assess(mb, text, "accounts_not_visible", topic="NMB Connect / Account", account_number=ZIG_ACCOUNT)
    assert out["where_the_client_stands"] == "account_active" and out["suggested_outcome"]["action"] == "request_info"
    mb.core[ZIG_ACCOUNT]["dormant"] = "1"
    mb.tickets.clear()
    out, s = assess(mb, text, "accounts_not_visible", topic="NMB Connect / Account", account_number=ZIG_ACCOUNT)
    assert out["where_the_client_stands"] == "account_inactive" and "dormant" in out["suggested_outcome"]["reply"]
    mb.core[ZIG_ACCOUNT].update(dormant="0", cif=OTHER_CIF)
    mb.tickets.clear()
    out, _ = assess(mb, text, "accounts_not_visible", topic="NMB Connect / Account", account_number=ZIG_ACCOUNT)
    assert out["where_the_client_stands"] == "account_other_cif" and out["needs_a_person"]


def test_number_changed(mb):
    mb.add_profile(mobile=NEW_MOBILE)
    out, _ = assess(mb, f"CIF {CIF} changed number to {NEW_MOBILE}", "number_changed",
                    topic="NMB Connect / Account")
    assert out["where_the_client_stands"] == "mobile_updated"
    mb.profiles[0]["mobile_number"] = MOBILE
    mb.tickets.clear()
    out, _ = assess(mb, f"CIF {CIF} changed number to {NEW_MOBILE}", "number_changed", topic="NMB Connect / Account")
    assert out["where_the_client_stands"] == "mobile_mismatch"


def test_duplicates_other_customers_and_locks_need_a_person(mb):
    mb.add_profile()
    mb.add_profile()
    out, _ = assess(mb, OTP_TICKET, "otp_not_received")
    assert out["where_the_client_stands"] == "duplicate_profiles"
    mb.profiles[1].update(customer_number=OTHER_CIF)
    mb.tickets.clear()
    out, s = assess(mb, OTP_TICKET, "otp_not_received")
    assert out["where_the_client_stands"] == "number_on_other_profile"
    assert OTHER_CIF not in s.ev.client_cifs and OTHER_CIF not in s.ev.known.cifs
    mb.profiles[1].update(status="DELETED")
    mb.profiles[0].update(status="LOCKED")
    mb.tickets.clear()
    out, _ = assess(mb, OTP_TICKET, "otp_not_received")
    assert out["where_the_client_stands"] == "locked"


def test_assessing_before_looking_up_is_refused(mb):
    s = Session(mb, ticket(mb, OTP_TICKET))
    assert "get_connect_profile" in s.run("assess_access_issue", problem="otp_not_received")
    assert s.run("assess_access_issue", problem="block_service")["where_the_client_stands"] == "how_to"


def test_a_cif_written_as_a_sentence_counts_as_given_on_the_ticket():
    """Seen in the audit (#627867): "CIF is 191565" was not picked up, so the lookups with it were refused."""
    from nmb_connect.osticket import find_details

    for text, cif in (("Good day, CIF is 191565, client fails to register", "191565"),
                      ("customer number is 2345678", "2345678"), ("cif was 765432", "765432")):
        assert find_details(text).cifs == {cif}
    assert find_details("CIF issue 1234 on login").cifs == set()
