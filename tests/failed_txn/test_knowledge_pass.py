"""Staff notes on a real desk pass, and trying a draft note on one ticket: the failed-transactions desk with a
scripted model and the fake Metabase. All data invented."""
from __future__ import annotations

import json

from agentcore.knowledge import Draft, Item, KnowledgeStore
from agentcore.trial import run_trial
from tests.failed_txn.conftest import ACCOUNT, POSTOFFICE_DB, ScriptedModel, call, decide
from tests.failed_txn.test_runner import CONFIRM, DECLINED_REPLY, SEARCH, build, seed

SQL = ("SELECT TOP 20 datetime_req, message_type, rsp_code_rsp FROM post_tran_leg WITH (NOLOCK) "
       "WHERE from_account_id = {{account}} ORDER BY datetime_req DESC")
NOTE = Draft(title="Response 91 means the switch timed out", body="When Postilion shows response 91 the issuer did "
             "not answer in time; the client may have been debited, so check core banking before replying.",
             query_db="POST OFFICE", query_sql=SQL, query_slots={"account": "account"},
             query_about="The latest switch legs for the client's account with their response codes, newest first.")


def with_bound_queries(mb):
    calls = []

    def query_bound(db, sql, params):
        calls.append((db, sql, params))
        return [{"datetime_req": "2026-10-12 10:00:00", "message_type": "0200", "rsp_code_rsp": "51"}]
    mb.query_bound = query_bound
    return calls


def cite(note_id):
    response = decide("reply_and_close", DECLINED_REPLY, "declined", standard=True)
    response.content[0].input["evidence"] = [f"K{note_id}: checked the legs", "test evidence"]
    return response


def test_a_switched_on_note_reaches_the_model_and_the_decision_records_it(settings, mb):
    seed(mb)
    calls = with_bound_queries(mb)
    runner, audit = build(settings, mb, None)
    store = KnowledgeStore(audit.db)
    note = store.propose(NOTE, "viewer1", source_ticket="200001")
    store.decide(note["id"], "approve", "dobe", "admin")
    tool = note["tool_name"]
    model = ScriptedModel(SEARCH, CONFIRM, call(tool, {"account": "<NUMBER_1>"}, "k"), cite(note["id"]))
    runner.agent._client = model
    runner.run()

    first = model.calls[0]
    assert f"[K{note['id']}] Response 91 means the switch timed out" in first["system"][1]["text"]
    assert tool in [t["name"] for t in first["tools"]]
    assert calls and calls[0][2] == {"account": ("text", ACCOUNT)} and ACCOUNT not in calls[0][1]
    row = audit.last_for_ticket(1)
    assert json.loads(row["knowledge_active"]) == [note["id"]] and json.loads(row["knowledge_used"]) == [note["id"]]
    assert row["prompt_version"] == runner.agent.prompt_version and len(row["prompt_version"]) == 12
    assert store.listing()[0]["uses"] == 1


def test_trying_a_draft_on_one_ticket_writes_nothing(settings, mb):
    seed(mb)
    calls = with_bound_queries(mb)
    model = ScriptedModel(SEARCH, CONFIRM, call("k0_draft_lookup", {"account": "<NUMBER_1>"}, "k"),
                          decide("escalate", "", "uncertain", conf=0.9))
    runner, audit = build(settings, mb, model)
    before = audit.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    result = run_trial(runner, "200001", Item.from_draft(NOTE, "viewer1", "200001"))

    assert result["final_action"] == "escalate" and result["draft_used"] is True
    assert "k0_draft_lookup" in result["tools_used"]
    assert calls[0][0] == POSTOFFICE_DB and calls[0][2] == {"account": ("text", ACCOUNT)}
    assert "[DRAFT] Response 91" in model.calls[0]["system"][1]["text"]
    assert audit.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == before and not mb.writes
    assert ACCOUNT not in json.dumps(result)
