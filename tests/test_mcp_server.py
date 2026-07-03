"""MCP server tests — in-process against the real 2A app (file:// backend, no Docker/network).

The MCP tools are exercised end-to-end: ``server.call_tool`` -> httpx GET -> the real FastAPI
app (via an injected ``TestClient``, an ``httpx.Client`` subclass) -> ``rtdp.query`` -> a temp
file:// warehouse seeded with deterministic synthetic snapshots. Requires the optional
``[mcp]`` extra; the module skips cleanly on the default install (the CI ``mcp-extra`` job
runs it with the extra installed). No pytest-asyncio: each call runs under ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

pytest.importorskip("mcp", reason="requires the optional [mcp] extra")

from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from rtdp.api import create_app  # noqa: E402
from rtdp.ingest import run_ingest  # noqa: E402
from rtdp.mcp.server import build_server  # noqa: E402
from rtdp.sources.synthetic import SyntheticSource  # noqa: E402

EXPECTED_TOOLS = {
    "health",
    "list_flights",
    "list_flights_in_bbox",
    "flights_per_interval",
    "list_snapshots",
    "get_meta",
}

_STATE_VECTOR_FIELDS = {
    "icao24", "callsign", "origin_country", "event_time", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "geo_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "squawk", "spi", "position_source", "category",
    "ingest_time", "ingest_batch_id", "source_name",
}


def _seed(settings, n_rows, seed):
    return run_ingest(settings, SyntheticSource(n_rows=n_rows, seed=seed, inject_warnings=False))


@pytest.fixture
def mcp_env(file_settings):
    """Two snapshots (20 then 15 rows) so time-travel is exercisable; yields (server, r1, r2)."""
    r1 = _seed(file_settings, 20, 1)
    time.sleep(0.05)
    r2 = _seed(file_settings, 15, 2)
    app = create_app(file_settings)
    with TestClient(app) as client:
        yield build_server(file_settings, client=client), r1, r2


def _call(server, name, args=None) -> dict:
    """Run one tool call; all six tools have output schemas, so the result is a
    (content, structured) tuple — return the structured dict."""
    _content, structured = asyncio.run(server.call_tool(name, args or {}))
    return structured


def _mock_server(file_settings, handler) -> object:
    """An MCP server whose HTTP client never reaches a real app (offline error-path tests)."""
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")
    return build_server(file_settings, client=client)


# ------------------------------------------------------------------ tool schemas
def test_tool_inventory_and_schemas(mcp_env):
    server, r1, r2 = mcp_env
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    by_name = {t.name: t for t in tools}
    # Every tool advertises a structured output schema and declares itself read-only.
    assert all(t.outputSchema is not None for t in tools)
    assert all(t.annotations.readOnlyHint is True for t in tools)
    # bbox coordinates are the only required params anywhere, with the route's bounds.
    bbox_schema = by_name["list_flights_in_bbox"].inputSchema
    assert set(bbox_schema["required"]) == {"min_lat", "max_lat", "min_lon", "max_lon"}
    assert bbox_schema["properties"]["min_lat"]["minimum"] == -90
    assert bbox_schema["properties"]["min_lat"]["maximum"] == 90
    assert bbox_schema["properties"]["min_lon"]["minimum"] == -180
    assert bbox_schema["properties"]["min_lon"]["maximum"] == 180
    # interval/group_by are enums mirroring the route Literals.
    stats_props = by_name["flights_per_interval"].inputSchema["properties"]
    assert set(stats_props["interval"]["enum"]) == {"hour", "day"}


# ------------------------------------------------------------------ happy paths
def test_health_ok(mcp_env):
    server, r1, r2 = mcp_env
    data = _call(server, "health")
    assert data["status"] == "ok"
    assert data["catalog_reachable"] is True
    assert data["current_snapshot_id"] == r2.snapshot_id


def test_list_flights_payload(mcp_env):
    server, r1, r2 = mcp_env
    data = _call(server, "list_flights", {"limit": 1000})
    assert data["snapshot_id"] == r2.snapshot_id
    assert data["count"] == 35
    assert set(data["items"][0].keys()) == _STATE_VECTOR_FIELDS


def test_list_flights_time_travel(mcp_env):
    server, r1, r2 = mcp_env
    data = _call(server, "list_flights", {"as_of_snapshot_id": r1.snapshot_id, "limit": 1000})
    assert data["count"] == 20
    assert data["snapshot_id"] == r1.snapshot_id


def test_datetime_params_round_trip(mcp_env):
    # ISO-8601 strings coerce to datetime tool params and reach the API as filters.
    server, r1, r2 = mcp_env
    past = _call(server, "list_flights", {"start": "2000-01-01T00:00:00Z", "limit": 1000})
    assert past["count"] == 35
    future = _call(server, "list_flights", {"start": "2100-01-01T00:00:00Z", "limit": 1000})
    assert future["count"] == 0


def test_list_flights_in_bbox(mcp_env):
    server, r1, r2 = mcp_env
    data = _call(
        server,
        "list_flights_in_bbox",
        {"min_lat": -90, "max_lat": 90, "min_lon": -180, "max_lon": 180, "limit": 1000},
    )
    assert data["count"] >= 1
    assert set(data["items"][0].keys()) == _STATE_VECTOR_FIELDS


def test_flights_per_interval_grouped(mcp_env):
    server, r1, r2 = mcp_env
    data = _call(
        server, "flights_per_interval", {"interval": "day", "group_by": "origin_country"}
    )
    assert data["snapshot_id"] == r2.snapshot_id
    assert data["buckets"]
    assert all(b["group"] is not None for b in data["buckets"])


def test_list_snapshots_wrapped_result(mcp_env):
    # MCP structured content must be an object, so list returns arrive as {"result": [...]}.
    server, r1, r2 = mcp_env
    snaps = _call(server, "list_snapshots")["result"]
    assert {s["snapshot_id"] for s in snaps} == {r1.snapshot_id, r2.snapshot_id}
    assert all(s["operation"] == "append" for s in snaps)


def test_get_meta_schema_alias_round_trip(mcp_env):
    # MetaResponse dumps by alias in structured output: the wire key is "schema".
    server, r1, r2 = mcp_env
    data = _call(server, "get_meta")
    assert data["table_identifier"].endswith("opensky_state_vectors")
    assert "table_schema" not in data
    assert len(data["schema"]) == 21
    assert data["partition_spec"][0]["transform"] == "day"
    assert data["snapshot_count"] == 2


# ------------------------------------------------------------------ error paths
def test_as_of_conflict_surfaces_http_400(mcp_env):
    server, r1, r2 = mcp_env
    with pytest.raises(ToolError, match="HTTP 400"):
        asyncio.run(
            server.call_tool(
                "list_flights",
                {
                    "as_of_snapshot_id": r1.snapshot_id,
                    "as_of_timestamp": "2026-06-14T00:00:00Z",
                },
            )
        )


def test_timestamp_before_history_surfaces_http_404(mcp_env):
    server, r1, r2 = mcp_env
    with pytest.raises(ToolError, match="HTTP 404"):
        asyncio.run(
            server.call_tool("list_flights", {"as_of_timestamp": "2000-01-01T00:00:00Z"})
        )


def test_transport_error_mentions_serve_hint(file_settings):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    server = _mock_server(file_settings, handler)
    with pytest.raises(ToolError, match="rtdp serve"):
        asyncio.run(server.call_tool("health", {}))


def test_health_unhealthy_503_is_data_not_error(file_settings):
    # /health sends a valid HealthResponse body with 503 — that's a report, not a failure.
    body = {
        "status": "unavailable",
        "catalog_reachable": False,
        "table_loadable": False,
        "current_snapshot_id": None,
        "error": "catalog/table unavailable: boom",
    }
    server = _mock_server(file_settings, lambda r: httpx.Response(503, json=body))
    data = _call(server, "health")
    assert data["status"] == "unavailable"
    assert data["error"] == "catalog/table unavailable: boom"


def test_data_tool_503_raises_with_detail(file_settings):
    server = _mock_server(
        file_settings,
        lambda r: httpx.Response(503, json={"detail": "catalog/table unavailable: boom"}),
    )
    with pytest.raises(ToolError, match="HTTP 503.*catalog/table unavailable"):
        asyncio.run(server.call_tool("get_meta", {}))


# ------------------------------------------------------------------ serve() entry
@pytest.fixture
def unreachable_settings(file_settings):
    # Loopback port 1 refuses instantly — deterministic, no network egress.
    return file_settings.model_copy(update={"agent_api_url": "http://127.0.0.1:1"})


def test_serve_is_stderr_only_and_runs_stdio(unreachable_settings, monkeypatch, capsys):
    from rtdp.mcp import server as server_mod

    calls = {}

    def fake_run(self, transport=None, mount_path=None):
        calls["transport"] = transport

    monkeypatch.setattr(server_mod.FastMCP, "run", fake_run)
    assert server_mod.serve(unreachable_settings) == 0
    assert calls["transport"] == "stdio"
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is reserved for the JSON-RPC frames
    assert "not reachable" in captured.err  # preflight warns but the server still starts


def test_serve_handles_keyboard_interrupt_cleanly(unreachable_settings, monkeypatch):
    from rtdp.mcp import server as server_mod

    def fake_run(self, transport=None, mount_path=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(server_mod.FastMCP, "run", fake_run)
    assert server_mod.serve(unreachable_settings) == 0
