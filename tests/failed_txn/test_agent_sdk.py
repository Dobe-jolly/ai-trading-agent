"""The desks through the Claude Agent SDK (a subscription token instead of an API key). The SDK client is replaced by
a scripted fake that calls the real tool handlers, so the masking, lookups, guardrails and log are the real ones.
All data invented."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

import claude_agent_sdk  # noqa: E402
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage  # noqa: E402

from agentcore import sdk_backend  # noqa: E402
from agentcore.loop import NUDGE, AgentError  # noqa: E402
from agentcore.sdk_backend import SdkClient, backend_problems, build_client, has_model, wants_sdk  # noqa: E402
from failed_txn.loop import TicketAgent  # noqa: E402
from tests.failed_txn.conftest import ACCOUNT, decide  # noqa: E402
from tests.failed_txn.test_runner import CONFIRM, DECLINED_REPLY, SEARCH, build, seed  # noqa: E402

TOKEN = "sk-ant-oat01-test-only"


def tool_input(response):
    block = response.content[0]
    return block.name, block.input


class FakeSdk:
    """Stands in for ClaudeSDKClient: each query() plays one scripted list of turns against the real handlers."""
    scripts: list = []
    made: list = []
    tools: dict = {}

    def __init__(self, options):
        self.options = options
        self.queries = []
        self.script = FakeSdk.scripts.pop(0)
        FakeSdk.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        turns = self.script.pop(0)
        if isinstance(turns, ResultMessage):
            yield turns
            return
        for n, turn in enumerate(turns):
            if isinstance(turn, str):
                yield AssistantMessage([TextBlock(turn)], model="m", message_id=f"t{n}", stop_reason="end_turn")
                continue
            name, args = turn
            use_id = f"u{len(self.queries)}_{n}"
            yield AssistantMessage([ToolUseBlock(use_id, f"mcp__desk__{name}", args)], model="m",
                                   message_id=f"t{n}", stop_reason="tool_use")
            out = await FakeSdk.tools[name].handler(args)
            yield UserMessage([ToolResultBlock(use_id, out["content"], out.get("is_error"))])
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=len(turns),
                            session_id="s", usage={"input_tokens": 1000, "output_tokens": 50})


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeSdk.scripts, FakeSdk.made, FakeSdk.tools = [], [], {}
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeSdk)
    real = sdk_backend.make_tool

    def recording(schema, handler):
        made = real(schema, handler)
        FakeSdk.tools[schema["name"]] = made
        return made
    monkeypatch.setattr(sdk_backend, "make_tool", recording)
    return FakeSdk


def sdk_settings(settings, tmp_path):
    return replace(settings, anthropic_api_key=None, claude_code_oauth_token=TOKEN, model_backend="auto",
                   state_db=tmp_path / "failed_txn" / "agent.sqlite")


def with_sdk(runner, s):
    client = build_client(s)
    runner.agent._client = client
    return client


def test_a_token_without_a_key_chooses_the_agent_sdk(settings, tmp_path):
    s = sdk_settings(settings, tmp_path)
    assert wants_sdk(s) and has_model(s) and isinstance(build_client(s), SdkClient)
    assert not backend_problems(s, need_model=True)
    keyed = replace(s, anthropic_api_key="k")
    assert not wants_sdk(keyed) and not isinstance(build_client(keyed), SdkClient)
    forced = replace(keyed, model_backend="agent_sdk")
    assert wants_sdk(forced)
    assert backend_problems(replace(s, claude_code_oauth_token=None), need_model=True)
    assert backend_problems(replace(s, model_backend="cloud"), need_model=False)


def test_a_pass_through_the_sdk_uses_the_same_lookups_masking_and_log(settings, mb, tmp_path, fake_sdk):
    seed(mb)
    s = sdk_settings(settings, tmp_path)
    s.state_db.parent.mkdir(parents=True)
    runner, audit = build(s, mb, None)
    client = with_sdk(runner, s)
    fake_sdk.scripts.append([[tool_input(SEARCH), tool_input(CONFIRM),
                              tool_input(decide("reply_and_close", DECLINED_REPLY, "declined", standard=True)), "done"]])
    summary = runner.run()

    assert summary.actions == {"reply_and_close": 1} and not mb.writes
    fake = fake_sdk.made[0]
    first = fake.queries[0]
    assert first.startswith("<ticket>") and "<NUMBER_1>" in first and ACCOUNT not in first
    o = fake.options
    assert o.tools == [] and o.permission_mode == "dontAsk" and o.setting_sources == [] and o.strict_mcp_config
    assert sorted(o.allowed_tools) == sorted(f"mcp__desk__{t['name']}" for t in runner.agent._tools)
    assert o.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and o.env["ENABLE_TOOL_SEARCH"] == "false"
    assert o.env["CLAUDE_CONFIG_DIR"].startswith(str(client.work_dir)) and o.system_prompt["type"] == "file"
    assert "no-session-persistence" in o.extra_args
    assert not list((client.work_dir / "prompts").iterdir())  # the prompt file is removed after the ticket

    row = audit.last_for_ticket(1)
    assert row["final_action"] == "reply_and_close" and row["input_tokens"] == 1000 and row["output_tokens"] == 50
    transcript = json.loads(row["transcript"])
    names = [b["name"] for m in transcript if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
    assert names == ["find_switch_transactions", "confirm_transaction", "submit_decision"]
    results = [b for m in transcript if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"]]
    assert "declined" in results[0]["content"] and ACCOUNT not in json.dumps(dict(row))
    assert json.loads(row["account_statuses"]) == {"transaction": "declined"}


def test_the_agent_is_nudged_once_when_it_stops_without_a_decision(settings, mb, tmp_path, fake_sdk):
    seed(mb)
    s = sdk_settings(settings, tmp_path)
    runner, audit = build(s, mb, None)
    with_sdk(runner, s)
    fake_sdk.scripts.append([["I think the transfer was declined."],
                             [tool_input(decide("escalate", "", "unclear", conf=0.9)), "done"]])
    runner.run()
    assert fake_sdk.made[0].queries[1] == NUDGE
    assert audit.last_for_ticket(1)["final_action"] == "escalate"


def test_a_failed_sdk_run_is_a_model_error(settings, mb, tmp_path, fake_sdk):
    seed(mb)
    s = sdk_settings(settings, tmp_path)
    runner, audit = build(s, mb, None)
    with_sdk(runner, s)
    fake_sdk.scripts.append([ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
                                           num_turns=1, session_id="s", result="Invalid bearer token",
                                           api_error_status=401)])
    agent: TicketAgent = runner.agent
    ticket = runner.on_request("200001")[0]
    runner.store.load_thread(ticket)
    with pytest.raises(AgentError, match="Invalid bearer token"):
        agent.decide(ticket)


def test_one_shot_calls_keep_the_messages_create_shape(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        if options.allowed_tools:
            out = await FakeSdk.tools["submit_triage"].handler({"kind": "sms_alerts", "confidence": 0.9, "reason": "r"})
            assert out["content"][0]["text"].startswith("Recorded")
        else:
            yield AssistantMessage([TextBlock("OK")], model="m")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", usage={"input_tokens": 9, "output_tokens": 1})

    FakeSdk.tools = {}
    real = sdk_backend.make_tool
    monkeypatch.setattr(sdk_backend, "make_tool", lambda schema, h: FakeSdk.tools.setdefault(schema["name"], real(schema, h)))
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    client = SdkClient(TOKEN, tmp_path / "sdk")

    reply = client.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "Reply with OK"}])
    assert reply.content[0].text == "OK" and reply.usage.output_tokens == 1

    tool = {"name": "submit_triage", "description": "d", "input_schema": {"type": "object", "properties": {}}}
    reply = client.messages.create(model="m", max_tokens=300, system=[{"type": "text", "text": "Sort tickets."}],
                                   tools=[tool], tool_choice={"type": "tool", "name": "submit_triage"},
                                   messages=[{"role": "user", "content": "<ticket>x</ticket>"}])
    block = reply.content[0]
    assert block.type == "tool_use" and block.name == "submit_triage" and block.input["kind"] == "sms_alerts"
    assert "Answer only by calling the submit_triage tool" in seen["prompt"]
    assert seen["options"].allowed_tools == ["mcp__desk__submit_triage"] and seen["options"].max_turns == 2
    assert Path(seen["options"].system_prompt["path"]).parent == tmp_path / "sdk" / "prompts"
