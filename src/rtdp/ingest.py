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
    table.append(bronze_rows_to_arrow(rows))

    snapshot_id, count = _snapshot_state(catalog, identifier)
    return IngestResult(identifier, len(rows), len(rows), report, snapshot_id, count)
