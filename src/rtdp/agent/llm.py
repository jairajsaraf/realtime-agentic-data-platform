"""Config-driven LLM client boundary for the Stage D agent.

The agent loop depends only on the small :class:`LLMClient` protocol (``chat`` over a list of
messages plus tool definitions). Two implementations are provided:

- :class:`OpenAICompatibleClient` — a thin client over the already-present ``httpx`` that talks to
  any OpenAI-compatible ``/chat/completions`` endpoint (an open dev model or a frontier model,
  chosen entirely by config). Provider-specific details live here and nowhere else.
- :class:`FakeLLMClient` — returns scripted responses for deterministic, offline tests (no network,
  no API key). This is the read-side analogue of the injected ``sleep``/fake sources used elsewhere
  in the codebase.

No model SDK is used or required.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    """One tool/function call requested by the model."""

    id: str
    name: str
    arguments: dict


@dataclass
class ChatMessage:
    """A single chat message in OpenAI-compatible shape."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages
    name: str | None = None  # tool name on role="tool" messages


@dataclass
class ChatResponse:
    """The model's reply: free text and/or tool calls, plus optional usage stats."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None


class LLMClient(Protocol):
    """Minimal chat interface the agent loop depends on."""

    def chat(self, messages: list[ChatMessage], tools: list[dict]) -> ChatResponse: ...


def _message_to_wire(msg: ChatMessage) -> dict:
    """Serialize a :class:`ChatMessage` to the OpenAI request shape."""
    # OpenAI requires "content" present (may be null on an assistant tool-call turn).
    wire: dict = {"role": msg.role, "content": msg.content}
    if msg.tool_calls:
        wire["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in msg.tool_calls
        ]
    if msg.tool_call_id is not None:
        wire["tool_call_id"] = msg.tool_call_id
    if msg.name is not None:
        wire["name"] = msg.name
    return wire


def _parse_tool_calls(raw_tool_calls: Iterable[dict] | None) -> list[ToolCall]:
    """Parse OpenAI tool_calls; bad JSON args degrade to ``{}`` for upstream validation."""
    calls: list[ToolCall] = []
    for raw in raw_tool_calls or []:
        fn = raw.get("function", {})
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(ToolCall(id=raw.get("id") or "", name=fn.get("name") or "", arguments=args))
    return calls


class OpenAICompatibleClient:
    """Thin OpenAI-compatible chat client over ``httpx`` (no model SDK)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = 60.0,
        temperature: float = 0.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._temperature = temperature

    def chat(self, messages: list[ChatMessage], tools: list[dict]) -> ChatResponse:
        import httpx  # lazy import (mirrors sources/opensky.py)

        body: dict = {
            "model": self._model,
            "messages": [_message_to_wire(m) for m in messages],
            "temperature": self._temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        resp = httpx.post(
            f"{self._base_url}/chat/completions",
            json=body,
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {}) or {}
        return ChatResponse(
            content=message.get("content"),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            usage=data.get("usage"),
        )


class FakeLLMClient:
    """Deterministic stub: returns pre-scripted responses in order.

    Pass either a list of :class:`ChatResponse` (consumed one per ``chat`` call) or a callable
    ``(messages, tools) -> ChatResponse``. Running past the end of a list raises ``IndexError``,
    surfacing an under-scripted test rather than hanging. Offline — no network or keys.
    """

    def __init__(
        self,
        responses: list[ChatResponse]
        | Callable[[list[ChatMessage], list[dict]], ChatResponse],
    ) -> None:
        self._responses = responses
        self._index = 0
        self.calls: list[tuple[list[ChatMessage], list[dict]]] = []

    def chat(self, messages: list[ChatMessage], tools: list[dict]) -> ChatResponse:
        self.calls.append((messages, tools))
        if callable(self._responses):
            return self._responses(messages, tools)
        if self._index >= len(self._responses):
            raise IndexError("FakeLLMClient ran out of scripted responses")
        resp = self._responses[self._index]
        self._index += 1
        return resp
