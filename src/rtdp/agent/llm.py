"""Chat-model boundary for the Stage D agent (requires the optional ``[agent]`` extra).

The agent loop (:mod:`rtdp.agent.loop`) drives any LangChain chat model. Two are used:

- :class:`ProviderExtrasChatOpenAI` (a ``ChatOpenAI`` from ``langchain-openai``), built by
  :func:`rtdp.agent.runtime.build_llm_client` against any OpenAI-compatible ``/chat/completions``
  endpoint — an open dev model or a frontier model, chosen entirely by config. Provider-specific
  details live there and nowhere else.
- :class:`ScriptedChatModel` — replays scripted ``AIMessage`` turns for deterministic, offline
  tests (no network, no API key). This is the read-side analogue of the injected ``sleep``/fake
  sources used elsewhere in the codebase.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import Field

_EXTRAS_KEY = "tool_call_extras"  # AIMessage.additional_kwargs key: {tool_call_id: extra_content}


class ProviderExtrasChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` that hands each tool call's ``extra_content`` back on the next request.

    Some OpenAI-compatible endpoints attach provider data to a tool call and require it on the
    replayed assistant message. Gemini 3 models return a thought signature at
    ``tool_calls[N].extra_content.google.thought_signature`` and answer HTTP 400 ("Function call
    is missing a thought_signature") when a later request omits it; ``ChatOpenAI`` parses the
    response and drops that field. This subclass keeps it, keyed by tool-call id, and writes it
    back unchanged. Endpoints that send no ``extra_content`` are unaffected: the payload is
    byte-identical to ``ChatOpenAI``'s.

    It relies on two private ``ChatOpenAI`` hooks (``_create_chat_result`` and
    ``_get_request_payload``), pinned by the lockfile and covered by tests. The response shape is
    documented only in community reports and the replay shape is its mirror image, so a live run
    is what verifies it.
    """

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get("choices") or [], strict=False):
            calls = (choice.get("message") or {}).get("tool_calls") or []
            extras = {
                call["id"]: call["extra_content"]
                for call in calls
                if call.get("id") and call.get("extra_content")
            }
            if extras and isinstance(generation.message, AIMessage):
                generation.message.additional_kwargs[_EXTRAS_KEY] = extras
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        extras: dict[str, Any] = {}
        for message in self._convert_input(input_).to_messages():
            if isinstance(message, AIMessage):
                extras.update(message.additional_kwargs.get(_EXTRAS_KEY) or {})
        if extras:
            for message in payload.get("messages") or []:
                for call in message.get("tool_calls") or []:
                    if call.get("id") in extras:
                        call["extra_content"] = extras[call["id"]]
        return payload


class ScriptedChatModel(GenericFakeChatModel):
    """Deterministic stub: returns pre-scripted ``AIMessage`` turns in order.

    Pass either a list of messages (consumed one per model call) or a callable
    ``(messages) -> AIMessage``. Running past the end of a list raises ``IndexError``, surfacing an
    under-scripted test rather than hanging. ``bind_tools`` is a no-op: the script, not the tool
    schema, decides which tools are "called". Offline — no network or keys.
    """

    responder: Callable[[list[BaseMessage]], AIMessage] | None = None
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def __init__(
        self,
        script: Sequence[AIMessage] | Callable[[list[BaseMessage]], AIMessage],
        **kwargs: Any,
    ) -> None:
        if callable(script):
            super().__init__(messages=iter(()), responder=script, **kwargs)
        else:
            super().__init__(messages=iter(list(script)), **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if self.responder is not None:
            return ChatResult(generations=[ChatGeneration(message=self.responder(messages))])
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except StopIteration:
            raise IndexError("ScriptedChatModel ran out of scripted responses") from None

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        return self
