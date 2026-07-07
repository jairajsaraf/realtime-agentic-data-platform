"""Ingestion path: source -> transform -> DQ -> append to the bronze Iceberg table.

One clean path. The table (schema + initial partition spec) is created on first run;
each successful batch is appended as a new snapshot. A FAIL-level DQ result aborts the
write entirely — nothing is committed and the table is left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pandas as pd

from .catalog import build_catalog, ensure_namespace
from .config import Settings
from .dq import DQReport, run_dq
from .schema import BRONZE_SCHEMA, INITIAL_SPEC
from .sources.base import Source
from .telemetry import span
from .transforms import bronze_rows_to_arrow, raw_records_to_bronze


@dataclass
class IngestResult:
    table_identifier: str
    rows_in: int
    rows_written: int
    dq: DQReport
    snapshot_id: int | None
    snapshot_count: int


def _snapshot_state(catalog, identifier: str) -> tuple[int | None, int]:
    if not catalog.table_exists(identifier):
        return None, 0
    table = catalog.load_table(identifier)
    current = table.current_snapshot()
    return (current.snapshot_id if current else None), len(table.metadata.snapshots)


_NUMERIC_TYPES = (int, float)


def _ingest_lag_seconds(ingest_time: datetime, rows: list[dict]) -> float | None:
    """Data staleness at write time: ingest wall-clock minus the newest ``last_contact`` in the
    batch, in seconds. Returns ``None`` (attribute omitted) for an empty batch, or when ANY row is
    missing ``last_contact`` or carries a non-numeric/bool value — the lag is computed only when
    every row has a valid numeric ``last_contact`` (never from a partial batch). Never raises."""
    try:
        if not rows:
            return None
        contacts = []
        for row in rows:
            value = row.get("last_contact")
            if not isinstance(value, _NUMERIC_TYPES) or isinstance(value, bool):
                return None
            contacts.append(value)
        return ingest_time.timestamp() - max(contacts)
    except Exception:
        return None


def run_ingest(
    settings: Settings, source: Source, *, ingest_time: datetime | None = None
) -> IngestResult:
    ingest_time = ingest_time or datetime.now(UTC)
    batch_id = str(uuid4())

    batch = source.fetch()
    rows = raw_records_to_bronze(
        batch.records,
        ingest_time=ingest_time,
        ingest_batch_id=batch_id,
        source_name=batch.source_name,
    )
    report = run_dq(pd.DataFrame(rows))

    catalog = build_catalog(settings)
    identifier = settings.table_identifier

    if not report.ok:
        snapshot_id, count = _snapshot_state(catalog, identifier)
        return IngestResult(identifier, len(rows), 0, report, snapshot_id, count)

    ensure_namespace(catalog, settings.namespace)
    table = catalog.create_table_if_not_exists(
        identifier, schema=BRONZE_SCHEMA, partition_spec=INITIAL_SPEC
    )
    lag_seconds = _ingest_lag_seconds(ingest_time, rows)
    arrow_batch = bronze_rows_to_arrow(rows)  # convert outside the span; the span times only append
    with span("rtdp.ingest.batch", rows_in=len(rows), rows_written=len(rows)) as ingest_span:
        ingest_span.set_attribute("ingest.lag_seconds", lag_seconds)  # None omitted by guard
        table.append(arrow_batch)

    snapshot_id, count = _snapshot_state(catalog, identifier)
    return IngestResult(identifier, len(rows), len(rows), report, snapshot_id, count)
