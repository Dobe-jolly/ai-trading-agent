from __future__ import annotations

from sms_alerts.decision import ESCALATE, NO_ACTION, REPLY_AND_CLOSE, REQUEST_INFO, Decision
from sms_alerts.guardrails import apply_guardrails, reply_problems
from sms_alerts.osticket import Ticket, ThreadEntry, TicketState
from agentcore.pii import Pseudonymizer
from sms_alerts.tools import Evidence

FORM = "00012345678901"
OTHER = "00000987654321"


def ticket(entries=None, account=FORM) -> Ticket:
    return Ticket(1, "100001", "SMS Alerts / Transactional", "", "0772123456", None, account, None, "USD", "Debits",
                  thread_id=9, entries=entries or [ThreadEntry(10, "M", None, "x", "No alerts", "")])


def fresh(**kw) -> TicketState:
    base = dict(ticket_id=1, status_state="open", status_name="Open", help_topic="SMS Alerts / Transactional", last_entry_id=10)
    base.update(kw)
    return TicketState(**base)


def evidence(**statuses) -> Evidence:
    ev = Evidence()
    for acct, status in statuses.items():
        ev.account_results[acct] = {"status": status, "reply": "r"}
    return ev


def masked_reply(pseudo: Pseudonymizer, acct: str = FORM) -> str:
    return f"The number on account {pseudo.mask(acct)} matches Intellect. Kindly advise the client to check the spam folder."


def run(dec, ev, t=None, f=None, pseudo=None, **kw):
    pseudo = pseudo or Pseudonymizer()
    return apply_guardrails(dec, t or ticket(), f if f is not None else fresh(), ev, pseudo,
                            confidence_threshold=0.85, closes_so_far=kw.pop("closes", 0), max_closes=20,
                            agent_staff_id=525, **kw)


def close(pseudo, acct=FORM, conf=0.95) -> Decision:
    return Decision(REPLY_AND_CLOSE, conf, "delivered_check_spam", "Delivered.", [], masked_reply(pseudo, acct))


def test_close_allowed_when_evidence_supports_it():
    p = Pseudonymizer()
    result = run(close(p), evidence(**{FORM: "delivered_check_spam"}), pseudo=p)
    assert result.final.action == REPLY_AND_CLOSE and not result.notes


def test_escalating_status_blocks_close_whatever_the_model_says():
    p = Pseudonymizer()
    result = run(close(p), evidence(**{FORM: "socket_failed"}), pseudo=p)
    assert result.final.action == ESCALATE and "socket_failed" in result.notes[0]


def test_unchecked_form_account_and_no_check_block_close():
    p = Pseudonymizer()
    assert run(close(p), Evidence(), pseudo=p).final.action == ESCALATE
    ev = Evidence()
    ev.check_errors[FORM] = "timeout"
    result = run(close(p), ev, pseudo=p)
    assert result.final.action == NO_ACTION and result.deferred


def test_thread_supplied_replacement_account_is_accepted():
    p = Pseudonymizer()
    entries = [ThreadEntry(10, "M", None, "x", "No alerts", ""),
               ThreadEntry(11, "R", 525, "agent", "Invalid account number, kindly share the correct one", ""),
               ThreadEntry(12, "M", None, "x", "Sorry, correct account is 987654321", "")]
    t = ticket(entries)
    ev = evidence(**{OTHER: "delivered_check_spam"})
    result = run(close(p, OTHER), ev, t=t, f=fresh(last_entry_id=12), pseudo=p)
    assert result.final.action == REPLY_AND_CLOSE


def test_account_not_on_ticket_is_rejected():
    p = Pseudonymizer()
    ev = evidence(**{FORM: "delivered_check_spam", OTHER: "delivered_check_spam"})
    result = run(close(p), ev, pseudo=p)
    assert result.final.action == ESCALATE and "do not appear" in result.notes[0]


def test_ticket_data_problems_cannot_close():
    p = Pseudonymizer()
    result = run(close(p), evidence(**{FORM: "invalid_ticket_phone"}), pseudo=p)
    assert result.final.action == ESCALATE and "request_info" in result.notes[0]


def test_request_info_limit():
    p = Pseudonymizer()
    ask = Decision(REQUEST_INFO, 0.95, "invalid_ticket_phone", "Phone invalid.", [],
                   "The phone number on the ticket is not a valid mobile number. Kindly share the client's correct number.")
    assert run(ask, Evidence(), pseudo=p).final.action == REQUEST_INFO
    entries = [ThreadEntry(i, k, 525 if k == "R" else None, "x", "y", "") for i, k in enumerate("MRMRM", start=6)]
    assert run(ask, Evidence(), t=ticket(entries), pseudo=p).final.action == ESCALATE
    assert run(ask, Evidence(), pseudo=p, allow_request_info=False).final.action == ESCALATE


def test_live_state_changes_defer():
    p = Pseudonymizer()
    ev = evidence(**{FORM: "delivered_check_spam"})
    assert run(close(p), ev, f=fresh(status_state="closed"), pseudo=p).deferred
    assert run(close(p), ev, f=fresh(last_entry_id=11), pseudo=p).final.action == NO_ACTION
    assert run(close(p), ev, f=fresh(help_topic="Ecocash / Registration"), pseudo=p).final.action == NO_ACTION
    assert run(close(p), ev, closes=20, pseudo=p).final.action == NO_ACTION


def test_low_confidence_escalates_and_escalate_is_never_blocked():
    p = Pseudonymizer()
    assert run(close(p, conf=0.5), evidence(**{FORM: "delivered_check_spam"}), pseudo=p).final.action == ESCALATE
    esc = Decision(ESCALATE, 0.1, "x", "Unsure.")
    assert run(esc, Evidence(), pseudo=p).final.action == ESCALATE


def test_reply_rules():
    p = Pseudonymizer()
    token = p.mask("0772123456")
    ok = f"The number {token} matches Intellect. Kindly advise the client to check the spam folder."
    assert reply_problems(ok, p) == []
    assert reply_problems(ok + " Last alert 07-10-2026.", p) == []
    assert any("placeholder" in x for x in reply_problems(ok.replace(token, "<MOBILE_9>"), p))
    assert any("never shown" in x for x in reply_problems(ok + " Account 1234 5678 90.", p))
    assert any("PIN" in x for x in reply_problems(ok + " Ask for the PIN.", p))
    assert any("link" in x for x in reply_problems(ok + " See www.nmbz.co.zw", p))
    assert any("leftovers" in x for x in reply_problems(ok + " [insert name]", p))
