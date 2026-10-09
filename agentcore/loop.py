"""The reasoning loop both agents use: the model reads the masked ticket, calls the agent's lookups, and
ends with submit_decision. Each agent supplies how the ticket is shown (render), its tools and its wording."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from .decision import SUBMIT_DECISION_TOOL, Decision
from .pii import Pseudonymizer

log = logging.getLogger(__name__)
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
    evidence: Any = None  # each agent's AgentResult gives it its own Evidence
    pseudo: Pseudonymizer = field(default_factory=Pseudonymizer)
    transcript: list[dict[str, Any]] = field(default_factory=list)  # masked, as the model saw it
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0


def _block_to_dict(block: Any) -> Optional[dict[str, Any]]:
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


class TicketAgent:
    """Runs one ticket through the model. Each agent's subclass sets render (the ticket as the model reads it),
    result_type (an AgentResult with its own Evidence) and instruction; its toolbox has
    run(name, args, pseudo, evidence, ticket) -> (masked text, is_error)."""
    result_type: Any = None  # set by the agent
    decision_type: Any = Decision  # a desk with its own decision fields sets its subclass
    instruction = "Investigate with the lookups, then call submit_decision."

    @staticmethod
    def render(ticket: Any, as_of: Optional[str], agent_staff_id: Optional[int], context: Optional[str]) -> str:
        raise NotImplementedError

    def __init__(
        self,
        client: Any,
        model: str,
        system_prompt: str,
        toolbox: Any,
        max_turns: int = 10,
        max_tokens: int = 2048,
        agent_staff_id: Optional[int] = None,
        decision_tool: Optional[dict[str, Any]] = None,
    ) -> None:
        self._client = client
        self._agent_staff_id = agent_staff_id
        self.context: Optional[str] = None  # set by the runner from the anomaly watch
        self.on_step: Optional[Any] = None  # callback(text) for the console's live view (masked text only)
        self._model = model
        self._system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        self._toolbox = toolbox
        self._tools = toolbox.schemas() + [decision_tool or SUBMIT_DECISION_TOOL]
        self._max_turns = max_turns
        self._max_tokens = max_tokens

    def _step(self, text: str) -> None:
        if self.on_step is not None:
            try:
                self.on_step(text)
            except Exception:  # noqa: BLE001 - the live view must never break a decision
                log.debug("live step not recorded", exc_info=True)

    def decide(self, ticket: Any, as_of: Optional[str] = None) -> AgentResult:
        pseudo = Pseudonymizer()
        result = self.result_type(decision=self.decision_type.fallback("not decided"), pseudo=pseudo)
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": pseudo.mask(self.render(ticket, as_of, self._agent_staff_id, self.context))
            + "\n\n" + self.instruction,
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
                    result.decision = self.decision_type.fallback("Model ended without submitting a decision.")
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue

            tool_results: list[dict[str, Any]] = []
            decision: Optional[Decision] = None
            for use in tool_uses:
                if use["name"] == "submit_decision":
                    try:
                        decision = self.decision_type.from_tool_input(use.get("input") or {})
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
            result.decision = self.decision_type.fallback(f"No decision within {self._max_turns} turns.")

        result.transcript = messages
        return result
