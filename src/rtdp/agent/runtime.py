"""Wiring/factories for the Stage D agent (kept apart from the pure loop/tool logic).

Builds the httpx client, the tool registry (from the live OpenAPI schema, falling back to the
static registry), the API tool executor, and the LLM client — all from
:class:`rtdp.config.Settings`. Configuration flows only through ``Settings``; there are no ad-hoc
environment reads here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Settings
from .loop import AgentResult, run_agent
from .tools import (
    ApiToolExecutor,
    ToolRegistry,
    build_registry_from_openapi,
    load_openapi,
    static_registry,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

# Sent when no key is configured (e.g. a local endpoint without auth). Passing it explicitly stops
# the OpenAI client from falling back to OPENAI_API_KEY in the environment, which would otherwise
# forward that key to whatever endpoint RTDP_AGENT_BASE_URL points at.
_NO_API_KEY = "not-configured"


def build_http_client(settings: Settings):
    """An ``httpx.Client`` for calling the read API (timeout from settings)."""
    import httpx

    return httpx.Client(timeout=settings.agent_timeout_seconds)


def build_registry(settings: Settings, client) -> ToolRegistry:
    """Derive tools from the live OpenAPI schema; fall back to the static registry on failure."""
    try:
        spec = load_openapi(settings.agent_api_base_url, client)
        return build_registry_from_openapi(spec)
    except Exception:
        return static_registry()


def build_llm_client(settings: Settings) -> BaseChatModel:
    """Construct the OpenAI-compatible chat model, or raise a clear error if unconfigured."""
    if not settings.agent_base_url or not settings.agent_model:
        raise RuntimeError(
            "No agent model configured. Set RTDP_AGENT_BASE_URL and RTDP_AGENT_MODEL "
            "(and RTDP_AGENT_API_KEY if the endpoint needs one). See the RUNBOOK Stage D section."
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.agent_model,
        base_url=settings.agent_base_url,
        api_key=settings.agent_api_key or _NO_API_KEY,
        temperature=settings.agent_temperature,
        timeout=settings.agent_timeout_seconds,
        max_retries=0,  # no silent retries: a quota or transport error surfaces immediately
    )


def answer_question(
    settings: Settings,
    question: str,
    *,
    llm: BaseChatModel | None = None,
    client=None,
) -> AgentResult:
    """Answer one question end-to-end. Injectable ``llm``/``client`` keep this offline-testable."""
    own_client = client is None
    client = client if client is not None else build_http_client(settings)
    try:
        registry = build_registry(settings, client)
        executor = ApiToolExecutor(
            settings.agent_api_base_url, client, registry, max_rows=settings.agent_max_rows
        )
        llm = llm if llm is not None else build_llm_client(settings)
        return run_agent(
            question,
            llm=llm,
            executor=executor,
            tools=registry.definitions(),
            max_turns=settings.agent_max_turns,
            max_tool_calls=settings.agent_max_tool_calls,
        )
    finally:
        if own_client:
            client.close()
