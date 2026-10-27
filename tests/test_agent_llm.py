"""Unit tests for the chat-model boundary (offline: scripted model + mocked HTTP transport)."""

from __future__ import annotations

import json
import socket
import threading

import httpx
import pytest

pytest.importorskip("langchain", reason="requires the optional [agent] extra")

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from rtdp.agent.llm import ProviderExtrasChatOpenAI, ScriptedChatModel  # noqa: E402
from rtdp.agent.loop import run_agent  # noqa: E402
from rtdp.agent.runtime import build_llm_client  # noqa: E402
from rtdp.agent.tools import ToolResult, static_registry  # noqa: E402
from rtdp.config import Settings  # noqa: E402


def _settings(**overrides) -> Settings:
    base = {"agent_base_url": "http://llm.test/v1", "agent_model": "m1"}
    return Settings(_env_file=None, **{**base, **overrides})


# ------------------------------------------------------------------ scripted model
def test_scripted_model_returns_scripted_in_order():
    fake = ScriptedChatModel([AIMessage(content="a"), AIMessage(content="b")])
    assert fake.invoke("x").content == "a"
    assert fake.invoke("y").content == "b"
    assert len(fake.calls) == 2
    with pytest.raises(IndexError):
        fake.invoke("z")


def test_scripted_model_callable_form_and_noop_bind_tools():
    fake = ScriptedChatModel(lambda messages: AIMessage(content=f"n={len(messages)}"))
    assert fake.bind_tools([{"type": "function"}]) is fake
    assert fake.invoke([HumanMessage("hi")]).content == "n=1"


# ------------------------------------------------------------------ build_llm_client
def test_build_llm_client_requires_model_config():
    with pytest.raises(RuntimeError, match="No agent model configured"):
        build_llm_client(Settings(_env_file=None))


def test_build_llm_client_maps_settings_onto_chat_openai():
    model = build_llm_client(
        _settings(agent_api_key="test-key", agent_timeout_seconds=12.0, agent_temperature=0.0)
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "m1"
    assert model.openai_api_base == "http://llm.test/v1"
    assert model.openai_api_key.get_secret_value() == "test-key"
    assert model.temperature == 0.0
    assert model.request_timeout == 12.0
    assert model.max_retries == 0  # quota/transport errors surface, never silently retried


def test_build_llm_client_routes_through_env_https_proxy(monkeypatch):
    """The model client must honour HTTPS_PROXY (egress-restricted hosts depend on it).

    A local socket stands in for the proxy and records the CONNECT line; it answers 502, so the
    call fails fast and nothing leaves the machine.
    """
    seen: list[str] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def accept_once() -> None:
        conn, _ = server.accept()
        with conn:
            seen.append(conn.recv(1024).split(b"\r\n")[0].decode())
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    proxy = f"http://127.0.0.1:{server.getsockname()[1]}"
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "localhost")
    model = build_llm_client(
        _settings(agent_base_url="https://llm.example.test/v1", agent_timeout_seconds=5.0)
    )
    try:
        with pytest.raises(Exception):  # noqa: B017 — the stand-in proxy refuses the tunnel
            model.invoke("hi")
        thread.join(timeout=5)
    finally:
        server.close()
    assert seen == ["CONNECT llm.example.test:443 HTTP/1.1"]


def test_build_llm_client_never_falls_back_to_openai_env_key(monkeypatch):
    # Config flows only through Settings: an OPENAI_API_KEY in the environment must not be
    # forwarded to whatever endpoint RTDP_AGENT_BASE_URL names.
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-must-not-be-used")
    model = build_llm_client(_settings())
    assert model.openai_api_key.get_secret_value() == "not-configured"


# ------------------------------------------------------------------ OpenAI wire shape (offline)
def _completion(message: dict, total_tokens: int) -> dict:
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "m1",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": total_tokens - 1, "completion_tokens": 1,
                  "total_tokens": total_tokens},
    }


def test_chat_openai_wire_shape_and_bad_json_tool_args_degrade(monkeypatch):
    """Drive the real ChatOpenAI over a mocked transport: request shape + response parsing."""
    requests: list[httpx.Request] = []
    replies = iter(
        [
            _completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "flights_bbox", "arguments": '{"min_lat": 4'},
                        }
                    ],
                },
                total_tokens=20,
            ),
            _completion({"role": "assistant", "content": "The box was missing."}, total_tokens=9),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(replies))

    model = ChatOpenAI(
        model="m1", base_url="http://llm.test/v1", api_key="test-key", temperature=0.0,
        max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    class _Executor:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(name, "GET /flights/bbox", arguments, ok=False,
                              error="missing required parameter 'min_lat'")

    executor = _Executor()
    result = run_agent(
        "flights over the box?", llm=model, executor=executor,
        tools=static_registry().definitions(),
    )

    first = json.loads(requests[0].content)
    assert str(requests[0].url) == "http://llm.test/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert first["model"] == "m1"
    assert first["temperature"] == 0.0
    assert {t["function"]["name"] for t in first["tools"]} >= {"flights", "flights_bbox"}
    assert first["messages"][0]["role"] == "system"
    # Malformed JSON args degrade to {} and the executor's validation answers, as before.
    assert executor.calls == [("flights_bbox", {})]
    assert result.answer == "The box was missing."
    assert result.tokens == 29
    assert result.citation_line() == "Sources: GET /flights/bbox"


# ------------------------------------------------------------------ provider extras round-trip
_SIG = {"google": {"thought_signature": "c2lnbmF0dXJl"}}


def _tool_call(call_id: str, name: str, extra: dict | None = None) -> dict:
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
    if extra is not None:
        call["extra_content"] = extra
    return call


def _scripted_endpoint(first_message: dict):
    """A mocked chat endpoint: first reply is `first_message`, the second is a final answer.

    Like Gemini 3, it answers HTTP 400 when a replayed assistant tool call lacks its signature.
    """
    requests: list[dict] = []
    replies = iter([first_message, {"role": "assistant", "content": "done"}])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        for message in body["messages"]:
            for call in message.get("tool_calls") or []:
                if call["id"] == "sig-call" and call.get("extra_content") != _SIG:
                    return httpx.Response(400, json={"error": {
                        "code": 400,
                        "message": "Function call is missing a thought_signature in functionCall "
                                   "parts."}})
        return httpx.Response(200, json=_completion(next(replies), total_tokens=5))

    return requests, httpx.Client(transport=httpx.MockTransport(handler))


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append(name)
        return ToolResult(name, f"GET /{name}", arguments, ok=True, snapshot_id=1, data={})


def _run(model, executor):
    return run_agent("q", llm=model, executor=executor, tools=static_registry().definitions())


def _model(http_client, cls=ProviderExtrasChatOpenAI):
    return cls(model="m", base_url="http://llm.test/v1", api_key="k", max_retries=0,
               http_client=http_client)


def test_extra_content_is_replayed_on_the_next_request():
    first = {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("sig-call", "flights", _SIG)]}
    requests, client = _scripted_endpoint(first)
    executor = _Executor()
    result = _run(_model(client), executor)

    assert result.answer == "done" and executor.calls == ["flights"]
    replayed = [m for m in requests[1]["messages"] if m["role"] == "assistant"][0]
    assert replayed["tool_calls"][0]["extra_content"] == _SIG
    # The first request has nothing to replay yet.
    assert not any("extra_content" in str(m) for m in requests[0]["messages"])


def test_plain_chat_openai_drops_the_signature_and_the_endpoint_rejects_it():
    # The failure this class exists for: the stock client loses extra_content between turns.
    first = {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("sig-call", "flights", _SIG)]}
    _, client = _scripted_endpoint(first)
    with pytest.raises(Exception, match="thought_signature"):
        _run(_model(client, cls=ChatOpenAI), _Executor())


def test_only_the_tool_call_that_carried_extra_content_gets_it_back():
    first = {"role": "assistant", "content": None,
             "tool_calls": [_tool_call("sig-call", "flights", _SIG), _tool_call("c2", "snapshots")]}
    requests, client = _scripted_endpoint(first)
    _run(_model(client), _Executor())
    calls = [m for m in requests[1]["messages"] if m["role"] == "assistant"][0]["tool_calls"]
    assert calls[0]["extra_content"] == _SIG and "extra_content" not in calls[1]


def test_payload_is_identical_to_chat_openai_when_no_extra_content_was_sent():
    from langchain_core.messages import HumanMessage

    first = {"role": "assistant", "content": None, "tool_calls": [_tool_call("c1", "flights")]}
    requests, client = _scripted_endpoint(first)
    _run(_model(client), _Executor())
    plain_requests, plain_client = _scripted_endpoint(first)
    _run(_model(plain_client, cls=ChatOpenAI), _Executor())
    assert requests == plain_requests
    message = HumanMessage("hi")
    ours = _model(httpx.Client())._get_request_payload([message])
    stock = _model(httpx.Client(), cls=ChatOpenAI)._get_request_payload([message])
    assert ours == stock


def test_build_llm_client_returns_the_extras_preserving_model():
    assert isinstance(build_llm_client(_settings()), ProviderExtrasChatOpenAI)
