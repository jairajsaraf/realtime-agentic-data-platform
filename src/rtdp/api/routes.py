"""HTTP routes — a thin transport over :mod:`rtdp.query`.

No read/query logic lives here: each handler validates inputs (via typed params),
calls a ``rtdp.query`` function, and maps the result onto a typed response model.
Time-travel selectors are accepted on every data endpoint; the resolved ``snapshot_id``
is echoed back so a caller always knows which snapshot answered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pyiceberg.table import Table

from .. import query
from ..catalog import build_catalog
from ..config import Settings
from .models import (
    FlightsResponse,
    HealthResponse,
    LivenessResponse,
    MetaResponse,
    SnapshotItem,
    StatsResponse,
)

router = APIRouter()


# ------------------------------------------------------------------- dependencies
def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_table(request: Request) -> Table:
    """Load the bronze table per request (cheap metadata read; picks up new snapshots).

    The catalog is built once at startup and cached on ``app.state``; if that failed we
    retry lazily here. Any catalog/table failure surfaces as HTTP 503.
    """
    settings: Settings = request.app.state.settings
    catalog = request.app.state.catalog
    try:
        if catalog is None:
            catalog = build_catalog(settings)
            request.app.state.catalog = catalog
        return catalog.load_table(settings.table_identifier)
    except Exception as exc:  # noqa: BLE001 — any load failure is reported as unavailable
        raise HTTPException(status_code=503, detail=f"catalog/table unavailable: {exc}") from exc


def _clamp_limit(limit: int | None, settings: Settings) -> int:
    # The configured default is itself capped at api_max_limit, so a deployment that
    # sets api_default_limit above api_max_limit can't bypass the advertised cap.
    effective = settings.api_default_limit if limit is None else limit
    return min(effective, settings.api_max_limit)


# Shared, reusable query-parameter annotations.
StartQ = Annotated[datetime | None, Query(description="Inclusive lower bound on event_time.")]
EndQ = Annotated[datetime | None, Query(description="Inclusive upper bound on event_time.")]
LimitQ = Annotated[int | None, Query(ge=1, description="Page size; capped at api_max_limit.")]
OffsetQ = Annotated[int, Query(ge=0, description="Page offset.")]
AsOfSnapQ = Annotated[int | None, Query(description="Read this snapshot id (time-travel).")]
AsOfTsQ = Annotated[
    datetime | None,
    Query(description="Read the newest snapshot at or before this UTC time (time-travel)."),
]


# ------------------------------------------------------------------------ routes
@router.get("/livez", response_model=LivenessResponse, tags=["meta"])
def livez() -> LivenessResponse:
    """Liveness: 200 whenever the process is serving. Deliberately touches no catalog, table,
    object storage, or other external state, so a fresh host with no table yet still passes the
    Docker healthcheck and the deploy gate WITHOUT starting ingestion. Data readiness stays on
    /health (503 until the catalog + table are loadable)."""
    return LivenessResponse()


@router.get("/health", response_model=HealthResponse, tags=["meta"])
def health(request: Request, response: Response) -> HealthResponse:
    """Readiness: 200 when catalog + table are reachable, 503 otherwise."""
    res = query.health(get_settings(request))
    if res.status != "ok":
        response.status_code = 503
    return HealthResponse(
        status=res.status,
        catalog_reachable=res.catalog_reachable,
        table_loadable=res.table_loadable,
        current_snapshot_id=res.current_snapshot_id,
        error=res.error,
    )


@router.get("/flights", response_model=FlightsResponse, tags=["flights"])
def flights(
    request: Request,
    table: Annotated[Table, Depends(get_table)],
    icao24: Annotated[str | None, Query(description="Exact aircraft id (scan pushdown).")] = None,
    callsign: Annotated[str | None, Query(description="Exact callsign (scan pushdown).")] = None,
    start: StartQ = None,
    end: EndQ = None,
    limit: LimitQ = None,
    offset: OffsetQ = 0,
    as_of_snapshot_id: AsOfSnapQ = None,
    as_of_timestamp: AsOfTsQ = None,
) -> FlightsResponse:
    """Filtered flight reads. icao24/callsign/time-window are pushed into the Iceberg scan."""
    settings = get_settings(request)
    res = query.query_flights(
        table,
        icao24=icao24,
        callsign=callsign,
        start=start,
        end=end,
        limit=_clamp_limit(limit, settings),
        offset=offset,
        as_of_snapshot_id=as_of_snapshot_id,
        as_of_timestamp=as_of_timestamp,
    )
    return FlightsResponse(snapshot_id=res.snapshot_id, count=res.count, items=res.items)


@router.get("/flights/bbox", response_model=FlightsResponse, tags=["flights"])
def flights_bbox(
    request: Request,
    table: Annotated[Table, Depends(get_table)],
    min_lat: Annotated[float, Query(ge=-90, le=90)],
    max_lat: Annotated[float, Query(ge=-90, le=90)],
    min_lon: Annotated[float, Query(ge=-180, le=180)],
    max_lon: Annotated[float, Query(ge=-180, le=180)],
    start: StartQ = None,
    end: EndQ = None,
    limit: LimitQ = None,
    offset: OffsetQ = 0,
    as_of_snapshot_id: AsOfSnapQ = None,
    as_of_timestamp: AsOfTsQ = None,
) -> FlightsResponse:
    """Bounding-box reads. Time window is pushed into the scan; the lat/lon box runs in DuckDB."""
    settings = get_settings(request)
    res = query.query_bbox(
        table,
        min_lat=min_lat,
        max_lat=max_lat,
        min_lon=min_lon,
        max_lon=max_lon,
        start=start,
        end=end,
        limit=_clamp_limit(limit, settings),
        offset=offset,
        as_of_snapshot_id=as_of_snapshot_id,
        as_of_timestamp=as_of_timestamp,
    )
    return FlightsResponse(snapshot_id=res.snapshot_id, count=res.count, items=res.items)


@router.get("/stats/flights-per-interval", response_model=StatsResponse, tags=["stats"])
def stats_flights_per_interval(
    table: Annotated[Table, Depends(get_table)],
    interval: Annotated[Literal["hour", "day"], Query()] = "hour",
    start: StartQ = None,
    end: EndQ = None,
    group_by: Annotated[Literal["origin_country"] | None, Query()] = None,
    as_of_snapshot_id: AsOfSnapQ = None,
    as_of_timestamp: AsOfTsQ = None,
) -> StatsResponse:
    """Flights-per-interval (hour|day); optional group_by=origin_country. GROUP BY in DuckDB."""
    res = query.query_stats_per_interval(
        table,
        interval=interval,
        start=start,
        end=end,
        group_by=group_by,
        as_of_snapshot_id=as_of_snapshot_id,
        as_of_timestamp=as_of_timestamp,
    )
    return StatsResponse(snapshot_id=res.snapshot_id, buckets=res.buckets)


@router.get("/snapshots", response_model=list[SnapshotItem], tags=["meta"])
def snapshots(table: Annotated[Table, Depends(get_table)]) -> list[SnapshotItem]:
    """All Iceberg snapshots from table metadata (oldest first)."""
    return [
        SnapshotItem(
            snapshot_id=s.snapshot_id,
            timestamp=s.timestamp,
            operation=s.operation,
            summary=s.summary,
        )
        for s in query.list_snapshots(table)
    ]


@router.get("/meta", response_model=MetaResponse, tags=["meta"])
def meta(table: Annotated[Table, Depends(get_table)]) -> MetaResponse:
    """Table identifier, snapshot pointer/count, schema, and partition spec (metadata-only)."""
    m = query.table_meta(table)
    return MetaResponse(
        table_identifier=m.table_identifier,
        current_snapshot_id=m.current_snapshot_id,
        snapshot_count=m.snapshot_count,
        table_schema=m.schema,
        partition_spec=m.partition_spec,
    )
