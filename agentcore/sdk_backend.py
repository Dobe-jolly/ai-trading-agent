"""Claude through the Claude Agent SDK, for a Claude subscription token (CLAUDE_CODE_OAUTH_TOKEN, from
`claude setup-token`) instead of an API key.

The desks normally call the Claude API with ANTHROPIC_API_KEY and run their own tool loop (agentcore.loop). An API
key is the right setup for the service; until there is one, MODEL_BACKEND=agent_sdk (or auto, with a token and no
key) runs the same agent through the Agent SDK, which drives Claude Code's engine with the subscription:

- The desk's lookups and submit_decision become in-process MCP tools whose handlers call the same toolbox, so
  masking, provenance checks and knowledge lookups are unchanged. Claude Code's own tools (files, shell, web) are
  all switched off; only these tools are allowed. User settings, CLAUDE.md files, memory and tool search are off,
  and sessions are not saved.
- The guardrails, approvals, modes and the decision log are untouched: they act on the decision as before.
- One-shot calls (SMS triage, attachment reading, anomaly summaries, the model ping) go through a small
  messages.create look-alike (SdkClient.messages).

Needs `pip install claude-agent-sdk` (Python 3.10 or newer; the package bundles the Claude Code binary).
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

log = logging.getLogger(__name__)

SERVER = "desk"
PREFIX = f"mcp__{SERVER}__"
BACKENDS = ("auto", "api", "agent_sdk")
DEFAULT_TIMEOUT = 600.0  # one ticket, all turns


class SdkError(RuntimeError):
    """The Agent SDK could not get an answer (not installed, not signed in, rate limited, timed out...)."""


# ---- choosing the backend -------------------------------------------------------------------------------------
def sdk_installed() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


def wants_sdk(s: Any) -> bool:
    backend = (getattr(s, "model_backend", None) or "auto").lower()
    if backend == "agent_sdk":
        return True
    if backend == "api":
        return False
    # auto: only route through the Agent SDK when the package is actually installed
    return (sdk_installed() and not getattr(s, "anthropic_api_key", None)
            and bool(getattr(s, "claude_code_oauth_token", None)))


def has_model(s: Any) -> bool:
    if wants_sdk(s):
        return bool(getattr(s, "claude_code_oauth_token", None))
    return bool(getattr(s, "anthropic_api_key", None)) or bool(getattr(s, "claude_code_oauth_token", None))


def backend_problems(s: Any, need_model: bool) -> list[str]:
    backend = (getattr(s, "model_backend", None) or "auto").lower()
    if backend not in BACKENDS:
        return [f"MODEL_BACKEND must be one of {', '.join(BACKENDS)}, got {backend!r}"]
    if not need_model:
        return []
    if wants_sdk(s):
        out = []
        if not getattr(s, "claude_code_oauth_token", None):
            out.append("MODEL_BACKEND=agent_sdk needs CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token`)")
        if not sdk_installed():
            out.append("The Agent SDK is not installed: pip install claude-agent-sdk (Python 3.10 or newer)")
        return out
    # API path: accepts an API key or an OAuth token (used as a Bearer token)
    if not getattr(s, "anthropic_api_key", None) and not getattr(s, "claude_code_oauth_token", None):
        return ["Set ANTHROPIC_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN to use a Claude subscription through the Agent SDK"]
    return []


def build_client(s: Any, max_retries: int = 3) -> Any:
    """The model client for a desk: the Claude API with a key, or the Agent SDK with a subscription token."""
    if wants_sdk(s):
        state = Path(getattr(s, "state_db", "state/agent.sqlite")).resolve()
        return SdkClient(getattr(s, "claude_code_oauth_token", None), state.parent.parent / "claude-sdk",
                         timeout=float(getattr(s, "model_timeout_seconds", None) or DEFAULT_TIMEOUT))
    import anthropic

    api_key = getattr(s, "anthropic_api_key", None)
    if api_key:
        return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
    oauth_token = getattr(s, "claude_code_oauth_token", None)
    if oauth_token:
        return anthropic.Anthropic(auth_token=oauth_token, max_retries=max_retries)
    return anthropic.Anthropic(api_key=None, max_retries=max_retries)


def is_sdk(client: Any) -> bool:
    return bool(getattr(client, "agent_sdk", False))


# ---- the client ------------------------------------------------------------------------------------------------
class SdkClient:
    agent_sdk = True

    def __init__(self, token: Optional[str], work_dir: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.token = token
        self.work_dir = Path(work_dir)
        self.timeout = timeout
        self.messages = _Messages(self)
        # An empty ANTHROPIC_API_KEY (an unfilled line in .env) would outrank the token in Claude Code.
        if os.environ.get("ANTHROPIC_API_KEY", None) == "":
            del os.environ["ANTHROPIC_API_KEY"]

    def env(self, max_tokens: Optional[int]) -> dict[str, str]:
        env = {
            "CLAUDE_CONFIG_DIR": str(self.work_dir / "config"),  # not the user's own ~/.claude
            "ENABLE_TOOL_SEARCH": "false",                         # few tools: give the model every schema
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        }
        if self.token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token
        if max_tokens and max_tokens >= 256:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_tokens))
        return env

    def options(self, *, model: str, system_file: Path, tools: list[Any], max_turns: int,
                max_tokens: Optional[int]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        names = [f"{PREFIX}{t.name}" for t in tools]
        wanted: dict[str, Any] = {
            "model": model,
            "system_prompt": {"type": "file", "path": str(system_file)},  # long prompts: Windows limits the command line
            "tools": [],                      # none of Claude Code's own tools
            "mcp_servers": {SERVER: create_sdk_mcp_server(name=SERVER, tools=tools)} if tools else {},
            "allowed_tools": names,
            "strict_mcp_config": True,
            "permission_mode": "dontAsk",     # anything not allowed above is refused, never prompted
            "setting_sources": [],
            "max_turns": max_turns,
            "cwd": str(self.work_dir / "cwd"),
            "env": self.env(max_tokens),
            "thinking": {"type": "disabled"},  # the API path does not ask for thinking either
            "extra_args": {"no-session-persistence": None},
        }
        known = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
        dropped = sorted(set(wanted) - known)
        if dropped:
            log.debug("Agent SDK options not supported by this version: %s", ", ".join(dropped))
        return ClaudeAgentOptions(**{k: v for k, v in wanted.items() if k in known})

    def prepare(self) -> None:
        for sub in ("config", "cwd", "prompts"):
            (self.work_dir / sub).mkdir(parents=True, exist_ok=True)

    def system_file(self, text: str) -> Path:
        self.prepare()
        fd, path = tempfile.mkstemp(prefix="system-", suffix=".md", dir=self.work_dir / "prompts")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return Path(path)

    def run(self, make_coro: Any) -> Any:
        """Run one async job to the end, from sync code (a pass, the console's trial thread)."""
        async def bounded() -> Any:
            return await asyncio.wait_for(make_coro(), timeout=self.timeout)

        try:
            return asyncio.run(bounded())
        except asyncio.TimeoutError:
            raise SdkError(f"no answer within {int(self.timeout)} s") from None
        except SdkError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK and CLI errors, in words for the log and the ticket
            raise SdkError(f"{type(exc).__name__}: {str(exc)[:400]}") from exc


def system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(str(b.get("text", "")) for b in system or [] if isinstance(b, dict))


def strip(name: str) -> str:
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")


def _result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": bool(is_error)}


def make_tool(schema: dict[str, Any], handler: Any) -> Any:
    from claude_agent_sdk import SdkMcpTool

    return SdkMcpTool(name=schema["name"], description=schema.get("description", ""),
                      input_schema=schema.get("input_schema") or {"type": "object", "properties": {}},
                      handler=handler)


class Transcript:
    """The SDK's messages in the shape agentcore.loop keeps (the console reads lookups from it)."""

    def __init__(self, first: str) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
        self._last_id: Optional[str] = None
        self.input_tokens = self.output_tokens = self.turns = 0
        self.stopped: Optional[str] = None   # max_tokens / refusal
        self.error: Optional[str] = None     # the run failed (auth, rate limit, API error)
        self.max_turns_hit = False

    def add(self, message: Any) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

        if isinstance(message, AssistantMessage):
            blocks = []
            for b in message.content:
                if isinstance(b, TextBlock) and b.text:
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    blocks.append({"type": "tool_use", "id": b.id, "name": strip(b.name), "input": b.input})
            msg_id = getattr(message, "message_id", None)
            if blocks:
                if msg_id and msg_id == self._last_id and self.messages[-1]["role"] == "assistant":
                    self.messages[-1]["content"].extend(blocks)
                else:
                    self.messages.append({"role": "assistant", "content": blocks})
                self._last_id = msg_id
            if getattr(message, "stop_reason", None) in ("max_tokens", "refusal"):
                self.stopped = message.stop_reason
            if getattr(message, "error", None):
                self.error = str(message.error)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            results = [{"type": "tool_result", "tool_use_id": b.tool_use_id, "content": _text(b.content),
                        "is_error": bool(b.is_error)} for b in message.content if isinstance(b, ToolResultBlock)]
            if results:
                self.messages.append({"role": "user", "content": results})
                self._last_id = None
        elif isinstance(message, ResultMessage):
            usage = message.usage or {}
            # Claude Code caches most of the prompt itself; count the cached part too, or a ticket looks like 6 tokens.
            self.input_tokens += sum(int(usage.get(k) or 0) for k in
                                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            self.output_tokens += int(usage.get("output_tokens") or 0)
            self.turns += int(message.num_turns or 0)
            if message.subtype == "error_max_turns":
                self.max_turns_hit = True
            elif message.is_error:
                detail = message.result or "; ".join(str(e) for e in (message.errors or [])) or message.subtype
                status = f" (HTTP {message.api_error_status})" if getattr(message, "api_error_status", None) else ""
                self.error = f"{detail}{status}"[:400]


# ---- the ticket agent ---------------------------------------------------------------------------------------------
DONE = "Decision recorded. You are finished: reply with the single word done and nothing else."


def decide(agent: Any, ticket: Any, as_of: Optional[str], first: str, pseudo: Any, result: Any, nudge: str) -> None:
    """agentcore.loop.TicketAgent.decide for the Agent SDK: fills result (decision, transcript, tokens)."""
    client: SdkClient = agent._client
    state: dict[str, Any] = {"decision": None, "lock": None}  # the lock is made inside the event loop
    decision_name = agent._decision_tool["name"]

    def handler_for(name: str) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            async with state["lock"]:
                args = dict(args or {})
                if name == decision_name:
                    if state["decision"] is not None:
                        return _result("A decision is already recorded. Reply with the single word done.")
                    try:
                        decision = agent.decision_type.from_tool_input(args)
                    except ValueError as exc:
                        return _result(f"Invalid decision: {exc}. Fix it and call submit_decision again.", True)
                    state["decision"] = decision
                    agent._step(f"decided: {decision.action} ({decision.confidence:.0%})")
                    return _result(DONE)
                if state["decision"] is not None:
                    return _result("A decision is already recorded; no more lookups.", True)
                shown = ", ".join(str(v) for v in args.values())
                agent._step(f"{name}({pseudo.mask(shown)[:120]})")
                loop = asyncio.get_running_loop()
                # Lookups are blocking HTTP calls to Metabase: run them off the event loop.
                output, is_error = await loop.run_in_executor(
                    None, lambda: agent._toolbox.run(name, args, pseudo, result.evidence, ticket))
                return _result(output, is_error)
        return handler

    tools = [make_tool(schema, handler_for(schema["name"])) for schema in agent._tools]
    system_file = client.system_file(system_text(agent._system))
    transcript = Transcript(first)

    async def run() -> None:
        from claude_agent_sdk import ClaudeSDKClient

        state["lock"] = asyncio.Lock()
        options = client.options(model=agent._model, system_file=system_file, tools=tools,
                                 max_turns=agent._max_turns + 1, max_tokens=agent._max_tokens)
        async with ClaudeSDKClient(options=options) as sdk:
            await sdk.query(first)
            async for message in sdk.receive_response():
                transcript.add(message)
            if state["decision"] is None and not (transcript.error or transcript.stopped or transcript.max_turns_hit):
                transcript.messages.append({"role": "user", "content": nudge})
                await sdk.query(nudge)
                async for message in sdk.receive_response():
                    transcript.add(message)

    try:
        client.run(run)
    finally:
        try:
            system_file.unlink()
        except OSError:
            pass
    result.transcript = transcript.messages
    result.input_tokens += transcript.input_tokens
    result.output_tokens += transcript.output_tokens
    result.turns = transcript.turns
    if state["decision"] is not None:
        result.decision = state["decision"]
    elif transcript.error:
        raise SdkError(transcript.error)
    elif transcript.stopped:
        from .loop import STOP_FALLBACKS

        agent._step(f"model stopped: {transcript.stopped}")
        result.decision = agent.decision_type.fallback(STOP_FALLBACKS[transcript.stopped])
    elif transcript.max_turns_hit:
        result.decision = agent.decision_type.fallback(f"No decision within {agent._max_turns} turns.")
    else:
        result.decision = agent.decision_type.fallback("Model ended without submitting a decision.")


def _has_media(messages: list[dict[str, Any]]) -> bool:
    """True when any message content block is an image or document (PDF)."""
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image", "document"):
                    return True
    return False


# ---- one-shot calls: a messages.create look-alike -------------------------------------------------------------
class _Messages:
    """client.messages.create(...) for the one-shot calls (one user message, optionally one forced tool)."""

    def __init__(self, client: SdkClient) -> None:
        self._client = client

    def create(self, *, model: str, messages: list[dict[str, Any]], max_tokens: Optional[int] = None,
               system: Any = None, tools: Optional[list[dict[str, Any]]] = None,
               tool_choice: Optional[dict[str, Any]] = None, **_: Any) -> Any:
        # Image/PDF attachment reads go through the SDK binary — the subscription handles them
        # without rate limits. Text-only calls (triage, anomaly) go through the direct API
        # where tool-forcing is reliable for simple single-message prompts.
        if _has_media(messages):
            return self._create_via_binary(model=model, messages=messages, max_tokens=max_tokens,
                                           system=system, tools=tools, tool_choice=tool_choice)
        import anthropic
        ac = anthropic.Anthropic(auth_token=self._client.token, max_retries=1)
        kw: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens:
            kw["max_tokens"] = max_tokens
        if system:
            kw["system"] = system_text(system) if not isinstance(system, str) else system
        if tools:
            kw["tools"] = tools
        if tool_choice:
            kw["tool_choice"] = tool_choice
        try:
            return ac.messages.create(**kw)
        except anthropic.APIError as exc:
            raise SdkError(f"one-shot call failed: {type(exc).__name__}: {exc}") from exc

    def _create_via_binary(self, *, model: str, messages: list[dict[str, Any]],
                           max_tokens: Optional[int] = None, system: Any = None,
                           tools: Optional[list[dict[str, Any]]] = None,
                           tool_choice: Optional[dict[str, Any]] = None) -> Any:
        """SDK binary path for media-containing one-shot calls (images, PDFs)."""
        client = self._client
        if len(messages) != 1 or messages[0].get("role") != "user":
            raise SdkError("the Agent SDK path takes one user message here")
        content = messages[0]["content"]
        forced = tool_choice.get("name") if tool_choice and tool_choice.get("type") == "tool" else None
        calls: list[Any] = []

        def capture(name: str) -> Any:
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                calls.append(SimpleNamespace(type="tool_use", id=f"sdk_{len(calls) + 1}", name=name,
                                             input=dict(args or {})))
                return _result("Recorded. Reply with the single word done.")
            return handler

        sdk_tools = [make_tool(t, capture(t["name"])) for t in tools or []]
        ask = f"\n\nAnswer only by calling the {forced} tool, once." if forced else ""
        if isinstance(content, list):
            content = list(content) + ([{"type": "text", "text": ask.strip()}] if ask else [])
        else:
            content = str(content) + ask
        system_file = client.system_file(system_text(system) or "Answer briefly.")
        texts: list[str] = []
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        stop = {"reason": "end_turn", "error": None}

        async def run() -> None:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

            options = client.options(model=model, system_file=system_file, tools=sdk_tools,
                                     max_turns=2 if sdk_tools else 1, max_tokens=max_tokens)

            async def stream() -> Any:
                yield {"type": "user", "message": {"role": "user", "content": content}}

            prompt = content if isinstance(content, str) else stream()
            async for m in query(prompt=prompt, options=options):
                if isinstance(m, AssistantMessage):
                    texts.extend(b.text for b in m.content if isinstance(b, TextBlock) and b.text)
                    if getattr(m, "error", None):
                        stop["error"] = str(m.error)
                elif isinstance(m, ResultMessage):
                    u = m.usage or {}
                    usage.input_tokens += sum(int(u.get(k) or 0) for k in
                                              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    usage.output_tokens += int(u.get("output_tokens") or 0)
                    if m.is_error and m.subtype != "error_max_turns" and not calls:
                        stop["error"] = (m.result or m.subtype or "failed")[:400]

        try:
            client.run(run)
        finally:
            try:
                system_file.unlink()
            except OSError:
                pass
        if stop["error"] and not calls:
            raise SdkError(stop["error"])
        blocks = list(calls) + ([SimpleNamespace(type="text", text="\n".join(texts))] if texts and not forced else [])
        return SimpleNamespace(content=blocks, usage=usage, stop_reason="tool_use" if calls else stop["reason"],
                               model=model)

