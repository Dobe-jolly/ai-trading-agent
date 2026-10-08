"""The reasoning loop: the model reads the masked ticket, calls the lookups, and ends with submit_decision."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from .attachments import render_attachment
from .decision import SUBMIT_DECISION_TOOL, Decision
from .normalize import today_local
from .osticket import Ticket
from agentcore.pii import Pseudonymizer
from .tools import Evidence, Toolbox

log = logging.getLogger(__name__)
MAX_ENTRY_CHARS = 2500
MAX_THREAD_CHARS = 16000  # thread text plus what was read from attachments
NUDGE = (
    "You have not submitted a decision. Call submit_decision now. "
    "If the evidence does not support a confident decision, use action escalate."
)
ENTRY_LABELS = {"M": "Ticket owner", "R": "Staff reply", "N": "Internal note"}


class AgentError(RuntimeError):
    """The model could not be reached; the ticket is left untouched and retried next run."""


@dataclass
class AgentResult:
    decision: Decision
    evidence: Evidence = field(default_factory=Evidence)
    pseudo: Pseudonymizer = field(default_factory=Pseudonymizer)
    transcript: list[dict[str, Any]] = field(default_factory=list)  # masked, as the model saw it
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0


def render_ticket(ticket: Ticket, as_of: Optional[str] = None, agent_staff_id: Optional[int] = None,
                  context: Optional[str] = None) -> str:
    """The ticket as the model sees it (before masking)."""
    found = ticket.owner_identifiers()
    lines = [
        f"Today: {as_of or today_local().isoformat()}",
        *([f"Operations context: {context}", ""] if context else []),
        f"Ticket #{ticket.number}   Help topic: {ticket.help_topic or '(none)'}   Opened: {ticket.created}"
        + (f"   Raised by: {ticket.org_name}" if ticket.org_name else ""),
        f"Subject: {ticket.subject or '(none)'}",
    ]
    if not ticket.topic_match:
        lines.append("(Not under an EcoCash help topic: picked up because the subject or a message mentions EcoCash.)")
    lines += ["", "Form fields (as captured on the ticket):"]
    lines += [f"  {label}: {value}" for label, value in ticket.fields.items()] or ["  (none)"]
    lines += [
        "",
        "Details the ticket owner gave (form, messages and their attachments):",
        # Keys are the last 9 digits; shown as 0 + key so they mask as the same <MOBILE_n> as in the thread.
        "  Mobile numbers: " + (", ".join("0" + m for m in sorted(found.mobiles)) or "(none)"),
        "  Account numbers: " + (", ".join(sorted(found.accounts)) or "(none)"),
        "  National IDs: " + (", ".join(sorted(found.ids)) or "(none)"),
        "",
        "Thread, oldest first:",
    ]
    budget = MAX_THREAD_CHARS
    rendered: list[str] = []
    for entry in reversed(ticket.entries):  # newest first so recent messages survive truncation
        text = entry.body[:MAX_ENTRY_CHARS] + (" [...truncated]" if len(entry.body) > MAX_ENTRY_CHARS else "")
        who = ENTRY_LABELS.get(entry.type, entry.type)
        if entry.type != "M":
            who += f" ({entry.poster})"
            if agent_staff_id is not None and entry.staff_id == agent_staff_id:
                who += " [posted by the EcoCash agent - an earlier automated reply/note]"
        files = "\n".join(render_attachment(a) for a in entry.attachments)
        block = f"--- [{entry.created}] {who}\n{text}" + (f"\n{files}" if files else "")
        if len(block) > budget and not rendered:  # the newest entry is always shown, cut to fit
            block = block[:budget] + " [...truncated]"
        elif len(block) > budget:
            rendered.append("--- [older entries omitted]")
            break
        budget -= len(block)
        rendered.append(block)
    if not rendered:
        rendered.append("(no thread entries)")
    return "\n".join(lines + list(reversed(rendered)))


def _block_to_dict(block: Any) -> Optional[dict[str, Any]]:
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


class TicketAgent:
    def __init__(
        self,
        client: Any,
        model: str,
        system_prompt: str,
        toolbox: Toolbox,
        max_turns: int = 10,
        max_tokens: int = 2048,
        agent_staff_id: Optional[int] = None,
    ) -> None:
        self._client = client
        self._agent_staff_id = agent_staff_id
        self.context: Optional[str] = None  # set by the runner from the anomaly watch
        self.on_step: Optional[Any] = None  # callback(text) for the console's live view (masked text only)
        self._model = model
        self._system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        self._toolbox = toolbox
        self._tools = toolbox.schemas() + [SUBMIT_DECISION_TOOL]
        self._max_turns = max_turns
        self._max_tokens = max_tokens

    def _step(self, text: str) -> None:
        if self.on_step is not None:
            try:
                self.on_step(text)
            except Exception:  # noqa: BLE001 - the live view must never break a decision
                log.debug("live step not recorded", exc_info=True)

    def decide(self, ticket: Ticket, as_of: Optional[str] = None) -> AgentResult:
        pseudo = Pseudonymizer()
        result = AgentResult(decision=Decision.fallback("not decided"), pseudo=pseudo)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": pseudo.mask(render_ticket(ticket, as_of, self._agent_staff_id, self.context))
            + "\n\nInvestigate with the lookups, then call submit_decision.",
        }]
        nudged = False

        for turn in range(1, self._max_turns + 1):
            result.turns = turn
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=self._system,
                    tools=self._tools,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                raise AgentError(f"Model call failed: {type(exc).__name__}: {exc}") from exc

            usage = getattr(response, "usage", None)
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0

            content = [d for d in (_block_to_dict(b) for b in response.content) if d]
            messages.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b["type"] == "tool_use"]

            if not tool_uses:
                if nudged:
                    result.decision = Decision.fallback("Model ended without submitting a decision.")
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue

            tool_results: list[dict[str, Any]] = []
            decision: Optional[Decision] = None
            for use in tool_uses:
                if use["name"] == "submit_decision":
                    try:
                        decision = Decision.from_tool_input(use.get("input") or {})
                        output, is_error = "Decision recorded.", False
                        self._step(f"decided: {decision.action} ({decision.confidence:.0%})")
                    except ValueError as exc:
                        output, is_error = f"Invalid decision: {exc}. Fix it and call submit_decision again.", True
                else:
                    log.debug("#%s: tool %s", ticket.number, use["name"])
                    args = ", ".join(str(v) for v in (use.get("input") or {}).values())
                    self._step(f"{use['name']}({args[:120]})")
                    output, is_error = self._toolbox.run(
                        use["name"], use.get("input") or {}, pseudo, result.evidence, ticket
                    )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": use["id"], "content": output, "is_error": is_error}
                )
            messages.append({"role": "user", "content": tool_results})

            if decision is not None:
                result.decision = decision
                break
        else:
            result.decision = Decision.fallback(f"No decision within {self._max_turns} turns.")

        result.transcript = messages
        return result
