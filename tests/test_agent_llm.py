"""Unit tests for the LLM client boundary (offline: fake client + monkeypatched httpx.post)."""

from __future__ import annotations

import httpx
import pytest

from rtdp.agent.llm import (
    ChatMessage,
    ChatResponse,
    FakeLLMClient,
    OpenAICompatibleClient,
    ToolCall,
    _message_to_wire,
    _parse_tool_calls,
)


def test_fake_llm_returns_scripted_in_order():
    r1, r2 = ChatResponse(content="a"), ChatResponse(content="b")
    fake = FakeLLMClient([r1, r2])
    assert fake.chat([], []) is r1
    assert fake.chat([], []) is r2
    assert len(fake.calls) == 2
    with pytest.raises(IndexError):
        fake.chat([], [])


def test_fake_llm_callable_form():
    fake = FakeLLMClient(lambda messages, tools: ChatResponse(content=f"n={len(messages)}"))
    assert fake.chat([ChatMessage("user", "hi")], []).content == "n=1"


def test_message_to_wire_and_parse_roundtrip():
    msg = ChatMessage("assistant", None, tool_calls=[ToolCall("c1", "flights", {"icao24": "abc"})])
    wire = _message_to_wire(msg)
    assert wire["role"] == "assistant"
    assert wire["content"] is None
    fn = wire["tool_calls"][0]["function"]
    assert fn["name"] == "flights"
    assert fn["arguments"] == '{"icao24": "abc"}'

    parsed = _parse_tool_calls(
        [{"id": "c1", "function": {"name": "flights", "arguments": '{"icao24":"abc"}'}}]
    )
    assert parsed[0].id == "c1"
    assert parsed[0].arguments == {"icao24": "abc"}


def test_parse_tool_calls_malformed_args_degrade_to_empty():
    parsed = _parse_tool_calls([{"id": "x", "function": {"name": "f", "arguments": "{not json"}}])
    assert parsed[0].arguments == {}


def test_openai_client_request_shape_and_response_parse(monkeypatch):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "hi",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "flights",
                                        "arguments": '{"icao24":"abc"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 5},
            }

    def fake_post(url, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleClient(
        base_url="http://llm.test/v1", api_key="secret", model="m1", timeout=12.0
    )
    tools = [{"type": "function", "function": {"name": "flights", "parameters": {}}}]
    resp = client.chat([ChatMessage("user", "q")], tools)

    assert captured["url"] == "http://llm.test/v1/chat/completions"
    assert captured["json"]["model"] == "m1"
    assert captured["json"]["tools"] == tools
    assert captured["json"]["tool_choice"] == "auto"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 12.0
    assert resp.content == "hi"
    assert resp.tool_calls[0].name == "flights"
    assert resp.tool_calls[0].arguments == {"icao24": "abc"}
    assert resp.usage == {"total_tokens": 5}


def test_openai_client_omits_tools_and_auth_when_absent(monkeypatch):
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "plain"}}]}

    def fake_post(url, json, headers, timeout):
        captured.update(json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleClient(base_url="http://x/v1", api_key=None, model="m")
    resp = client.chat([ChatMessage("user", "q")], [])

    assert "tools" not in captured["json"]
    assert "Authorization" not in captured["headers"]
    assert resp.content == "plain"
    assert resp.tool_calls == []
