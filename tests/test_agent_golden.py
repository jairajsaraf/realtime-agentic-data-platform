"""Golden transcripts: frozen end-to-end behaviour of the agent loop (offline, deterministic).

Each fixture in tests/fixtures/agent_golden/ holds a question, the two budgets, scripted model
turns in a NEUTRAL format (content, tool calls, usage), and canned synthetic read-API responses.
Only the model is faked: tool calls run through the real ApiToolExecutor + static_registry over an
httpx.MockTransport, so validation, transport-error handling, and provenance are the real code
paths. The `expected` block (answer, Sources line, provenance, turns, complete, error, tokens,
and the API calls made) characterizes the loop and must not change when the loop's implementation
does: only `_scripted_llm` (the neutral-turn -> model adapter) may change with it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from rtdp.agent.llm import ChatResponse, FakeLLMClient, _parse_tool_calls
from rtdp.agent.loop import run_agent
from rtdp.agent.tools import ApiToolExecutor, static_registry

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agent_golden"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
_API_BASE = "http://api.test"


def _turn_to_response(turn: dict, turn_index: int, repeating: bool) -> ChatResponse:
    """Neutral turn -> the loop's model-response type (the ONLY implementation-specific part)."""
    wire_calls = []
    for call in turn.get("tool_calls", []):
        call_id = f"{call['id']}-t{turn_index}" if repeating else call["id"]
        raw = call["raw_arguments"] if "raw_arguments" in call else json.dumps(call["arguments"])
        wire_calls.append({"id": call_id, "function": {"name": call["name"], "arguments": raw}})
    usage = turn.get("usage")
    return ChatResponse(
        content=turn.get("content"),
        tool_calls=_parse_tool_calls(wire_calls),
        usage=None if usage is None else {"total_tokens": usage["total_tokens"]},
    )


def _scripted_llm(case: dict):
    turns = case["turns"]
    if not case.get("repeat_last_turn"):
        return FakeLLMClient([_turn_to_response(t, i, False) for i, t in enumerate(turns, 1)])
    state = {"n": 0}

    def respond(_messages, _tools):
        state["n"] += 1
        return _turn_to_response(turns[min(state["n"], len(turns)) - 1], state["n"], True)

    return FakeLLMClient(respond)


def _api_client(api: dict, calls: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append([request.url.path, dict(sorted(request.url.params.items()))])
        spec = api.get(request.url.path)
        if spec is None:
            return httpx.Response(404, json={"detail": "not found"})
        if spec == "transport_error":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(spec["status"], json=spec["json"])

    return httpx.Client(transport=httpx.MockTransport(handler))


def run_case(case: dict) -> dict:
    """Run one fixture through the real loop + executor and return the observable outcome."""
    calls: list = []
    registry = static_registry()
    with _api_client(case["api"], calls) as client:
        executor = ApiToolExecutor(_API_BASE, client, registry, max_rows=1000)
        result = run_agent(
            case["question"],
            llm=_scripted_llm(case),
            executor=executor,
            tools=registry.definitions(),
            max_turns=case["max_turns"],
            max_tool_calls=case["max_tool_calls"],
        )
    return {
        "answer": result.answer,
        "citation_line": result.citation_line(),
        "provenance": [asdict(p) for p in result.provenance],
        "turns": result.turns,
        "complete": result.complete,
        "error": result.error,
        "tokens": result.tokens,
        "api_calls": sorted(calls, key=json.dumps),
    }


def test_golden_fixture_set_is_present():
    # Guards against the parametrized test silently collecting nothing.
    assert len(FIXTURES) == 12, [p.name for p in FIXTURES]


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_golden_transcript(fixture: Path):
    case = json.loads(fixture.read_text(encoding="utf-8"))
    assert run_case(case) == case["expected"]
