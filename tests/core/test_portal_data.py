"""The console's read models: a reply held for approval is not counted as an outcome."""
from __future__ import annotations

from agentcore.audit import AuditLog
from agentcore.portal import data


def test_a_held_reply_is_waiting_not_closed(tmp_path):
    audit = AuditLog(tmp_path / "log.sqlite")
    base = dict(run_id="r1", mode="live", ticket_id=1, ticket_number="100001", proposed_action="reply_and_close",
                final_action="reply_and_close", confidence=0.9, category="x")
    held = audit.record(**base, executed=["awaiting approval"], approval="waiting: beyond the standard reply")
    audit.record(**{**base, "ticket_id": 2, "ticket_number": "100002"}, executed=["reply", "close"])
    audit.record(**{**base, "confidence": 1.0, "category": "approved_reply"}, executed=["reply", "close"])
    o = data.overview(audit.db, 14, 40)
    k = o["kpis"]
    assert k["decided"] == 3 and k["held"] == 1 and k["closed"] == 2
    assert k["avg_confidence"] == 0.9  # approved replies (confidence 1.0) are not the model's confidence
    assert sum(day["reply_and_close"] for day in o["by_day"]) == 2
    recent = {r["id"]: r for r in o["recent"]}
    assert recent[held]["held"] is True and sum(r["held"] for r in recent.values()) == 1
    assert [r["id"] for r in data.decisions(audit.db, {"action": "held"})["items"]] == [held]
    assert held not in [r["id"] for r in data.decisions(audit.db, {"action": "reply_and_close"})["items"]]
    audit.close()


def test_a_held_reply_stops_waiting_once_a_person_acts_on_it(tmp_path):
    from agentcore.approval import ApprovalStore

    audit = AuditLog(tmp_path / "log.sqlite")
    held = audit.record(run_id="r1", mode="live", ticket_id=1, ticket_number="100001", proposed_action="request_info",
                        final_action="request_info", confidence=0.9, category="x", executed=["awaiting approval"])
    store = ApprovalStore(audit.db)
    aid = store.create(decision_id=held, ticket_id=1, ticket_number="100001", last_entry_id=1, action="request_info",
                       why="beyond", reply_masked="r", reply_text="r", token_map={})
    assert data.overview(audit.db, 14, 40)["kpis"]["held"] == 1
    store.finish(aid, "posted", "reply, answered")
    o = data.overview(audit.db, 14, 40)
    row = o["recent"][0]
    assert o["kpis"]["held"] == 0 and row["held"] is False and row["approval"] == "posted"
    assert o["kpis"]["request_info"] == 0  # the posted reply is recorded (and counted) as its own decision
    assert data.decisions(audit.db, {"action": "held"})["total"] == 0
    audit.close()
