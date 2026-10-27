"""CLI tests for `rtdp agent` (offline: MockTransport API + scripted model, no keys/network).

The import-guard tests run on the DEFAULT install (no extra), mirroring tests/test_mcp_cli.py:
the install hint appears only when the missing module belongs to the [agent] SDK; any other
ImportError must re-raise. The wiring tests need the optional [agent] extra and skip without it.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys

import httpx
import pytest

from rtdp.cli import main

requires_agent_extra = pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None,
    reason="requires the optional [agent] extra",
)
_SDK_TOP_LEVEL = ("langchain", "langchain_core", "langchain_openai", "langgraph", "openai")


def _block_agent_sdk(monkeypatch):
    """Simulate the missing [agent] extra even when it is installed (cf. _block_mcp_sdk).

    Cached SDK and rtdp.agent modules are purged so the CLI's lazy import really re-imports,
    and the parent ``rtdp`` attribute is dropped so no stale ``rtdp.agent`` binding survives.
    """
    for name in [m for m in sys.modules if m.partition(".")[0] in _SDK_TOP_LEVEL]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for top in _SDK_TOP_LEVEL:
        monkeypatch.setitem(sys.modules, top, None)
    for name in [m for m in sys.modules if m == "rtdp.agent" or m.startswith("rtdp.agent.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    import rtdp

    if hasattr(rtdp, "agent"):
        monkeypatch.delattr(rtdp, "agent")


# ------------------------------------------------------------------ import guard (default install)
def test_cli_agent_missing_extra_exits_2_with_hint(monkeypatch, capsys):
    _block_agent_sdk(monkeypatch)
    rc = main(["agent", "q"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--extra agent" in err
    assert "rtdp[agent]" in err


def test_cli_agent_partial_extra_still_gets_the_hint(monkeypatch, capsys):
    # langchain present but langchain-openai missing: the hint, not a later traceback.
    pytest.importorskip("langchain", reason="requires the optional [agent] extra")
    for name in [m for m in sys.modules if m.startswith(("langchain_openai", "rtdp.agent"))]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "langchain_openai", None)
    import rtdp

    if hasattr(rtdp, "agent"):
        monkeypatch.delattr(rtdp, "agent")
    assert main(["agent", "q"]) == 2
    assert "--extra agent" in capsys.readouterr().err


def test_cli_agent_reraises_non_sdk_import_errors(monkeypatch):
    # A failure inside our own module (name not an SDK) must surface, never show the hint.
    monkeypatch.setitem(sys.modules, "rtdp.agent.runtime", None)
    with pytest.raises(ImportError):
        main(["agent", "q"])


def test_core_cli_and_agent_tool_layer_work_with_sdk_blocked(monkeypatch, capsys):
    _block_agent_sdk(monkeypatch)
    assert main(["info"]) == 0
    assert "Resolved RTDP configuration" in capsys.readouterr().out
    # The tool layer (read-only HTTP tools + DQ re-derivation) needs no extra.
    tools = importlib.import_module("rtdp.agent.tools")
    assert "flights" in tools.static_registry().names()


# ------------------------------------------------------------------ CLI wiring ([agent] extra)

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
    from rtdp.agent.llm import ScriptedChatModel

    monkeypatch.setattr(
        "rtdp.agent.runtime.build_llm_client", lambda settings: ScriptedChatModel(script)
    )


def _answer_script():
    from langchain_core.messages import AIMessage
    from langchain_core.messages.tool import tool_call

    return [
        AIMessage(content="", tool_calls=[tool_call(name="flights", args={"icao24": "abc"},
                                                     id="c1")]),
        AIMessage(content="There is 1 record for abc."),
    ]


@requires_agent_extra
def test_cli_agent_one_shot(monkeypatch, capsys):
    _patch_api(monkeypatch)
    _patch_llm(monkeypatch, _answer_script())
    rc = main(["agent", "how many records for abc?"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "There is 1 record for abc." in out
    assert "Sources:" in out
    assert "GET /flights @ snapshot 42" in out


@requires_agent_extra
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


@requires_agent_extra
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


@requires_agent_extra
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


@requires_agent_extra
def test_cli_agent_missing_model_config_returns_2(monkeypatch, capsys):
    # API reachable, but no live model configured -> clear error (real build_llm_client raises).
    _patch_api(monkeypatch)
    rc = main(["agent", "q"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "No agent model configured" in err
