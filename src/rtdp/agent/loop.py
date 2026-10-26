"""Agent loop on LangChain ``create_agent``: question -> read-only tool calls -> grounded answer.

Pure orchestration. The loop depends only on an injected LangChain chat model and an executor
exposing ``execute(name, arguments) -> ToolResult``, so tests drive it with a scripted model and a
fake/mock executor — no network. Provenance (endpoint + snapshot id per tool call) is collected
from the ``ToolResult`` artifacts attached to the tool messages, never from model text, so an
answer's citations are guaranteed regardless of what the model writes.

Two independent budgets bound the loop, enforced by LangChain's built-in limit middleware:
``max_turns`` caps model round-trips (``ModelCallLimitMiddleware``), and ``max_tool_calls`` caps
total tool executions per question (``ToolCallLimitMiddleware``) — so even a single model response
that emits many tool calls cannot exceed the configured tool-call budget. :class:`_LoopGuard` adds
what the built-ins do not: it stops the run as soon as the tool budget has blocked a call (instead
of granting the model another turn), counts turns and tokens, degrades malformed tool-call JSON to
``{}`` for the executor's own validation, and routes unknown tool names to the executor so the
attempt is recorded like any other tool result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelResponse,
    ToolCallLimitMiddleware,
    hook_config,
)
from langchain.agents.middleware.tool_call_limit import ToolCallLimitState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import tool_call as make_tool_call
from langchain_core.tools import StructuredTool
from langsmith import tracing_context

from .prompts import SYSTEM_PROMPT
from .tools import ToolResult

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

_TOOL_BUDGET_ANSWER = "Stopped: reached the per-question tool-call budget."
_TURN_BUDGET_ANSWER = "Stopped: reached the tool-call budget before producing a final answer."
_ALL_TOOLS_KEY = "__all__"  # ToolCallLimitMiddleware's count key when no tool_name filter is set


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


def _as_tool(definition: dict, executor) -> StructuredTool:
    """An OpenAI-style tool definition -> a LangChain tool that runs through ``executor``.

    The JSON-schema ``parameters`` are handed to the model as-is; LangChain does not validate
    dict schemas, so the executor still validates and coerces arguments exactly as before. The
    :class:`ToolResult` rides along as the message artifact (``content_and_artifact``).
    """
    fn = definition["function"]
    name = fn["name"]

    def call(**arguments: Any) -> tuple[str, ToolResult]:
        result = executor.execute(name, arguments)
        return result.to_content(), result

    return StructuredTool(
        name=name,
        description=fn.get("description") or name,
        args_schema=fn.get("parameters") or {"type": "object", "properties": {}},
        func=call,
        response_format="content_and_artifact",
    )


def _degrade_invalid_tool_calls(message: AIMessage) -> AIMessage:
    """Turn calls whose JSON arguments failed to parse into calls with ``{}`` arguments.

    LangChain records malformed arguments as ``invalid_tool_calls`` and would end the run on
    them; the executor's own validation (missing required params, type errors) answers instead.

    Ordering: LangChain parses well-formed and malformed calls into separate lists and keeps no
    record of how they interleaved on the wire, so degraded calls run after the well-formed calls
    of the same response. Under a tool budget that runs out within that response, the
    well-formed calls are the ones that run (the pre-LangChain loop kept wire order).
    """
    if not message.invalid_tool_calls:
        return message
    degraded = [
        make_tool_call(name=bad.get("name") or "", args={}, id=bad.get("id") or "")
        for bad in message.invalid_tool_calls
    ]
    return message.model_copy(
        update={"tool_calls": [*message.tool_calls, *degraded], "invalid_tool_calls": []}
    )


class _LoopGuard(AgentMiddleware):
    """Per-run accounting + the behaviours the built-in limit middleware does not provide."""

    state_schema = ToolCallLimitState  # read the run's tool-call count kept by the limiter

    def __init__(
        self,
        executor,
        max_tool_calls: int | None,
        on_event: Callable[[str, object], None] | None,
    ) -> None:
        super().__init__()
        self._executor = executor
        self._max_tool_calls = max_tool_calls
        self._on_event = on_event
        self.turns = 0
        self.tokens: int | None = None
        self.last_reply: AIMessage | None = None
        self.tool_budget_hit = False

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        # The limiter counts every attempted call, blocked ones included: a count above the
        # budget means a call was refused, so stop now rather than give the model another turn.
        if self._max_tool_calls is None:
            return None
        attempted = (state.get("run_tool_call_count") or {}).get(_ALL_TOOLS_KEY, 0)
        if attempted > self._max_tool_calls:
            self.tool_budget_hit = True
            return {"jump_to": "end"}
        return None

    def wrap_model_call(self, request, handler):
        response = handler(request)
        result = []
        for message in response.result:
            if isinstance(message, AIMessage):
                message = _degrade_invalid_tool_calls(message)
                usage = message.usage_metadata
                if usage and isinstance(usage.get("total_tokens"), int):
                    self.tokens = (self.tokens or 0) + usage["total_tokens"]
                self.last_reply = message
            result.append(message)
        self.turns += 1
        return ModelResponse(result=result, structured_response=response.structured_response)

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if self._on_event is not None:
            self._on_event("tool_call", call)
        if request.tool is None:
            # Unknown tool name: the executor answers (and records `unknown:<name>`), so the
            # attempt carries provenance like any other tool result.
            result = self._executor.execute(call["name"], call.get("args") or {})
            message = ToolMessage(
                content=result.to_content(),
                artifact=result,
                tool_call_id=call["id"],
                name=call["name"],
            )
        else:
            message = handler(request)
        if self._on_event is not None:
            self._on_event("tool_result", getattr(message, "artifact", None))
        return message


def _reply_text(message: AIMessage) -> str:
    return message.content if isinstance(message.content, str) else message.text


def run_agent(
    question: str,
    *,
    llm: BaseChatModel,
    executor,
    tools: list[dict],
    max_turns: int = 6,
    max_tool_calls: int | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    on_event: Callable[[str, object], None] | None = None,
) -> AgentResult:
    """Run the tool-calling agent until the model gives a final answer or a budget is spent.

    ``max_turns`` caps model round-trips; ``max_tool_calls`` (when set) caps total tool
    executions across the whole question, enforced per individual call so a single multi-call
    response cannot overshoot it. Tool calls run one at a time, in the order the model gave them.
    """
    guard = _LoopGuard(executor, max_tool_calls, on_event)
    middleware: list[AgentMiddleware] = [
        guard,  # first, so a spent tool budget is reported ahead of a spent turn budget
        ModelCallLimitMiddleware(run_limit=max_turns, exit_behavior="end"),
    ]
    if max_tool_calls is not None:
        middleware.append(
            ToolCallLimitMiddleware(run_limit=max_tool_calls, exit_behavior="continue")
        )
    agent = create_agent(
        llm,
        [_as_tool(definition, executor) for definition in tools],
        system_prompt=system_prompt,
        middleware=middleware,
    )
    config = {
        "max_concurrency": 1,  # sequential tool execution, as before
        # A ceiling on graph steps only; the budgets above are what stop the loop.
        "recursion_limit": (max_turns + 1) * (2 * len(middleware) + 3) + 1,
    }
    # Never ship prompts or tool data to LangSmith, whatever LANGSMITH_* says in the environment.
    with tracing_context(enabled=False):
        state = agent.invoke({"messages": [HumanMessage(question)]}, config=config)

    provenance = [
        Provenance(message.artifact.endpoint, message.artifact.snapshot_id)
        for message in state["messages"]
        if isinstance(message, ToolMessage) and isinstance(message.artifact, ToolResult)
    ]
    if guard.tool_budget_hit:
        return AgentResult(
            answer=_TOOL_BUDGET_ANSWER,
            provenance=provenance,
            turns=guard.turns,
            complete=False,
            error="max_tool_calls exhausted",
            tokens=guard.tokens,
        )
    reply = guard.last_reply
    if reply is not None and not reply.tool_calls:
        return AgentResult(
            answer=_reply_text(reply),
            provenance=provenance,
            turns=guard.turns,
            complete=True,
            tokens=guard.tokens,
        )
    return AgentResult(
        answer=_TURN_BUDGET_ANSWER,
        provenance=provenance,
        turns=guard.turns,
        complete=False,
        error="max_turns exhausted",
        tokens=guard.tokens,
    )
