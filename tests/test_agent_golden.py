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

pytest.importorskip("langchain", reason="requires the optional [agent] extra")

from langchain_core.exceptions import OutputParserException  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.output_parsers.openai_tools import (  # noqa: E402
    make_invalid_tool_call,
    parse_tool_call,
)

from rtdp.agent.llm import ScriptedChatModel  # noqa: E402
from rtdp.agent.loop import run_agent  # noqa: E402
from rtdp.agent.tools import ApiToolExecutor, static_registry  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agent_golden"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))
_API_BASE = "http://api.test"


def _turn_to_message(turn: dict, turn_index: int, repeating: bool) -> AIMessage:
    """Neutral turn -> the loop's model-response type (the ONLY implementation-specific part).

    Raw tool-call JSON goes through the same langchain_core parser ChatOpenAI uses, so a
    malformed payload lands in ``invalid_tool_calls`` exactly as it would from a live endpoint.
    """
    tool_calls, invalid_tool_calls = [], []
    for call in turn.get("tool_calls", []):
        call_id = f"{call['id']}-t{turn_index}" if repeating else call["id"]
        raw = call["raw_arguments"] if "raw_arguments" in call else json.dumps(call["arguments"])
        wire = {"id": call_id, "function": {"name": call["name"], "arguments": raw}}
        try:
            tool_calls.append(parse_tool_call(wire, return_id=True))
        except OutputParserException as exc:
            invalid_tool_calls.append(make_invalid_tool_call(wire, str(exc)))
    usage = turn.get("usage")
    return AIMessage(
        content=turn.get("content") or "",
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls,
        usage_metadata=usage,
    )


def _scripted_llm(case: dict) -> ScriptedChatModel:
    turns = case["turns"]
    if not case.get("repeat_last_turn"):
        return ScriptedChatModel([_turn_to_message(t, i, False) for i, t in enumerate(turns, 1)])
    state = {"n": 0}

    def respond(_messages):
        state["n"] += 1
        return _turn_to_message(turns[min(state["n"], len(turns)) - 1], state["n"], True)

    return ScriptedChatModel(respond)


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
