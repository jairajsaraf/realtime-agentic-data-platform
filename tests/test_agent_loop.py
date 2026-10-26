"""Unit tests for the agent loop (scripted model + fake executor, fully offline/deterministic)."""

from __future__ import annotations

import pytest

pytest.importorskip("langchain", reason="requires the optional [agent] extra")

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.messages.tool import tool_call  # noqa: E402

from rtdp.agent.llm import ScriptedChatModel  # noqa: E402
from rtdp.agent.loop import run_agent  # noqa: E402
from rtdp.agent.tools import ToolResult, static_registry  # noqa: E402

TOOLS: list[dict] = static_registry().definitions()


class _FakeExecutor:
    """Returns canned ToolResults keyed by tool name and records every call."""

    def __init__(self, results: dict[str, ToolResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, arguments))
        return self._results[name]


def _calls(*specs: tuple[str, str, dict], usage: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[tool_call(name=n, args=a, id=i) for i, n, a in specs],
        usage_metadata=usage,
    )


def _usage(total: int) -> dict:
    return {"input_tokens": total - 1, "output_tokens": 1, "total_tokens": total}


def _run(question, llm, executor, **kw):
    return run_agent(question, llm=llm, executor=executor, tools=TOOLS, **kw)


def test_answers_flight_question_via_tool_then_finalizes():
    script = [
        _calls(("c1", "flights", {"icao24": "abc"}), usage=_usage(30)),
        AIMessage(content="There is 1 record for abc (snapshot 42).", usage_metadata=_usage(12)),
    ]
    executor = _FakeExecutor(
        {
            "flights": ToolResult(
                "flights", "GET /flights", {"icao24": "abc"}, ok=True, snapshot_id=42,
                data={"count": 1, "items": [{"icao24": "abc"}]},
            )
        }
    )
    result = _run("how many for abc?", ScriptedChatModel(script), executor)

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
        _calls(("d1", "diagnose_data_quality", {})),
        AIMessage(content="Found 1 over-speed row; proposed a fix (not applied)."),
    ]
    executor = _FakeExecutor(
        {
            "diagnose_data_quality": ToolResult(
                "diagnose_data_quality", "agent:diagnose_data_quality", {}, ok=True,
                snapshot_id=7, data=diagnosis,
            )
        }
    )
    result = _run("any data quality issues?", ScriptedChatModel(script), executor)

    assert result.complete is True
    # Only the read-only diagnose tool was called; there is no write tool to call.
    assert [name for name, _ in executor.calls] == ["diagnose_data_quality"]
    assert "agent:diagnose_data_quality @ snapshot 7" in result.citation_line()


def test_out_of_scope_request_is_refused_without_tool_calls():
    script = [AIMessage(content="I'm read-only; I can't modify data, only propose changes.")]
    executor = _FakeExecutor({})
    result = _run("delete all flights", ScriptedChatModel(script), executor)

    assert result.complete is True
    assert executor.calls == []  # no tool invoked
    assert "read-only" in result.answer
    assert result.citation_line() == "Sources: (no tool calls)"
    assert result.tokens is None  # no usage reported


def test_max_turns_guard_returns_incomplete():
    # The model loops forever requesting a tool; the budget must stop it.
    counter = {"n": 0}

    def respond(_messages):
        counter["n"] += 1
        return _calls((f"c{counter['n']}", "flights", {}))

    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=1, data={})}
    )
    result = _run("loop", ScriptedChatModel(respond), executor, max_turns=3)

    assert result.complete is False
    assert result.error == "max_turns exhausted"
    assert result.turns == 3
    assert len(executor.calls) == 3


def test_tool_call_budget_caps_executions_within_one_response():
    # One assistant message emits 3 tool calls; the budget of 2 must stop the 3rd from executing.
    script = [
        _calls(
            ("c1", "flights", {"icao24": "a"}),
            ("c2", "flights", {"icao24": "b"}),
            ("c3", "flights", {"icao24": "c"}),
        ),
    ]
    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=1, data={})}
    )
    result = _run("q", ScriptedChatModel(script), executor, max_tool_calls=2)

    assert result.complete is False
    assert result.error == "max_tool_calls exhausted"
    assert len(executor.calls) == 2  # only the first two of three executed


def test_multiple_tool_calls_in_one_response_run_in_model_order():
    script = [
        _calls(
            ("c1", "flights", {"icao24": "a"}),
            ("c2", "flights", {"icao24": "b"}),
            ("c3", "flights", {"icao24": "c"}),
        ),
        AIMessage(content="done"),
    ]
    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=42, data={})}
    )
    result = _run("q", ScriptedChatModel(script), executor, max_tool_calls=5)

    assert result.complete is True
    # Sequential, in the order the model gave them (max_concurrency=1), all under the budget.
    assert [args["icao24"] for _, args in executor.calls] == ["a", "b", "c"]


def test_citation_line_dedupes_repeated_sources():
    script = [
        _calls(("c1", "flights", {"icao24": "a"}), ("c2", "flights", {"icao24": "b"})),
        AIMessage(content="done"),
    ]
    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=42, data={})}
    )
    result = _run("q", ScriptedChatModel(script), executor)
    assert result.citation_line() == "Sources: GET /flights @ snapshot 42"


def test_on_event_reports_each_call_and_its_result():
    script = [_calls(("c1", "flights", {"icao24": "a"})), AIMessage(content="done")]
    canned = ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=42, data={})
    events: list[tuple[str, object]] = []
    _run(
        "q", ScriptedChatModel(script), _FakeExecutor({"flights": canned}),
        on_event=lambda kind, payload: events.append((kind, payload)),
    )
    assert [kind for kind, _ in events] == ["tool_call", "tool_result"]
    assert events[0][1]["name"] == "flights"
    assert events[1][1] == canned


def test_under_scripted_model_surfaces_index_error():
    script = [_calls(("c1", "flights", {}))]  # no final turn scripted
    executor = _FakeExecutor(
        {"flights": ToolResult("flights", "GET /flights", {}, ok=True, snapshot_id=1, data={})}
    )
    with pytest.raises(IndexError, match="ran out of scripted responses"):
        _run("q", ScriptedChatModel(script), executor)


def test_langsmith_tracing_is_forced_off(monkeypatch):
    # Even with tracing switched on in the environment, no run is traced to LangSmith.
    from langsmith.utils import tracing_is_enabled

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    seen: list[bool] = []

    def respond(_messages):
        seen.append(bool(tracing_is_enabled()))
        return AIMessage(content="ok")

    _run("q", ScriptedChatModel(respond), _FakeExecutor({}))
    assert seen == [False]


def test_package_exports_resolve_lazily():
    import rtdp.agent as agent_pkg

    assert agent_pkg.run_agent is run_agent
    assert agent_pkg.ScriptedChatModel is ScriptedChatModel
    with pytest.raises(AttributeError):
        agent_pkg.no_such_name  # noqa: B018
