"""The case store and its builder (agentcore.cases.store, agentcore.cases.build). Invented tickets, names and numbers."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from agentcore.audit import AuditLog
from agentcore.cases.build import CaseBuilder, log_signatures
from agentcore.cases.store import CaseDraft, CaseStore, clean_tag
from agentcore.osticket import ThreadEntry
from agentcore.pii import TOKEN_RE

TODAY = date(2026, 10, 27)
AGENT = 99
MARKER = "agent did not auto-close this ticket."


def entry(i, kind, body, poster, staff_id=None):
    return ThreadEntry(id=i, type=kind, staff_id=staff_id, poster=poster, body=body, created="")


def ticket(ticket_id, closed_on, entries, subject="ZIPIT not received", fields=None):
    return SimpleNamespace(ticket_id=ticket_id, number=str(100000 + ticket_id), help_topic="Failed Transactions / ZIPIT",
                           subject=subject, closed_on=f"{closed_on} 10:00:00", fields=fields or {}, entries=entries,
                           escalation_marker=MARKER)


class FakeOsTicket:
    def __init__(self, tickets, reopened=()):
        self.tickets = tickets
        self.reopened = set(reopened)
        self.threads_read = 0

    def closed_tickets(self, since):
        return [t for t in self.tickets if t.closed_on[:10] >= since.isoformat()]

    def load_thread(self, t):
        self.threads_read += 1
        return t

    def reopen_states(self, ids):
        return {i: {"reopened": "2026-10-01 09:00:00" if i in self.reopened else None, "status_state": "closed"}
                for i in ids}


def owner_message(name="Rudo Chikore"):
    return entry(1, "M", f"Dear Team,\nClient Tendai Moyo, account 1234567890123, mobile 0772123456 sent a ZIPIT of "
                         f"150.00 that was not received.\nKind regards\n{name}\nAvondale branch", name)


def staff_resolved(ticket_id=1, closed_on="2026-09-20", note="Postilion shows response code 91, issuer down; "
                                                              "reversal 0420 posted."):
    return ticket(ticket_id, closed_on, [
        owner_message(),
        entry(2, "N", note, "Farai Ndlovu", staff_id=7),
        entry(3, "R", "Good morning Rudo,\nThe ZIPIT was reversed; Tendai can check the balance and try again.\n"
                      "Thanks\nFarai", "Farai Ndlovu", staff_id=7),
    ])


TRANSCRIPT = [
    {"role": "user", "content": "<ticket>...</ticket>"},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "a", "name": "log_errors", "input": {"mode": "top"}},
        {"type": "tool_use", "id": "b", "name": "log_search", "input": {"key": "<NUMBER_1>"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": json.dumps({"signatures": [{"signature": "aaaaaaaaaaaa"}]})},
        {"type": "tool_result", "tool_use_id": "b",
         "content": json.dumps({"events": [{"signature": "3f9a0c1d2e4b", "message": "timeout"}]})}]},
]


def record(audit, ticket_id, action="reply_and_close", executed=("reply", "close"), reopened_at=None, **kw):
    row = dict(run_id="r1", mode="live", ticket_id=ticket_id, ticket_number=str(100000 + ticket_id),
               proposed_action=action, final_action=action, confidence=0.9, category="ZIPIT reversed",
               reason="Postilion shows the ZIPIT for <NUMBER_1> was reversed (0420) the same day.",
               reply_text="The ZIPIT was reversed. Tendai Moyo can try again.",
               actual_reply="The ZIPIT was reversed. Tendai Moyo can try again.",
               executed=list(executed), transcript=TRANSCRIPT, reopened_at=reopened_at,
               facts={"confirmed": {"channel": "ZIPIT", "status": "reversed", "code": "91"}})
    row.update(kw)
    return audit.record(**row)


@pytest.fixture
def audit(tmp_path):
    log = AuditLog(tmp_path / "state.db")
    yield log
    log.close()


def build(audit, tickets, reopened=(), full=False):
    store = CaseStore(audit.db, "failed_txn")
    os_ = FakeOsTicket(tickets, reopened)
    counts = CaseBuilder(store, audit.db, os_, AGENT, today=TODAY).build(full=full)
    return store, counts, os_


def only_case(store):
    rows = store.db.execute("SELECT * FROM cases").fetchall()
    assert len(rows) == 1
    tags = {(r["kind"], r["value"]) for r in store.db.execute("SELECT kind, value FROM case_tags")}
    return rows[0], tags


# ---- building -------------------------------------------------------------------------------------------
def test_a_ticket_staff_resolved_becomes_a_masked_staff_case(audit):
    store, counts, _ = build(audit, [staff_resolved()])
    assert counts["added"] == 1
    case, tags = only_case(store)
    assert case["source"] == "staff" and case["closed_on"] == "2026-09-20"
    text = " ".join((case["asked"], case["found"], case["resolution"]))
    for gone in ("Rudo", "Farai", "Chikore", "1234567890123", "0772123456", "Avondale"):
        assert gone not in text
    assert "Client [CUSTOMER], account [NUMBER]" in case["asked"]  # named in the text by nobody listed: the cue
    # ("Client ...") finds the name, and it is masked again where it appears alone in a later field
    assert case["resolution"] == "The ZIPIT was reversed; [CUSTOMER] can check the balance and try again."
    assert "response code 91" in case["found"]
    assert ("code", "91") in tags and ("action", "staff_resolved") in tags
    assert not TOKEN_RE.search(text)


def test_a_name_in_a_form_field_is_masked(audit):
    t = staff_resolved()
    t.fields = {"Client name": "Tendai Moyo", "Channel": "ZIPIT"}
    store, _, _ = build(audit, [t])
    case, _ = only_case(store)
    assert "Tendai" not in case["asked"] + case["resolution"]
    assert "[CUSTOMER]" in case["resolution"]


def test_a_ticket_the_agent_closed_becomes_an_agent_case_with_its_tags(audit):
    t = ticket(2, "2026-09-10", [owner_message(), entry(2, "R", "The ZIPIT was reversed.", "Agent", staff_id=AGENT)],
               fields={"Client name": "Tendai Moyo"})
    record(audit, 2)
    store, counts, _ = build(audit, [t])
    case, tags = only_case(store)
    assert case["source"] == "agent" and case["action"] == "reply_and_close"
    assert case["found"] == "Postilion shows the ZIPIT for [NUMBER] was reversed (0420) the same day."
    assert case["resolution"] == "The ZIPIT was reversed. [CUSTOMER] can try again."
    assert {("channel", "zipit"), ("reading", "reversed"), ("code", "91"), ("signature", "3f9a0c1d2e4b"),
            ("action", "reply_and_close"), ("action", "zipit reversed")} <= tags
    assert ("signature", "aaaaaaaaaaaa") not in tags  # log_errors is service-wide, not this client's trail


def test_an_escalation_a_person_resolved_keeps_what_the_agent_found(audit):
    t = staff_resolved(3)
    t.entries.insert(1, entry(9, "N", f"{MARKER} Findings: reversal pending.", "Agent", staff_id=AGENT))
    record(audit, 3, action="escalate", executed=("note",), reason="Reversal for <NUMBER_1> still pending at the switch.")
    store, _, _ = build(audit, [t])
    case, tags = only_case(store)
    assert case["source"] == "staff"
    assert case["found"] == "Reversal for [NUMBER] still pending at the switch."
    assert ("signature", "3f9a0c1d2e4b") in tags and ("action", "staff_resolved") in tags


def test_a_wrong_escalation_keeps_the_staff_case_but_not_the_agents_reading(audit):
    t = staff_resolved(4)
    decision = record(audit, 4, action="escalate", executed=("note",), reason="Thought it was a limit (61).")
    audit.add_review(decision, "reviewer", "incorrect", "reply_and_close", "it was an issuer outage")
    store, _, _ = build(audit, [t])
    case, tags = only_case(store)
    assert "limit" not in case["found"] and "response code 91" in case["found"]
    assert ("signature", "3f9a0c1d2e4b") not in tags


def test_tickets_that_teach_nothing_are_skipped(audit):
    unsettled = staff_resolved(5, closed_on="2026-10-20")
    silent = ticket(6, "2026-09-01", [owner_message()])
    shadow_only = ticket(7, "2026-09-01", [owner_message()])
    record(audit, 7, mode="shadow", executed=())
    store, counts, _ = build(audit, [unsettled, silent, shadow_only])
    assert counts["not_settled"] == 1 and counts["skipped"] == 2
    assert store.counts() == {}


# ---- re-checking ----------------------------------------------------------------------------------------
def test_a_rebuild_reads_no_thread_again_and_changes_nothing(audit):
    store, _, _ = build(audit, [staff_resolved()])
    store2, counts, os_ = build(audit, [staff_resolved()])
    assert os_.threads_read == 0
    assert counts["added"] == 0 and counts["suppressed"] == 0
    assert store2.counts() == {"staff:active": 1}


def test_a_reopened_ticket_is_never_added_and_a_stored_one_is_suppressed(audit):
    store, counts, _ = build(audit, [staff_resolved(1)], reopened={1})
    assert counts["skipped"] == 1 and store.counts() == {}
    build(audit, [staff_resolved(1)])
    store, counts, _ = build(audit, [staff_resolved(1)], reopened={1})
    assert counts["suppressed"] == 1 and store.counts() == {"staff:suppressed": 1}


def test_an_agent_close_reviewed_incorrect_is_suppressed(audit):
    t = ticket(2, "2026-09-10", [owner_message()])
    decision = record(audit, 2)
    build(audit, [t])
    audit.add_review(decision, "reviewer", "incorrect", "escalate", "reversal had failed")
    store, counts, _ = build(audit, [t])
    assert counts["suppressed"] == 1
    assert store.db.execute("SELECT suppressed_reason FROM cases").fetchone()[0] == "decision reviewed incorrect"


def test_an_admins_suppression_survives_a_full_rebuild(audit):
    store, _, _ = build(audit, [staff_resolved()])
    case_id = store.db.execute("SELECT id FROM cases").fetchone()[0]
    store.suppress(case_id, "admin1", "a third party's name got through")
    store, counts, _ = build(audit, [staff_resolved()], full=True)
    assert counts["updated"] == 1
    assert store.counts() == {"staff:suppressed": 1}
    with pytest.raises(ValueError):
        store.suppress(case_id, "AUTO", "x")
    with pytest.raises(LookupError):
        store.suppress(999, "admin1", "x")


# ---- searching ------------------------------------------------------------------------------------------
def add(store, ticket_id, closed_on, tags=(), asked="ZIPIT not received", resolution="Reversed."):
    store.save(CaseDraft(source="staff", ticket_id=ticket_id, source_ticket=str(ticket_id), topic="ZIPIT",
                         closed_on=closed_on, asked=asked, resolution=resolution, tags=set(tags)))


@pytest.fixture
def store(audit):
    return CaseStore(audit.db, "failed_txn")


def ids(results):
    return [r["case"] for r in results]


def test_a_signature_match_outranks_a_better_worded_case(store):
    add(store, 1, "2026-10-01", asked="ZIPIT not received, ZIPIT failed, ZIPIT timeout")
    add(store, 2, "2026-10-01", tags={("signature", "3f9a0c1d2e4b")}, asked="Money did not arrive")
    results = store.search(["zipit"], {"signature": "3f9a0c1d2e4b"}, today=TODAY)
    assert ids(results) == ["C2", "C1"]
    assert results[0]["matched"] == ["signature"] and results[1]["matched"] == ["words"]


def test_newer_cases_rank_higher_and_old_ones_drop_out(store):
    add(store, 1, "2026-04-01", tags={("code", "91")})
    add(store, 2, "2026-10-01", tags={("code", "91")})
    add(store, 3, "2025-09-01", tags={("code", "91")})
    assert ids(store.search(tags={"code": "91"}, today=TODAY)) == ["C2", "C1"]


def test_cutoffs_leave_out_what_came_before_a_change(store):
    add(store, 1, "2026-08-01", tags={("channel", "ecocash")})
    add(store, 2, "2026-08-01", tags={("channel", "zipit")})
    add(store, 3, "2026-10-01", tags={("channel", "ecocash")})
    store.add_cutoff(date(2026, 9, 2), "EcoCash API v3 live", "admin1", "channel", "EcoCash")
    assert ids(store.search(["zipit"], today=TODAY)) == ["C3", "C2"]
    store.add_cutoff(date(2026, 9, 1), "new switch", "admin1")
    assert ids(store.search(["zipit"], today=TODAY)) == ["C3"]
    with pytest.raises(ValueError):
        store.add_cutoff(date(2026, 9, 1), "x", "admin1", "code", "1234567890")


def test_suppressed_cases_are_never_offered(store):
    add(store, 1, "2026-10-01", tags={("code", "91")})
    store.suppress(1, "admin1", "misleading")
    assert store.search(["zipit"], {"code": "91"}, today=TODAY) == []


def test_search_ignores_anything_that_could_find_a_customer(store):
    add(store, 1, "2026-10-01", asked="Account [NUMBER] ZIPIT failed")
    assert store.search(["1234567890"], today=TODAY) == []
    assert store.search(tags={"code": "<NUMBER_1>"}, today=TODAY) == []
    assert store.search(["zipit", "OR", '"x'], today=TODAY)[0]["case"] == "C1"
    assert clean_tag("code", "12345678") is None and clean_tag("bogus", "91") is None
    assert clean_tag("channel", " ZIPIT  Transfer ") == "zipit transfer"


def test_what_the_model_reads_has_no_ticket_number(store):
    add(store, 1, "2026-10-01", tags={("code", "91")})
    view = store.search(tags={"code": "91"}, today=TODAY)[0]
    assert set(view) == {"case", "source", "closed", "topic", "matched", "what_was_asked", "what_was_found",
                         "how_it_was_resolved", "outcome", "score"}
    assert "1" not in (view["closed"], view["topic"])


def test_log_signatures_come_only_from_log_search():
    assert log_signatures(TRANSCRIPT) == ["3f9a0c1d2e4b"]
    assert log_signatures(json.dumps(TRANSCRIPT)) == ["3f9a0c1d2e4b"]
    assert log_signatures(None) == []
