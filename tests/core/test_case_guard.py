"""What past cases can't do (agentcore.cases.guard, docs/retrieval.md section 4): close a ticket on their own, or put
their text in a reply. Invented data only."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from agentcore.audit import AuditLog
from agentcore.cases.guard import COPY_WORDS, copied_case, placeholder_problem
from agentcore.cases.store import CaseDraft, CaseStore
from agentcore.cases.tools import MAX_RESULT_CHARS, TOOL_NAME, CaseTools, shown_cases
from agentcore.decision import REPLY_AND_CLOSE, Decision
from agentcore.guardrails import reply_problems
from agentcore.loop import AgentResult
from agentcore.pii import Pseudonymizer
from agentcore.runner import DeskRunner

TODAY = date(2026, 10, 27)
RESOLUTION = ("The ZIPIT did not reach the beneficiary bank because the issuer was unavailable at the time, and the "
              "switch posted a reversal advice within minutes, so the full amount went back to the account the same "
              "day and the client may simply try the transfer again now.")


@pytest.fixture
def audit(tmp_path):
    log = AuditLog(tmp_path / "state.db")
    yield log
    log.close()


def transcript(store, args):
    text, _ = CaseTools(store, today=TODAY).run(TOOL_NAME, args, Pseudonymizer())
    return [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": TOOL_NAME, "input": args}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}]


@pytest.fixture
def store(audit):
    s = CaseStore(audit.db, "failed_txn")
    s.save(CaseDraft(source="staff", ticket_id=1, source_ticket="501", topic="ZIPIT", closed_on="2026-10-01",
                     asked="ZIPIT not received by [CUSTOMER]", found="Postilion: response code 91.",
                     resolution=RESOLUTION, tags={("code", "91")}))
    return s


# ---- placeholders ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("placeholder", ["[NUMBER]", "[MOBILE]", "[NATIONAL_ID]", "[EMAIL]", "[CUSTOMER]", "[STAFF]"])
def test_a_case_placeholder_blocks_the_reply_on_every_desk(placeholder):
    text = f"Kindly ask the client to confirm {placeholder} and try the ZIPIT again."
    problems = reply_problems(text, Pseudonymizer())
    assert any(placeholder in p and "past case" in p for p in problems)
    assert any("past case" in p for p in reply_problems(text, Pseudonymizer(), secret_topic=True))  # NMB Connect


def test_ordinary_brackets_and_tokens_are_not_case_placeholders():
    assert placeholder_problem("The ZIPIT [ref 123] was reversed.") is None
    pseudo = Pseudonymizer()
    pseudo.mask("account 1234567890")
    assert reply_problems("The ZIPIT from <NUMBER_1> was reversed; the client may try again.", pseudo) == []


# ---- copied passages ------------------------------------------------------------------------------------
def test_a_long_copied_passage_is_found_through_numbers_and_punctuation():
    reply = "Good day. " + RESOLUTION.replace("the same day", "on 2026-10-26,") + " Regards."
    assert copied_case(reply, {1: RESOLUTION, 2: "unrelated"}) == 1


def test_a_short_shared_phrase_is_not_a_copy():
    reply = "The switch posted a reversal advice within minutes, so the client can try again."
    assert copied_case(reply, {1: RESOLUTION}) is None


def test_the_desks_own_standard_wording_is_not_a_copy():
    assert copied_case(RESOLUTION, {1: RESOLUTION}, [RESOLUTION]) is None
    assert copied_case(RESOLUTION, {1: RESOLUTION}, [None, "something else"]) == 1
    assert copied_case("", {1: RESOLUTION}) is None


def test_shown_cases_reads_what_the_model_was_shown(store):
    cases = shown_cases(transcript(store, {"code": "91"}))
    assert list(cases) == [1] and RESOLUTION in cases[1] and "response code 91" in cases[1]
    assert shown_cases([]) == {}


def test_shown_cases_reads_a_cut_short_result_up_to_its_last_whole_case(audit):
    s = CaseStore(audit.db, "failed_txn")
    for i in range(1, 6):
        s.save(CaseDraft(source="staff", ticket_id=i, source_ticket=str(i), topic="ZIPIT", closed_on="2026-10-01",
                         asked="ZIPIT failed " + "word " * 300, resolution=f"Resolution {i}", tags={("code", "91")}))
    t = transcript(s, {"code": "91"})
    assert t[1]["content"][0]["content"].count("truncated") == 1
    cases = shown_cases(t)
    assert 1 <= len(cases) < 5


def fake_runner(template=None):
    return SimpleNamespace(template=lambda ticket, result: template)


def result_with(store, reply):
    decision = Decision(action=REPLY_AND_CLOSE, confidence=0.95, category="ZIPIT", reason="r", reply_text=reply)
    return AgentResult(decision=decision, transcript=transcript(store, {"code": "91"}))


def test_a_reply_copying_a_case_waits_for_a_person(store):
    result = result_with(store, RESOLUTION)
    why = DeskRunner.case_copy_reason(fake_runner(), None, result, RESOLUTION)
    assert why and "C1" in why and str(COPY_WORDS) in why


def test_a_reply_in_its_own_words_or_the_standard_wording_does_not(store):
    own = "The ZIPIT was reversed by the switch the same day; the client can check the balance and try again."
    assert DeskRunner.case_copy_reason(fake_runner(), None, result_with(store, own), own) is None
    assert DeskRunner.case_copy_reason(fake_runner(RESOLUTION), None, result_with(store, RESOLUTION), RESOLUTION) is None
    no_cases = AgentResult(decision=Decision(action=REPLY_AND_CLOSE, confidence=1, category="", reason=""))
    assert DeskRunner.case_copy_reason(fake_runner(), None, no_cases, RESOLUTION) is None


# ---- a close on a past case alone, desk by desk ------------------------------------------------------------
REPLY = "The ZIPIT was reversed the same day, so the client can check the balance and try again."


def case_only(decision_cls=Decision):
    return decision_cls(action=REPLY_AND_CLOSE, confidence=0.99, category="ZIPIT reversed",
                        reason="Past case C1 had the same code and was reversed; this one will be too.",
                        evidence=["C1: response 91, reversed the same day"], reply_text=REPLY)


def run_desk(desk):
    pseudo = Pseudonymizer()
    common = dict(confidence_threshold=0.85, closes_so_far=0, max_closes=20, check_live_state=False)
    if desk == "failed_txn":
        from failed_txn.guardrails import apply_guardrails
        from failed_txn.osticket import Ticket, ThreadEntry
        from failed_txn.tools import Evidence
        t = Ticket(1, "100001", "Failed ZIPIT Transaction", "",
                   entries=[ThreadEntry(1, "M", None, "x", "Client did a ZIPIT of $20 and it failed", "")])
        return apply_guardrails(case_only(), t, None, Evidence(tools_used=[TOOL_NAME]), pseudo, **common)
    if desk == "ecocash":
        from ecocash.guardrails import apply_guardrails
        from ecocash.osticket import Ticket, ThreadEntry
        from ecocash.tools import Evidence
        t = Ticket(1, "100001", "EcoCash", "",
                   entries=[ThreadEntry(1, "M", None, "x", "Client cannot link EcoCash", "")])
        return apply_guardrails(case_only(), t, None, Evidence(tools_used=[TOOL_NAME]), pseudo, **common)
    if desk == "sms_alerts":
        from sms_alerts.guardrails import apply_guardrails
        from sms_alerts.osticket import Ticket, ThreadEntry
        from sms_alerts.tools import Evidence
        t = Ticket(1, "100001", "SMS Alerts / Transactional", "", "0772123456", None, "00012345678901", None, "USD",
                   "Debits", thread_id=9, entries=[ThreadEntry(10, "M", None, "x", "No alerts", "")])
        ev = Evidence()
        ev.tools_used.append(TOOL_NAME)
        return apply_guardrails(case_only(), t, None, ev, pseudo, **common)
    if desk == "nmb_connect":
        from nmb_connect.guardrails import apply_guardrails
        from nmb_connect.osticket import Ticket, ThreadEntry
        from nmb_connect.tools import Evidence
        t = Ticket(1, "100001", "NMB Connect / Registration", "",
                   entries=[ThreadEntry(1, "M", None, "x", "Client cannot register on NMB Connect", "")])
        return apply_guardrails(case_only(), t, None, Evidence(tools_used=[TOOL_NAME]), pseudo, **common)
    raise AssertionError(desk)


@pytest.mark.parametrize("desk", ["failed_txn", "ecocash", "sms_alerts", "nmb_connect"])
def test_a_close_resting_on_a_past_case_alone_is_refused(desk):
    assert reply_problems(REPLY, Pseudonymizer()) == []  # the reply is clean: the evidence gate is what stops it
    guard = run_desk(desk)
    assert guard.final.action != REPLY_AND_CLOSE
    assert guard.notes and all(n.startswith("close blocked:") for n in guard.notes)  # the desk's own evidence gate


def test_the_router_cannot_close_at_all():
    from router.decision import RouteDecision

    with pytest.raises(ValueError):
        RouteDecision.from_tool_input({"action": REPLY_AND_CLOSE, "confidence": 0.99, "category": "x", "reason": "C1",
                                       "evidence": ["C1"], "reply_text": REPLY})
