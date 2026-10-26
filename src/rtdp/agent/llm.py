"""Chat-model boundary for the Stage D agent (requires the optional ``[agent]`` extra).

The agent loop (:mod:`rtdp.agent.loop`) drives any LangChain chat model. Two are used:

- ``ChatOpenAI`` (``langchain-openai``), built by :func:`rtdp.agent.runtime.build_llm_client`
  against any OpenAI-compatible ``/chat/completions`` endpoint — an open dev model or a frontier
  model, chosen entirely by config. Provider-specific details live there and nowhere else.
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
from pydantic import Field


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
