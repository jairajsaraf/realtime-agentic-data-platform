"""CLI wiring tests for `rtdp agent` (offline: MockTransport API + fake LLM, no keys/network)."""

from __future__ import annotations

import json

import httpx

from rtdp.agent.llm import ChatResponse, FakeLLMClient, ToolCall
from rtdp.cli import main

OPENAPI = {
    "paths": {
        "/health": {"get": {"summary": "Health", "parameters": []}},
        "/flights": {
            "get": {
                "summary": "Typed flight reads",
                "parameters": [
                    {
                        "name": "icao24",
                        "in": "query",
                        "required": False,
                        "schema": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    }
                ],
            }
        },
    }
}


def _api_handler(request):
    path = request.url.path
    if path == "/health":
        return httpx.Response(200, json={"status": "ok", "current_snapshot_id": 42})
    if path == "/openapi.json":
        return httpx.Response(200, json=OPENAPI)
    if path == "/flights":
        return httpx.Response(
            200, json={"snapshot_id": 42, "count": 1, "items": [{"icao24": "abc"}]}
        )
    return httpx.Response(404, json={"detail": "not found"})


def _patch_api(monkeypatch, handler=_api_handler):
    monkeypatch.setattr(
        "rtdp.agent.runtime.build_http_client",
        lambda settings: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _patch_llm(monkeypatch, script):
    monkeypatch.setattr(
        "rtdp.agent.runtime.build_llm_client", lambda settings: FakeLLMClient(script)
    )


def _answer_script():
    return [
        ChatResponse(tool_calls=[ToolCall("c1", "flights", {"icao24": "abc"})]),
        ChatResponse(content="There is 1 record for abc."),
    ]


def test_cli_agent_one_shot(monkeypatch, capsys):
    _patch_api(monkeypatch)
    _patch_llm(monkeypatch, _answer_script())
    rc = main(["agent", "how many records for abc?"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "There is 1 record for abc." in out
    assert "Sources:" in out
    assert "GET /flights @ snapshot 42" in out


def test_cli_agent_json_output(monkeypatch, capsys):
    _patch_api(monkeypatch)
    _patch_llm(monkeypatch, _answer_script())
    rc = main(["agent", "q", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["answer"] == "There is 1 record for abc."
    assert payload["complete"] is True
    assert payload["provenance"][0]["snapshot_id"] == 42


def test_cli_agent_interactive(monkeypatch, capsys):
    _patch_api(monkeypatch)
    _patch_llm(monkeypatch, _answer_script())
    answers = iter(["how many records for abc?", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    rc = main(["agent", "--interactive"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "interactive mode" in out
    assert "There is 1 record for abc." in out


def test_cli_agent_api_unreachable_returns_2(monkeypatch, capsys):
    def down_handler(request):
        raise httpx.ConnectError("connection refused")

    _patch_api(monkeypatch, down_handler)
    _patch_llm(monkeypatch, _answer_script())
    rc = main(["agent", "q"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not reachable" in err
    assert "rtdp serve" in err


def test_cli_agent_missing_model_config_returns_2(monkeypatch, capsys):
    # API reachable, but no live model configured -> clear error (real build_llm_client raises).
    _patch_api(monkeypatch)
    rc = main(["agent", "q"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "No agent model configured" in err
