"""Deterministic agent loop: question -> tool calls -> grounded answer.

Pure orchestration. The loop depends only on an injected :class:`~rtdp.agent.llm.LLMClient` and an
executor exposing ``execute(name, arguments) -> ToolResult``, so tests drive it with a fake LLM and
a fake/mock executor — no network. Provenance (endpoint + snapshot id per tool call) is collected
from the tool results themselves, so an answer's citations are guaranteed regardless of what the
model writes. Two independent budgets bound the loop: ``max_turns`` caps model round-trips, and
``max_tool_calls`` caps total tool executions per question — so even a single model response that
emits many tool calls cannot exceed the configured tool-call budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .llm import ChatMessage, LLMClient
from .prompts import SYSTEM_PROMPT


@dataclass
class Provenance:
    endpoint: str
    snapshot_id: int | None


@dataclass
class AgentResult:
    answer: str
    provenance: list[Provenance] = field(default_factory=list)
    turns: int = 0
    complete: bool = True
    error: str | None = None
    tokens: int | None = None  # summed total_tokens across turns when the endpoint reports usage

    def citation_line(self) -> str:
        """A deterministic 'Sources: ...' line built from the tool results, not the model text."""
        if not self.provenance:
            return "Sources: (no tool calls)"
        parts: list[str] = []
        seen: set[tuple[str, int | None]] = set()
        for prov in self.provenance:
            key = (prov.endpoint, prov.snapshot_id)
            if key in seen:
                continue
            seen.add(key)
            snap = f" @ snapshot {prov.snapshot_id}" if prov.snapshot_id is not None else ""
            parts.append(f"{prov.endpoint}{snap}")
        return "Sources: " + "; ".join(parts)


def run_agent(
    question: str,
    *,
    llm: LLMClient,
    executor,
    tools: list[dict],
    max_turns: int = 6,
    max_tool_calls: int | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    on_event: Callable[[str, object], None] | None = None,
) -> AgentResult:
    """Run the tool-calling loop until the model gives a final answer or a budget is spent.

    ``max_turns`` caps model round-trips; ``max_tool_calls`` (when set) caps total tool
    executions across the whole question, enforced per individual call so a single multi-call
    response cannot overshoot it.
    """
    messages: list[ChatMessage] = [
        ChatMessage("system", system_prompt),
        ChatMessage("user", question),
    ]
    provenance: list[Provenance] = []
    turns = 0
    tool_calls_made = 0
    total_tokens = 0
    saw_usage = False
    for turn in range(1, max_turns + 1):
        turns = turn
        response = llm.chat(messages, tools)
        if response.usage and isinstance(response.usage.get("total_tokens"), int):
            total_tokens += response.usage["total_tokens"]
            saw_usage = True
        if not response.tool_calls:
            return AgentResult(
                answer=response.content or "",
                provenance=provenance,
                turns=turns,
                complete=True,
                tokens=total_tokens if saw_usage else None,
            )
        messages.append(
            ChatMessage("assistant", response.content, tool_calls=response.tool_calls)
        )
        for call in response.tool_calls:
            if max_tool_calls is not None and tool_calls_made >= max_tool_calls:
                # Budget reached mid-response: stop before executing any further tool calls.
                return AgentResult(
                    answer="Stopped: reached the per-question tool-call budget.",
                    provenance=provenance,
                    turns=turns,
                    complete=False,
                    error="max_tool_calls exhausted",
                    tokens=total_tokens if saw_usage else None,
                )
            if on_event is not None:
                on_event("tool_call", call)
            result = executor.execute(call.name, call.arguments)
            tool_calls_made += 1
            provenance.append(Provenance(result.endpoint, result.snapshot_id))
            messages.append(
                ChatMessage("tool", result.to_content(), tool_call_id=call.id, name=call.name)
            )
            if on_event is not None:
                on_event("tool_result", result)
    return AgentResult(
        answer="Stopped: reached the tool-call budget before producing a final answer.",
        provenance=provenance,
        turns=turns,
        complete=False,
        error="max_turns exhausted",
        tokens=total_tokens if saw_usage else None,
    )
