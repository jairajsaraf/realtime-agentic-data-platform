"""Unit tests for the agent loop (fake LLM + fake executor, fully offline/deterministic)."""

from __future__ import annotations

from rtdp.agent.llm import ChatResponse, FakeLLMClient, ToolCall
from rtdp.agent.loop import run_agent
from rtdp.agent.tools import ToolResult

TOOLS: list[dict] = []  # the fake LLM ignores the tool list; pass an empty one


class _FakeExecutor:
    """Returns canned ToolResults keyed by tool name and records every call."""

    def __init__(self, results: dict[str, ToolResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, arguments))
        return self._results[name]


def _run(question, llm, executor, **kw):
    return run_agent(question, llm=llm, executor=executor, tools=TOOLS, **kw)


def test_answers_flight_question_via_tool_then_finalizes():
    script = [
        ChatResponse(
            tool_calls=[ToolCall("c1", "flights", {"icao24": "abc"})],
            usage={"total_tokens": 30},
        ),
        ChatResponse(
            content="There is 1 record for abc (snapshot 42).", usage={"total_tokens": 12}
        ),
    ]
    executor = _FakeExecutor(
        {
            "flights": ToolResult(
                "flights", "GET /flights", {"icao24": "abc"}, ok=True, snapshot_id=42,
                data={"count": 1, "items": [{"icao24": "abc"}]},
            )
        }
    )
    result = _run("how many for abc?", FakeLLMClient(script), executor)

    assert result.complete is True
    assert result.turns == 2
    assert result.answer == "There is 1 record for abc (snapshot 42)."
    assert result.tokens == 42  # summed across turns
    assert executor.calls == [("flights", {"icao24": "abc"})]
    assert "GET /flights @ snapshot 42" in result.citation_line()


def test_dq_diagnosis_proposes_without_applying():
    diagnosis = {
        "ok": False,
        "findings": [{"kind": "over_speed", "severity": "WARN", "count": 1}],
        "proposals": ["PROPOSED (requires human approval; not applied): review velocity."],
    }
    script = [
        ChatResponse(tool_calls=[ToolCall("d1", "diagnose_data_quality", {})]),
        ChatResponse(content="Found 1 over-speed row; proposed a fix (not applied)."),
    ]
    executor = _FakeExecutor(
        {
            "diagnose_data_quality": ToolResult(
                "diagnose_data_quality", "agent:diagnose_data_quality", {}, ok=True,
                snapshot_id=7, data=diagnosis,
            )
        }
    )
    result = _run("any data quality issues?", FakeLLMClient(script), executor)

    assert result.complete is True
    # Only the read-only diagnose tool was called; there is no write tool to call.
    assert [name for name, _ in executor.calls] == ["diagnose_data_quality"]
    assert "agent:diagnose_data_quality @ snapshot 7" in result.citation_line()


def test_out_of_scope_request_is_refused_without_tool_calls():
    script = [ChatResponse(content="I'm read-only; I can't modify data, only propose changes.")]
    executor = _FakeExecutor({})
    result = _run("delete all flights", FakeLLMClient(script), executor)

    assert result.complete is True
    assert executor.calls == []  # no tool invoked
    assert "read-only" in result.answer
    assert result.citation_line() == "Sources: (no tool calls)"


def test_max_turns_guard_returns_incomplete():
    # The model loops forever requesting a tool; the budget must stop it.
    llm = FakeLLMClient(
        lambda messages, tools: ChatResponse(tool_calls=[ToolCall("c", "flights", {})])
    )
    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=1, data={})}
    )
    result = _run("loop", llm, executor, max_turns=3)

    assert result.complete is False
    assert result.error == "max_turns exhausted"
    assert result.turns == 3
    assert len(executor.calls) == 3


def test_citation_line_dedupes_repeated_sources():
    script = [
        ChatResponse(
            tool_calls=[
                ToolCall("c1", "flights", {"icao24": "a"}),
                ToolCall("c2", "flights", {"icao24": "b"}),
            ]
        ),
        ChatResponse(content="done"),
    ]
    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=42, data={})}
    )
    result = _run("q", FakeLLMClient(script), executor)
    assert result.citation_line() == "Sources: GET /flights @ snapshot 42"
