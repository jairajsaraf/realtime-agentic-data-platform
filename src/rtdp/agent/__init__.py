"""Stage D agentic layer: a natural-language agent that calls the Stage 2A read API as tools.

Strictly read-only and human-in-the-loop. The agent is an HTTP client of the serving API only —
it never imports the query/catalog/DuckDB/ingest/maintenance layers and adds no write surface.
"""

from __future__ import annotations

from .llm import (
    ChatMessage,
    ChatResponse,
    FakeLLMClient,
    LLMClient,
    OpenAICompatibleClient,
    ToolCall,
)
from .loop import AgentResult, Provenance, run_agent
from .runtime import answer_question, build_http_client, build_llm_client, build_registry
from .tools import (
    ApiTool,
    ApiToolExecutor,
    ToolRegistry,
    ToolResult,
    build_registry_from_openapi,
    load_openapi,
    static_registry,
)

__all__ = [
    "AgentResult",
    "ApiTool",
    "ApiToolExecutor",
    "ChatMessage",
    "ChatResponse",
    "FakeLLMClient",
    "LLMClient",
    "OpenAICompatibleClient",
    "Provenance",
    "ToolCall",
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
