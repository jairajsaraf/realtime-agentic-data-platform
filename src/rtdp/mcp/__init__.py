"""Optional read-only MCP server over the Stage 2A read API (requires the ``[mcp]`` extra).

This package is another HTTP client of the read API, exactly like the Stage D agent:
``MCP client -> rtdp mcp (stdio) -> Stage 2A HTTP API -> query -> catalog``. It never imports
``rtdp.query``, the catalog, DuckDB/Iceberg table APIs, ingestion, or maintenance code.

Import :mod:`rtdp.mcp.server` lazily — it requires the optional ``mcp`` SDK; this package
module deliberately does not, so non-MCP CLI paths never touch the extra.
"""
