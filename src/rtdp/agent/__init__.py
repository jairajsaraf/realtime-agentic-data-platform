"""Stage D agentic layer: a natural-language agent that calls the Stage 2A read API as tools.

Strictly read-only and human-in-the-loop. The agent is an HTTP client of the serving API only —
it never imports the query/catalog/DuckDB/ingest/maintenance layers and adds no write surface.

The tool layer (:mod:`rtdp.agent.tools`, :mod:`rtdp.agent.dq`) needs no extra. The loop, the
chat-model boundary, and the runtime wiring need the optional ``[agent]`` extra (LangChain), so
those names are imported lazily on first access (PEP 562) and a default install can still import
this package.
"""

from __future__ import annotations

from importlib import import_module

from .tools import (
    ApiTool,
    ApiToolExecutor,
    ToolRegistry,
    ToolResult,
    build_registry_from_openapi,
    load_openapi,
    static_registry,
)

_LAZY: dict[str, str] = {
    "ScriptedChatModel": ".llm",
    "AgentResult": ".loop",
    "Provenance": ".loop",
    "run_agent": ".loop",
    "answer_question": ".runtime",
    "build_http_client": ".runtime",
    "build_llm_client": ".runtime",
    "build_registry": ".runtime",
}


def __getattr__(name: str):
    if name in _LAZY:
        return getattr(import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AgentResult",
    "ApiTool",
    "ApiToolExecutor",
    "Provenance",
    "ScriptedChatModel",
    "ToolRegistry",
    "ToolResult",
    "answer_question",
    "build_http_client",
    "build_llm_client",
    "build_registry",
    "build_registry_from_openapi",
    "load_openapi",
    "run_agent",
    "static_registry",
]
