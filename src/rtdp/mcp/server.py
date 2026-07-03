"""Read-only MCP server exposing the Stage 2A read endpoints as typed MCP tools.

The server is a thin HTTP client of the read API — the same seat the Stage D agent occupies:
``MCP client -> rtdp mcp (stdio) -> Stage 2A HTTP API -> query -> catalog``. Each tool maps
1:1 onto an existing GET endpoint; parameters mirror the route signatures in
:mod:`rtdp.api.routes` and results are validated against the shared response models in
:mod:`rtdp.api.models` (pure Pydantic), which double as MCP structured-output schemas. There
is deliberately no write/mutation tool and no other rtdp import — the API stays authoritative
for validation, limits, and time-travel semantics.

stdio discipline: stdout carries the JSON-RPC frames, so nothing in this module may print to
stdout — banners, preflight warnings, and diagnostics all go to stderr.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from ..api.models import (
    FlightsResponse,
    HealthResponse,
    MetaResponse,
    SnapshotItem,
    StatsResponse,
)
from ..config import Settings

_READ_ONLY = ToolAnnotations(readOnlyHint=True)

_INSTRUCTIONS = (
    "Read-only tools over the rtdp Stage 2A flight-data API (Iceberg lakehouse, synthetic "
    "OpenSky-shaped state vectors). All tools are HTTP GETs against a running `rtdp serve`; "
    "nothing can write or mutate. Time-travel: pass as_of_snapshot_id OR as_of_timestamp "
    "(never both); list_snapshots shows what is available."
)

# Shared parameter annotations mirroring the route constraints in rtdp.api.routes.
StartQ = Annotated[
    datetime | None, Field(description="Inclusive UTC lower bound on event_time (ISO-8601).")
]
EndQ = Annotated[
    datetime | None, Field(description="Inclusive UTC upper bound on event_time (ISO-8601).")
]
LimitQ = Annotated[
    int | None, Field(ge=1, description="Page size; the API caps it at its configured maximum.")
]
OffsetQ = Annotated[int, Field(ge=0, description="Page offset.")]
AsOfSnapQ = Annotated[
    int | None, Field(description="Read this Iceberg snapshot id (time-travel).")
]
AsOfTsQ = Annotated[
    datetime | None,
    Field(
        description="Read the newest snapshot at or before this UTC time (time-travel). "
        "Mutually exclusive with as_of_snapshot_id."
    ),
]
LatQ = Annotated[float, Field(ge=-90, le=90)]
LonQ = Annotated[float, Field(ge=-180, le=180)]


def _params(**kwargs) -> dict:
    """Drop ``None`` values and serialize datetimes to ISO-8601 for the query string."""
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v)
        for k, v in kwargs.items()
        if v is not None
    }


def _detail(resp: httpx.Response) -> str:
    """The API's ``detail`` message (422 details are lists — JSON-dump those), else raw text."""
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return resp.text[:500]
    if detail is None:
        return resp.text[:500]
    return detail if isinstance(detail, str) else json.dumps(detail, default=str)


def _get(
    client: httpx.Client,
    path: str,
    params: dict | None = None,
    *,
    ok_statuses: tuple[int, ...] = (),
) -> httpx.Response:
    """GET ``path`` and surface failures as :class:`ToolError` (becomes an MCP tool error)."""
    try:
        resp = client.get(path, params=params)
    except httpx.HTTPError as exc:
        raise ToolError(
            f"read API request failed (GET {path}): {exc}. "
            "Is the API running? Start it with `rtdp serve`."
        ) from exc
    if not (200 <= resp.status_code < 300) and resp.status_code not in ok_statuses:
        raise ToolError(f"GET {path} -> HTTP {resp.status_code}: {_detail(resp)}")
    return resp


def build_server(settings: Settings, client: httpx.Client | None = None) -> FastMCP:
    """Create the MCP server with six read-only tools bound to an HTTP client.

    When ``client`` is ``None`` an ``httpx.Client`` is created against
    ``settings.agent_api_base_url``. Tools use relative paths, so tests can inject a
    ``fastapi.testclient.TestClient`` (an ``httpx.Client`` subclass) and exercise the real
    app in-process. The caller owns the client's lifetime (see :func:`serve`).
    """
    if client is None:
        client = httpx.Client(
            base_url=settings.agent_api_base_url, timeout=settings.agent_timeout_seconds
        )
    server = FastMCP("rtdp", instructions=_INSTRUCTIONS)

    @server.tool(annotations=_READ_ONLY)
    def health() -> HealthResponse:
        """Liveness of the read API: catalog/table reachability and current snapshot id.

        Returns the health report even when unhealthy — the API sends a valid body with
        HTTP 503, and an unhealthy report is data, not a tool failure.
        """
        resp = _get(client, "/health", ok_statuses=(503,))
        try:
            return HealthResponse.model_validate(resp.json())
        except ValueError as exc:
            raise ToolError(
                f"GET /health -> HTTP {resp.status_code}: unparseable body"
            ) from exc

    @server.tool(annotations=_READ_ONLY)
    def list_flights(
        icao24: Annotated[str | None, Field(description="Exact aircraft id.")] = None,
        callsign: Annotated[str | None, Field(description="Exact callsign.")] = None,
        start: StartQ = None,
        end: EndQ = None,
        limit: LimitQ = None,
        offset: OffsetQ = 0,
        as_of_snapshot_id: AsOfSnapQ = None,
        as_of_timestamp: AsOfTsQ = None,
    ) -> FlightsResponse:
        """Typed flight/state-vector reads with optional filters and Iceberg time-travel."""
        resp = _get(
            client,
            "/flights",
            _params(
                icao24=icao24,
                callsign=callsign,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
                as_of_snapshot_id=as_of_snapshot_id,
                as_of_timestamp=as_of_timestamp,
            ),
        )
        return FlightsResponse.model_validate(resp.json())

    @server.tool(annotations=_READ_ONLY)
    def list_flights_in_bbox(
        min_lat: LatQ,
        max_lat: LatQ,
        min_lon: LonQ,
        max_lon: LonQ,
        start: StartQ = None,
        end: EndQ = None,
        limit: LimitQ = None,
        offset: OffsetQ = 0,
        as_of_snapshot_id: AsOfSnapQ = None,
        as_of_timestamp: AsOfTsQ = None,
    ) -> FlightsResponse:
        """Flights within a lat/lon bounding box, with optional time window and time-travel."""
        resp = _get(
            client,
            "/flights/bbox",
            _params(
                min_lat=min_lat,
                max_lat=max_lat,
                min_lon=min_lon,
                max_lon=max_lon,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
                as_of_snapshot_id=as_of_snapshot_id,
                as_of_timestamp=as_of_timestamp,
            ),
        )
        return FlightsResponse.model_validate(resp.json())

    @server.tool(annotations=_READ_ONLY)
    def flights_per_interval(
        interval: Literal["hour", "day"] = "hour",
        start: StartQ = None,
        end: EndQ = None,
        group_by: Literal["origin_country"] | None = None,
        as_of_snapshot_id: AsOfSnapQ = None,
        as_of_timestamp: AsOfTsQ = None,
    ) -> StatsResponse:
        """Counts of state vectors per hour or day, optionally grouped by origin_country."""
        resp = _get(
            client,
            "/stats/flights-per-interval",
            _params(
                interval=interval,
                start=start,
                end=end,
                group_by=group_by,
                as_of_snapshot_id=as_of_snapshot_id,
                as_of_timestamp=as_of_timestamp,
            ),
        )
        return StatsResponse.model_validate(resp.json())

    @server.tool(annotations=_READ_ONLY)
    def list_snapshots() -> list[SnapshotItem]:
        """All Iceberg snapshots (oldest first) — ids usable for time-travel reads."""
        resp = _get(client, "/snapshots")
        return [SnapshotItem.model_validate(item) for item in resp.json()]

    @server.tool(annotations=_READ_ONLY)
    def get_meta() -> MetaResponse:
        """Table metadata: identifier, snapshot pointer/count, schema, and partition spec."""
        resp = _get(client, "/meta")
        return MetaResponse.model_validate(resp.json())

    return server


def serve(settings: Settings) -> int:
    """Run the MCP server on stdio. Requires a reachable read API (start with ``rtdp serve``).

    The preflight health probe only warns on failure — MCP clients often launch stdio servers
    before the API is up, and every tool returns a clear error until it is.
    """
    base_url = settings.agent_api_base_url
    client = httpx.Client(base_url=base_url, timeout=settings.agent_timeout_seconds)
    try:
        print(
            f"rtdp MCP server (stdio): read-only tools over the read API at {base_url}.",
            file=sys.stderr,
        )
        try:
            resp = client.get("/health")
            if resp.status_code != 200:
                print(
                    f"WARNING: read API at {base_url} reports HTTP {resp.status_code}; "
                    "tools may return errors until it is healthy.",
                    file=sys.stderr,
                )
        except httpx.HTTPError as exc:
            print(
                f"WARNING: read API not reachable at {base_url}: {exc}. "
                "Tools will return errors until `rtdp serve` is running.",
                file=sys.stderr,
            )
        server = build_server(settings, client=client)
        try:
            server.run(transport="stdio")
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        client.close()
