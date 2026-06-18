"""Transport-agnostic read logic over the bronze Iceberg table.

This is the read-side mirror of Stage 1's "thin writer boundary": every read — filters,
geographic bounding-box, interval aggregations, snapshot/time-travel, table metadata, and
health — lives here as a small, testable function. The FastAPI layer (``rtdp.api``) is a
thin transport over these functions, and the CLI and a future agent reuse the same code.

Dependency direction is ``api -> query -> (catalog, config, schema)``, never the reverse.

Predicate-pushdown note (a deliberate, partition-aware design choice): equality and
time-window predicates are pushed into the pyiceberg ``row_filter`` so the scan can prune
``day(event_time)`` partitions, instead of materializing the whole table and filtering in
DuckDB. DuckDB is reserved for the SQL-shaped work it is good at: bounding-box predicates,
``GROUP BY`` interval aggregations, and ordered ``LIMIT``/``OFFSET`` paging. At this data
scale either layer would return correct results — the point is demonstrating reads that
push work down to the table format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import duckdb
import pyarrow as pa
from pyiceberg.expressions import (
    AlwaysTrue,
    And,
    BooleanExpression,
    EqualTo,
    GreaterThanOrEqual,
    LessThanOrEqual,
)
from pyiceberg.table import Table

from .catalog import build_catalog
from .config import Settings
from .schema import BRONZE_SCHEMA

# All bronze columns, in schema order — mirrors src/rtdp/schema.py (never invented here).
# Used as ``selected_fields`` for the flight endpoints (the response returns every column).
FLIGHT_FIELDS: tuple[str, ...] = tuple(f.name for f in BRONZE_SCHEMA.fields)

# Inputs that get interpolated into DuckDB SQL are constrained to these allow-lists so the
# transport-agnostic layer is safe even if a caller bypasses the API's typed validation.
INTERVALS: tuple[str, ...] = ("hour", "day")
GROUP_BY_FIELDS: tuple[str, ...] = ("origin_country",)


# --------------------------------------------------------------------------- errors
class AsOfConflictError(ValueError):
    """Both ``as_of_snapshot_id`` and ``as_of_timestamp`` were supplied (API -> 400)."""


class SnapshotNotFoundError(LookupError):
    """No snapshot matched the requested id/timestamp (API -> 404)."""


# --------------------------------------------------------------------------- results
@dataclass
class FlightsResult:
    snapshot_id: int | None
    count: int  # number of items RETURNED (after limit/offset), not total matches
    items: list[dict]


@dataclass
class StatsResult:
    snapshot_id: int | None
    buckets: list[dict]


@dataclass
class SnapshotInfo:
    snapshot_id: int
    timestamp: datetime
    operation: str | None
    summary: dict[str, str]


@dataclass
class TableMeta:
    table_identifier: str
    current_snapshot_id: int | None
    snapshot_count: int
    schema: list[dict]
    partition_spec: list[dict]


@dataclass
class HealthResult:
    status: str
    catalog_reachable: bool
    table_loadable: bool
    current_snapshot_id: int | None
    error: str | None = field(default=None)


# --------------------------------------------------------------------------- helpers
def load_bronze_table(settings: Settings) -> Table:
    """Build the catalog and load the configured bronze table (read path)."""
    catalog = build_catalog(settings)
    return catalog.load_table(settings.table_identifier)


def _to_utc(dt: datetime) -> datetime:
    """Normalize to timezone-aware UTC (event_time is a timestamptz column)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def build_row_filter(
    *,
    icao24: str | None = None,
    callsign: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BooleanExpression:
    """Translate supported filters into a pyiceberg ``row_filter`` expression.

    These predicates are pushed into ``table.scan(...)`` so ``day(event_time)`` partition
    pruning fires. Returns :class:`AlwaysTrue` when no filters are supplied. ``And`` folds
    over :class:`AlwaysTrue`, so the result is always the tightest expression.
    """
    predicates: list[BooleanExpression] = []
    if icao24 is not None:
        predicates.append(EqualTo("icao24", icao24))
    if callsign is not None:
        predicates.append(EqualTo("callsign", callsign))
    if start is not None:
        predicates.append(GreaterThanOrEqual("event_time", _to_utc(start)))
    if end is not None:
        predicates.append(LessThanOrEqual("event_time", _to_utc(end)))
    if not predicates:
        return AlwaysTrue()
    expr = predicates[0]
    for pred in predicates[1:]:
        expr = And(expr, pred)
    return expr


def resolve_snapshot_id(
    table: Table,
    *,
    as_of_snapshot_id: int | None = None,
    as_of_timestamp: datetime | None = None,
) -> int | None:
    """Resolve which snapshot a read should target.

    - both selectors set -> :class:`AsOfConflictError` (API maps to 400).
    - ``as_of_snapshot_id`` -> that id, validated to exist (else :class:`SnapshotNotFoundError`).
    - ``as_of_timestamp`` -> the newest snapshot with ``timestamp_ms <= as_of`` (else
      :class:`SnapshotNotFoundError`); ties at the same millisecond resolve to the newest
      commit by sequence number (matching pyiceberg's own ``snapshot_as_of_timestamp``).
      Snapshot-id reads are the primary, deterministic path; timestamp resolution is the
      convenience layer on top.
    - neither -> ``None`` (read the current snapshot — the scan default).
    """
    if as_of_snapshot_id is not None and as_of_timestamp is not None:
        raise AsOfConflictError("Provide at most one of as_of_snapshot_id, as_of_timestamp.")
    if as_of_snapshot_id is not None:
        if table.snapshot_by_id(as_of_snapshot_id) is None:
            raise SnapshotNotFoundError(f"snapshot_id {as_of_snapshot_id} not found")
        return as_of_snapshot_id
    if as_of_timestamp is not None:
        as_of_ms = int(_to_utc(as_of_timestamp).timestamp() * 1000)
        candidates = [s for s in table.metadata.snapshots if s.timestamp_ms <= as_of_ms]
        if not candidates:
            raise SnapshotNotFoundError(
                f"no snapshot at or before {_to_utc(as_of_timestamp).isoformat()}"
            )
        # Tie-break equal timestamps by sequence number so fast same-millisecond appends
        # resolve to the newest commit. pyiceberg types sequence_number as int | None; fall
        # back to -1 when it is missing/None so a snapshot with a real sequence wins the tie
        # (degrading at worst to metadata order if every candidate lacks one).
        def _seq(s) -> int:
            seq = getattr(s, "sequence_number", None)
            return seq if seq is not None else -1

        return max(candidates, key=lambda s: (s.timestamp_ms, _seq(s))).snapshot_id
    return None


def _effective_snapshot_id(table: Table, resolved: int | None) -> int | None:
    """The snapshot id actually answering a query (echoed in every response)."""
    if resolved is not None:
        return resolved
    current = table.current_snapshot()
    return current.snapshot_id if current else None


def _scan_arrow(
    table: Table, snapshot_id: int | None, row_filter: BooleanExpression, fields: tuple[str, ...]
) -> pa.Table:
    return table.scan(
        snapshot_id=snapshot_id, row_filter=row_filter, selected_fields=fields
    ).to_arrow()


def _connect_utc() -> duckdb.DuckDBPyConnection:
    """A DuckDB connection pinned to UTC.

    ``event_time``/``ingest_time`` are stored as UTC ``timestamptz``. DuckDB's ``date_trunc``
    and timestamptz rendering otherwise follow the host's local timezone, which would make
    interval-bucket boundaries (and returned offsets) machine-dependent — flaky across a
    developer box and UTC CI, and inconsistent with the UTC ``day(event_time)`` partitioning.
    Pinning the session to UTC keeps all reads reproducible.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def _select_page(
    arrow: pa.Table,
    *,
    where: str | None = None,
    params: list | None = None,
    limit: int,
    offset: int,
) -> list[dict]:
    """Deterministic ``ORDER BY event_time, icao24`` + LIMIT/OFFSET paging via DuckDB."""
    sql = "select * from sv"
    bind: list = list(params or [])
    if where:
        sql += f" where {where}"
    sql += " order by event_time, icao24 limit ? offset ?"
    bind += [limit, offset]
    with _connect_utc() as con:
        con.register("sv", arrow)
        return con.execute(sql, bind).to_arrow_table().to_pylist()


# ----------------------------------------------------------------------------- reads
def query_flights(
    table: Table,
    *,
    icao24: str | None = None,
    callsign: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    as_of_snapshot_id: int | None = None,
    as_of_timestamp: datetime | None = None,
) -> FlightsResult:
    """Filtered flight reads. icao24/callsign/time-window are pushed into the scan."""
    resolved = resolve_snapshot_id(
        table, as_of_snapshot_id=as_of_snapshot_id, as_of_timestamp=as_of_timestamp
    )
    row_filter = build_row_filter(icao24=icao24, callsign=callsign, start=start, end=end)
    arrow = _scan_arrow(table, resolved, row_filter, FLIGHT_FIELDS)
    items = _select_page(arrow, limit=limit, offset=offset)
    return FlightsResult(_effective_snapshot_id(table, resolved), len(items), items)


def query_bbox(
    table: Table,
    *,
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    as_of_snapshot_id: int | None = None,
    as_of_timestamp: datetime | None = None,
) -> FlightsResult:
    """Geographic bounding-box reads. Time window is pushed into the scan; the lat/lon box
    is applied in DuckDB (lat/lon are not partition columns, so pushdown buys no pruning —
    this is the SQL-shaped path)."""
    resolved = resolve_snapshot_id(
        table, as_of_snapshot_id=as_of_snapshot_id, as_of_timestamp=as_of_timestamp
    )
    row_filter = build_row_filter(start=start, end=end)
    arrow = _scan_arrow(table, resolved, row_filter, FLIGHT_FIELDS)
    items = _select_page(
        arrow,
        where="latitude between ? and ? and longitude between ? and ?",
        params=[min_lat, max_lat, min_lon, max_lon],
        limit=limit,
        offset=offset,
    )
    return FlightsResult(_effective_snapshot_id(table, resolved), len(items), items)


def query_stats_per_interval(
    table: Table,
    *,
    interval: str = "hour",
    start: datetime | None = None,
    end: datetime | None = None,
    group_by: str | None = None,
    as_of_snapshot_id: int | None = None,
    as_of_timestamp: datetime | None = None,
) -> StatsResult:
    """Flights-per-interval aggregation (``hour``|``day``), optionally grouped by
    ``origin_country``. Time window is pushed into the scan; the GROUP BY runs in DuckDB."""
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {INTERVALS}, got {interval!r}")
    if group_by is not None and group_by not in GROUP_BY_FIELDS:
        raise ValueError(f"group_by must be one of {GROUP_BY_FIELDS}, got {group_by!r}")

    resolved = resolve_snapshot_id(
        table, as_of_snapshot_id=as_of_snapshot_id, as_of_timestamp=as_of_timestamp
    )
    row_filter = build_row_filter(start=start, end=end)
    fields = ("event_time",) if group_by is None else ("event_time", group_by)
    arrow = _scan_arrow(table, resolved, row_filter, fields)

    # interval and group_by are validated against the allow-lists above, so this f-string
    # interpolation cannot inject arbitrary SQL.
    if group_by is None:
        sql = (
            f"select date_trunc('{interval}', event_time) as bucket_start, "
            "count(*) as cnt from sv group by 1 order by 1"
        )
    else:
        sql = (
            f"select date_trunc('{interval}', event_time) as bucket_start, "
            f"{group_by} as grp, count(*) as cnt from sv group by 1, 2 order by 1, 2"
        )
    with _connect_utc() as con:
        con.register("sv", arrow)
        rows = con.execute(sql).to_arrow_table().to_pylist()

    buckets = [
        {"bucket_start": r["bucket_start"], "group": r.get("grp"), "count": r["cnt"]}
        for r in rows
    ]
    return StatsResult(_effective_snapshot_id(table, resolved), buckets)


def _operation_str(operation) -> str | None:
    if operation is None:
        return None
    return getattr(operation, "value", None) or str(operation)


def list_snapshots(table: Table) -> list[SnapshotInfo]:
    """All snapshots from ``table.metadata.snapshots`` (oldest first)."""
    out: list[SnapshotInfo] = []
    for snap in table.metadata.snapshots:
        summary = snap.summary
        out.append(
            SnapshotInfo(
                snapshot_id=snap.snapshot_id,
                timestamp=datetime.fromtimestamp(snap.timestamp_ms / 1000, tz=UTC),
                operation=_operation_str(summary.operation) if summary else None,
                summary=dict(summary.additional_properties) if summary else {},
            )
        )
    return out


def table_meta(table: Table) -> TableMeta:
    """Table identifier, snapshot pointer/count, schema, and partition spec.

    Metadata-only — never triggers a data scan (so no row_count)."""
    schema = table.schema()
    spec = table.spec()
    name_by_id = {f.field_id: f.name for f in schema.fields}
    current = table.current_snapshot()
    return TableMeta(
        table_identifier=".".join(table.name()),
        current_snapshot_id=current.snapshot_id if current else None,
        snapshot_count=len(table.metadata.snapshots),
        schema=[
            {
                "field_id": f.field_id,
                "name": f.name,
                "type": str(f.field_type),
                "required": f.required,
            }
            for f in schema.fields
        ],
        partition_spec=[
            {
                "name": pf.name,
                "transform": str(pf.transform),
                "source_id": pf.source_id,
                "source_column": name_by_id.get(pf.source_id),
            }
            for pf in spec.fields
        ],
    )


def health(settings: Settings) -> HealthResult:
    """Liveness/readiness probe: is the catalog reachable and the bronze table loadable?

    Broad exception capture is intentional here — a probe reports failure, it does not raise.
    """
    catalog_reachable = False
    table_loadable = False
    current_snapshot_id: int | None = None
    error: str | None = None
    try:
        catalog = build_catalog(settings)
        catalog_reachable = True
        table = catalog.load_table(settings.table_identifier)
        table_loadable = True
        current = table.current_snapshot()
        current_snapshot_id = current.snapshot_id if current else None
    except Exception as exc:  # noqa: BLE001 — health probe must not raise
        error = f"{type(exc).__name__}: {exc}"
    status = "ok" if (catalog_reachable and table_loadable) else "unavailable"
    return HealthResult(status, catalog_reachable, table_loadable, current_snapshot_id, error)
