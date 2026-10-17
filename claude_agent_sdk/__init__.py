"""Minimal claude_agent_sdk stub — wraps the claude CLI binary that is already installed.

The real package (pip install claude-agent-sdk) is not publicly available on PyPI.
This stub provides the same public interface by driving the claude binary via subprocess,
with a local stdio MCP server bridging the desk's Python tool handlers.

The public API matches what agentcore/sdk_backend.py imports.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class TextBlock:
    text: str


@dataclasses.dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclasses.dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: Optional[bool] = None


@dataclasses.dataclass
class AssistantMessage:
    content: list
    model: str = ""
    message_id: str = ""
    stop_reason: str = "end_turn"
    error: Optional[str] = None


@dataclasses.dataclass
class UserMessage:
    content: list


@dataclasses.dataclass
class ResultMessage:
    subtype: str
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 0
    session_id: str = ""
    usage: dict = dataclasses.field(default_factory=dict)
    result: Optional[str] = None
    errors: Optional[list] = None
    api_error_status: Optional[int] = None


class SdkMcpTool:
    def __init__(self, name: str, description: str, input_schema: dict, handler: Callable):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


@dataclasses.dataclass
class ClaudeAgentOptions:
    model: str = ""
    system_prompt: Any = None       # {"type": "file", "path": str} | {"type": "text", "text": str} | str
    tools: list = dataclasses.field(default_factory=list)
    mcp_servers: dict = dataclasses.field(default_factory=dict)  # name -> _McpSetup
    allowed_tools: list = dataclasses.field(default_factory=list)
    strict_mcp_config: bool = False
    permission_mode: str = "dontAsk"
    setting_sources: list = dataclasses.field(default_factory=list)
    max_turns: int = 10
    cwd: str = ""
    env: dict = dataclasses.field(default_factory=dict)
    thinking: dict = dataclasses.field(default_factory=dict)
    extra_args: dict = dataclasses.field(default_factory=dict)


class _McpSetup:
    """Holds the MCP server name and tool list for one session."""
    def __init__(self, name: str, tools: list[SdkMcpTool]):
        self.name = name
        self.tools = {t.name: t for t in tools}


def create_sdk_mcp_server(name: str, tools: list[SdkMcpTool]) -> _McpSetup:
    return _McpSetup(name, tools)


# ---------------------------------------------------------------------------
# MCP stdio server script — spawned as a subprocess by the claude binary
# ---------------------------------------------------------------------------

_MCP_SCRIPT = r"""
import json, socket, sys

HOST, PORT, SESSION = "127.0.0.1", {port}, {session!r}
TOOLS = {tools_json}


def call_bridge(name, args):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((HOST, PORT))
        s.sendall((json.dumps({{"session": SESSION, "name": name, "args": args}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.strip())


def reply(req_id, result):
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": req_id, "result": result}}) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        req = json.loads(raw)
    except Exception:
        continue
    m, rid = req.get("method", ""), req.get("id")
    if m == "initialize":
        reply(rid, {{"protocolVersion": "2024-11-05",
                    "serverInfo": {{"name": "desk", "version": "0"}},
                    "capabilities": {{"tools": {{}}}}}})
    elif m == "tools/list":
        reply(rid, {{"tools": TOOLS}})
    elif m == "tools/call":
        n = req["params"]["name"]
        a = req["params"].get("arguments", {{}})
        r = call_bridge(n, a)
        reply(rid, {{"content": [{{"type": "text", "text": r.get("output", "")}}],
                    "isError": r.get("is_error", False)}})
    elif rid is not None and not m.startswith("notifications/"):
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": rid,
                                     "error": {{"code": -32601, "message": "Not found"}}}}) + "\n")
        sys.stdout.flush()
"""


# ---------------------------------------------------------------------------
# TCP bridge — in our async process, handles tool calls from the MCP script
# ---------------------------------------------------------------------------

class _Bridge:
    def __init__(self, tools: dict[str, SdkMcpTool], session: str):
        self._tools = tools
        self._session = session
        self._server: Optional[asyncio.Server] = None
        self.port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = await reader.read(8192)
                if not chunk:
                    break
                buf += chunk
            if not buf.strip():
                return
            req = json.loads(buf.strip())
            if req.get("session") != self._session:
                writer.write(b'{"output":"wrong session","is_error":true}\n')
            else:
                tool = self._tools.get(req["name"])
                if tool is None:
                    result = {"output": f"unknown tool: {req['name']}", "is_error": True}
                else:
                    try:
                        raw = await tool.handler(req["args"])
                        text = ""
                        for b in (raw.get("content") or []):
                            if isinstance(b, dict) and b.get("type") == "text":
                                text += b.get("text", "")
                        result = {"output": text, "is_error": bool(raw.get("is_error"))}
                    except Exception as exc:
                        result = {"output": str(exc), "is_error": True}
                writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
        finally:
            try:
                writer.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Internal: find the claude binary and build command
# ---------------------------------------------------------------------------

def _find_claude() -> str:
    exe = shutil.which("claude") or shutil.which("claude.exe")
    if exe:
        return exe
    npm = Path(os.environ.get("APPDATA", "")).expanduser() / "npm" / "claude.cmd"
    if npm.exists():
        return str(npm)
    return "claude"


def _build_cmd(options: ClaudeAgentOptions, mcp_cfg_str: Optional[str], sys_prompt_path: Optional[str]) -> list[str]:
    claude = _find_claude()
    cmd = [
        claude, "--print", "--verbose",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--model", options.model or "claude-sonnet-4-6",
        "--max-turns", str(options.max_turns),
        "--permission-mode", options.permission_mode or "dontAsk",
        "--tools", "",
        "--no-session-persistence",
    ]
    if sys_prompt_path:
        cmd += ["--system-prompt-file", sys_prompt_path]
    if options.allowed_tools:
        cmd += ["--allowed-tools"] + options.allowed_tools
    if mcp_cfg_str:
        cmd += ["--mcp-config", mcp_cfg_str]
    if options.strict_mcp_config:
        cmd += ["--strict-mcp-config"]
    if options.setting_sources is not None:
        srcs = ",".join(options.setting_sources)
        cmd += ["--setting-sources", srcs]
    for key, val in (options.extra_args or {}).items():
        cmd.append(f"--{key}")
        if val is not None:
            cmd.append(str(val))
    return cmd


# ---------------------------------------------------------------------------
# Parse stream-json output events into message objects
# ---------------------------------------------------------------------------

def _parse(raw: dict) -> Optional[Any]:
    typ = raw.get("type")
    if typ == "assistant":
        msg = raw.get("message", {})
        blocks = []
        for b in msg.get("content", []):
            if b.get("type") == "text" and b.get("text"):
                blocks.append(TextBlock(b["text"]))
            elif b.get("type") == "tool_use":
                blocks.append(ToolUseBlock(b.get("id", ""), b.get("name", ""), b.get("input") or {}))
        if not blocks:
            return None
        return AssistantMessage(
            content=blocks,
            model=msg.get("model", ""),
            message_id=msg.get("id", ""),
            stop_reason=msg.get("stop_reason", "end_turn"),
            error=str(msg["error"]) if msg.get("error") else None,
        )
    if typ == "user":
        msg = raw.get("message", {})
        blocks = []
        for b in msg.get("content", []):
            if b.get("type") == "tool_result":
                blocks.append(ToolResultBlock(b.get("tool_use_id", ""), b.get("content", ""),
                                              b.get("is_error")))
        return UserMessage(content=blocks) if blocks else None
    if typ == "result":
        usage = raw.get("usage") or {}
        err_status = raw.get("api_error_status")
        return ResultMessage(
            subtype=raw.get("subtype", "success"),
            duration_ms=int(raw.get("duration_ms") or 0),
            duration_api_ms=int(raw.get("duration_api_ms") or 0),
            is_error=bool(raw.get("is_error")),
            num_turns=int(raw.get("num_turns") or 0),
            session_id=raw.get("session_id", ""),
            usage=usage,
            result=raw.get("result"),
            api_error_status=int(err_status) if err_status else None,
        )
    return None


# ---------------------------------------------------------------------------
# Core: run claude as a subprocess and stream events
# ---------------------------------------------------------------------------

async def _run(
    messages: list[dict],
    options: ClaudeAgentOptions,
    mcp_setup: Optional[_McpSetup],
) -> AsyncIterator[Any]:
    session = uuid.uuid4().hex[:12]
    bridge: Optional[_Bridge] = None
    script_path: Optional[Path] = None
    sys_prompt_tmp: Optional[Path] = None
    mcp_cfg_str: Optional[str] = None

    # Start bridge + write MCP server script
    if mcp_setup and mcp_setup.tools:
        bridge = _Bridge(mcp_setup.tools, session)
        await bridge.start()
        tools_json = json.dumps([
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in mcp_setup.tools.values()
        ])
        script_src = _MCP_SCRIPT.format(port=bridge.port, session=session, tools_json=tools_json)
        fd, path = tempfile.mkstemp(suffix=".py", prefix="mcp_desk_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script_src)
        script_path = Path(path)
        mcp_cfg_str = json.dumps({"mcpServers": {mcp_setup.name: {
            "command": sys.executable, "args": [str(script_path)]
        }}})

    # System prompt: get the file path if already a file, else write one
    sp = options.system_prompt
    sys_prompt_file: Optional[str] = None
    if sp:
        if isinstance(sp, dict) and sp.get("type") == "file":
            sys_prompt_file = sp["path"]          # already on disk (SdkClient.system_file())
        else:
            text = sp.get("text", "") if isinstance(sp, dict) else str(sp)
            if text:
                fd2, p2 = tempfile.mkstemp(suffix=".md", prefix="sysprompt_")
                with os.fdopen(fd2, "w", encoding="utf-8") as f:
                    f.write(text)
                sys_prompt_tmp = Path(p2)
                sys_prompt_file = str(sys_prompt_tmp)

    cmd = _build_cmd(options, mcp_cfg_str, sys_prompt_file)

    # Build stdin: stream-json messages
    stdin_lines = []
    for m in messages:
        stdin_lines.append(json.dumps({"type": m["role"], "message": m}) + "\n")
    stdin_data = "".join(stdin_lines).encode()

    # Environment
    env = {**os.environ}
    env.update(options.env or {})
    if env.get("ANTHROPIC_API_KEY") == "":
        del env["ANTHROPIC_API_KEY"]

    cwd = options.cwd or str(Path.cwd())
    os.makedirs(cwd, exist_ok=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=cwd,
        )
        proc.stdin.write(stdin_data)
        proc.stdin.close()

        async for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj = _parse(event)
            if obj is not None:
                yield obj
                if isinstance(obj, ResultMessage):
                    break

        await proc.wait()
    finally:
        if bridge:
            await bridge.stop()
        if script_path and script_path.exists():
            try:
                script_path.unlink()
            except OSError:
                pass
        if sys_prompt_tmp and sys_prompt_tmp.exists():
            try:
                sys_prompt_tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class ClaudeSDKClient:
    """Async context manager matching the real SDK's ClaudeSDKClient interface."""

    def __init__(self, options: ClaudeAgentOptions):
        self.options = options
        self._messages: list[dict] = []
        self._mcp: Optional[_McpSetup] = None

    async def __aenter__(self) -> "ClaudeSDKClient":
        if self.options.mcp_servers:
            self._mcp = next(iter(self.options.mcp_servers.values()))
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def query(self, prompt: Any) -> None:
        """Queue a user message."""
        if isinstance(prompt, str):
            self._messages.append({"role": "user", "content": prompt})
        elif isinstance(prompt, list):
            self._messages.append({"role": "user", "content": prompt})

    async def receive_response(self) -> AsyncIterator[Any]:
        """Run claude with all queued messages and stream back events."""
        events: list[Any] = []
        async for evt in _run(list(self._messages), self.options, self._mcp):
            events.append(evt)
            yield evt
        # Append assistant/user turns to history so a follow-up query (nudge) has context
        for evt in events:
            if isinstance(evt, AssistantMessage):
                content = []
                for b in evt.content:
                    if isinstance(b, TextBlock):
                        content.append({"type": "text", "text": b.text})
                    elif isinstance(b, ToolUseBlock):
                        content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                self._messages.append({"role": "assistant", "content": content})
            elif isinstance(evt, UserMessage):
                content = []
                for b in evt.content:
                    if isinstance(b, ToolResultBlock):
                        c = b.content if isinstance(b.content, str) else str(b.content)
                        content.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                                        "content": c, "is_error": bool(b.is_error)})
                if content:
                    self._messages.append({"role": "user", "content": content})


# ---------------------------------------------------------------------------
# Standalone query function (used by SdkClient._Messages.create)
# ---------------------------------------------------------------------------

async def query(prompt: Any, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
    """One-shot query — used by _Messages.create for triage / one-shot calls."""
    mcp = next(iter(options.mcp_servers.values())) if options.mcp_servers else None
    messages: list[dict] = []
    if isinstance(prompt, str):
        messages = [{"role": "user", "content": prompt}]
    elif isinstance(prompt, list):
        messages = list(prompt)
    elif hasattr(prompt, "__aiter__"):
        async for item in prompt:
            if isinstance(item, dict) and item.get("type") == "user":
                messages.append(item.get("message", {}))
    else:
        messages = [{"role": "user", "content": str(prompt)}]
    async for evt in _run(messages, options, mcp):
        yield evt
