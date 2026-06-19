"""Unit tests for the API tool layer (offline: httpx.MockTransport, captured OpenAPI fixture)."""

from __future__ import annotations

import httpx

from rtdp.agent.tools import (
    DIAGNOSE_TOOL_NAME,
    ApiToolExecutor,
    build_registry_from_openapi,
    static_registry,
)

# Minimal captured OpenAPI shape (FastAPI-style: anyOf for optionals, required flags).
OPENAPI = {
    "paths": {
        "/health": {"get": {"summary": "Health", "parameters": []}},
        "/flights": {
            "get": {
                "summary": "Typed flight reads",
                "parameters": [
                    {
                        "name": "icao24",
                        "in": "query",
                        "required": False,
                        "schema": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "description": "aircraft id",
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    },
                ],
            }
        },
        "/flights/bbox": {
            "get": {
                "summary": "Bounding box",
                "parameters": [
                    {"name": n, "in": "query", "required": True, "schema": {"type": "number"}}
                    for n in ("min_lat", "max_lat", "min_lon", "max_lon")
                ],
            }
        },
    }
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _noop_executor() -> ApiToolExecutor:
    return ApiToolExecutor(
        "http://test", _client(lambda r: httpx.Response(200)), static_registry()
    )


# ---------------------------------------------------------------- tool definitions
def test_build_registry_from_openapi_names_and_params():
    registry = build_registry_from_openapi(OPENAPI)
    assert set(registry.names()) == {"health", "flights", "flights_bbox", DIAGNOSE_TOOL_NAME}

    flights = registry.get("flights")
    assert flights.path == "/flights"
    assert flights.param_types["icao24"] == "string"  # anyOf unwrapped
    assert flights.param_types["limit"] == "integer"
    assert flights.required == ()

    bbox = registry.get("flights_bbox")
    assert bbox.required == ("min_lat", "max_lat", "min_lon", "max_lon")


def test_tool_to_openai_definition_shape():
    registry = build_registry_from_openapi(OPENAPI)
    definition = registry.get("flights_bbox").to_openai()
    assert definition["type"] == "function"
    fn = definition["function"]
    assert fn["name"] == "flights_bbox"
    assert set(fn["parameters"]["required"]) == {"min_lat", "max_lat", "min_lon", "max_lon"}


def test_static_registry_mirrors_endpoints():
    registry = static_registry()
    assert set(registry.names()) == {
        "health",
        "flights",
        "flights_bbox",
        "stats_flights_per_interval",
        "snapshots",
        "meta",
        DIAGNOSE_TOOL_NAME,
    }


# ---------------------------------------------------------------- execution
def test_execute_endpoint_preserves_snapshot_provenance():
    def handler(request):
        assert request.url.path == "/flights"
        return httpx.Response(
            200, json={"snapshot_id": 42, "count": 1, "items": [{"icao24": "abc"}]}
        )

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute("flights", {"icao24": "abc"})
    assert result.ok is True
    assert result.endpoint == "GET /flights"
    assert result.snapshot_id == 42
    assert result.data["items"][0]["icao24"] == "abc"


def test_execute_health_uses_current_snapshot_id_for_provenance():
    # /health returns current_snapshot_id (not snapshot_id); provenance must still be captured.
    def handler(request):
        return httpx.Response(200, json={"status": "ok", "current_snapshot_id": 99})

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute("health", {})
    assert result.ok is True
    assert result.snapshot_id == 99


def test_execute_meta_uses_current_snapshot_id_for_provenance():
    def handler(request):
        return httpx.Response(
            200,
            json={"table_identifier": "bronze.x", "current_snapshot_id": 7, "snapshot_count": 3},
        )

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute("meta", {})
    assert result.ok is True
    assert result.snapshot_id == 7


def test_execute_prefers_snapshot_id_over_current_when_both_present():
    def handler(request):
        return httpx.Response(
            200, json={"snapshot_id": 42, "current_snapshot_id": 1, "count": 0, "items": []}
        )

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute("flights", {})
    assert result.snapshot_id == 42  # flights/stats snapshot_id wins over current_snapshot_id


def test_execute_coerces_argument_types():
    seen: dict = {}

    def handler(request):
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json={"snapshot_id": 1, "count": 0, "items": []})

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    # limit declared integer; passed as a string -> coerced and forwarded as "5".
    result = executor.execute("flights", {"limit": "5"})
    assert result.ok is True
    assert seen["limit"] == "5"


def test_execute_unknown_tool_is_error():
    executor = _noop_executor()
    result = executor.execute("delete_everything", {})
    assert result.ok is False
    assert "unknown tool" in result.error


def test_execute_missing_required_param_is_error():
    executor = _noop_executor()
    result = executor.execute("flights_bbox", {"min_lat": 0})
    assert result.ok is False
    assert "missing required parameter" in result.error


def test_execute_http_error_surfaced():
    def handler(request):
        return httpx.Response(404, json={"detail": "snapshot not found"})

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute("flights", {"as_of_snapshot_id": 999})
    assert result.ok is False
    assert "HTTP 404" in result.error
    assert "snapshot not found" in result.error


def test_execute_transport_error_surfaced():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute("flights", {})
    assert result.ok is False
    assert "request failed" in result.error


def test_diagnose_tool_runs_dq_over_api_rows():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "snapshot_id": 7,
                "count": 2,
                "items": [{"icao24": "ok", "velocity": 100}, {"icao24": "fast", "velocity": 2200}],
            },
        )

    executor = ApiToolExecutor("http://test", _client(handler), static_registry())
    result = executor.execute(DIAGNOSE_TOOL_NAME, {})
    assert result.ok is True
    assert result.endpoint == f"agent:{DIAGNOSE_TOOL_NAME}"
    assert result.snapshot_id == 7
    assert result.data["ok"] is False
    assert any(f["kind"] == "over_speed" for f in result.data["findings"])
    assert result.data["proposals"]
    # The diagnose tool only reads the flights endpoint — no write/other endpoints touched.
    assert calls == ["/flights"]
