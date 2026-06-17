"""Runnable Iceberg capability demos.

Each demo runs against a DEDICATED demo table (``demo.opensky_state_vectors``) so it
never touches the real bronze table, and resets that table first (purge + recreate +
deterministic synthetic seed) so snapshot counts / specs / row counts are reproducible.
Demos use the configured backend — default LocalStack S3 (the primary path).

Capabilities demonstrated: catalog discovery, schema evolution, partition evolution
(without rewriting existing data), and time-travel / snapshot queries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
from pyiceberg.io.pyarrow import schema_to_pyarrow
from pyiceberg.transforms import HourTransform
from pyiceberg.types import StringType

from .catalog import build_catalog, ensure_namespace
from .config import Settings
from .schema import BRONZE_SCHEMA, INITIAL_SPEC
from .sources.synthetic import SyntheticSource
from .transforms import raw_records_to_bronze

DEMO_NAMESPACE = "demo"
DEMO_TABLE = "opensky_state_vectors"
DEMO_IDENTIFIER = f"{DEMO_NAMESPACE}.{DEMO_TABLE}"  # never the bronze table
EVOLVED_COLUMN = "data_quality_grade"

_SEED_INGEST_TIME = datetime(2026, 6, 17, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers
def _seed_rows(
    n_rows: int, seed: int, *, days: int = 3, base_date: str = "2026-06-14"
) -> list[dict]:
    """Deterministic synthetic bronze rows (clean — no warn/fail injection)."""
    batch = SyntheticSource(
        n_rows=n_rows,
        seed=seed,
        days=days,
        base_date=base_date,
        inject_warnings=False,
        inject_failures=False,
    ).fetch()
    return raw_records_to_bronze(
        batch.records,
        ingest_time=_SEED_INGEST_TIME,
        ingest_batch_id=f"demo-seed-{seed}",
        source_name=batch.source_name,
    )


def reset_demo_table(settings: Settings):
    """Purge the demo table if present and recreate it empty (catalog, table)."""
    catalog = build_catalog(settings)
    ensure_namespace(catalog, DEMO_NAMESPACE)
    if catalog.table_exists(DEMO_IDENTIFIER):
        catalog.purge_table(DEMO_IDENTIFIER)
    table = catalog.create_table(DEMO_IDENTIFIER, schema=BRONZE_SCHEMA, partition_spec=INITIAL_SPEC)
    return catalog, table


def _append(catalog, table, rows: list[dict]):
    """Append rows using the table's CURRENT schema (works pre/post column-add)."""
    arrow = pa.Table.from_pylist(rows, schema=schema_to_pyarrow(table.schema()))
    table.append(arrow)
    return catalog.load_table(DEMO_IDENTIFIER)


def _file_specs(table) -> list[tuple[str, int]]:
    """(file_path, spec_id) for every data file — the partition-evolution proof."""
    df = table.inspect.data_files().to_pandas()
    return sorted((str(p), int(s)) for p, s in zip(df["file_path"], df["spec_id"], strict=False))


def _count_value(arrow: pa.Table, column: str, value) -> int:
    if column not in arrow.schema.names:
        return 0
    return pc.sum(pc.equal(arrow.column(column), value)).as_py() or 0


def reset_demo(settings: Settings) -> str:
    """Explicitly purge ONLY the dedicated demo table. Never touches bronze."""
    catalog = build_catalog(settings)
    if catalog.table_exists(DEMO_IDENTIFIER):
        catalog.purge_table(DEMO_IDENTIFIER)
    return DEMO_IDENTIFIER


# --------------------------------------------------------------------------- results
@dataclass
class CatalogDemoResult:
    namespaces: list[str]
    demo_tables: list[str]
    identifier: str
    location: str
    schema_columns: list[str]
    current_snapshot_id: int | None
    snapshot_count: int


@dataclass
class SchemaEvolutionResult:
    added_column: str
    before_columns: list[str]
    after_columns: list[str]
    s1_snapshot_id: int
    s2_snapshot_id: int
    s1_row_count: int
    s1_value_count: int
    current_value_count: int
    n_batch2: int


@dataclass
class PartitionEvolutionResult:
    before_spec: str
    after_spec: str
    spec_ids: list[int]
    files_before: list[tuple[str, int]]
    files_after_evolution: list[tuple[str, int]]
    files_final: list[tuple[str, int]]
    pre_files_unchanged: bool
    new_file_spec_ids: list[int]
    rows_a: int
    rows_b: int
    total_rows: int


@dataclass
class TimeTravelResult:
    s1_snapshot_id: int
    s2_snapshot_id: int
    n1: int
    n_total: int
    rows_at_s1: int
    rows_current: int
    as_of_snapshot_id: int | None
    history: list[tuple[int, int]] = field(default_factory=list)


# ----------------------------------------------------------------------------- demos
def demo_catalog(settings: Settings) -> CatalogDemoResult:
    catalog, table = reset_demo_table(settings)
    table = _append(catalog, table, _seed_rows(20, seed=1))

    namespaces = [".".join(ns) for ns in catalog.list_namespaces()]
    demo_tables = [".".join(t) for t in catalog.list_tables(DEMO_NAMESPACE)]
    table = catalog.load_table(DEMO_IDENTIFIER)
    current = table.current_snapshot()
    return CatalogDemoResult(
        namespaces=namespaces,
        demo_tables=demo_tables,
        identifier=DEMO_IDENTIFIER,
        location=table.location(),
        schema_columns=[f.name for f in table.schema().fields],
        current_snapshot_id=current.snapshot_id if current else None,
        snapshot_count=len(table.metadata.snapshots),
    )


def demo_schema_evolution(settings: Settings) -> SchemaEvolutionResult:
    catalog, table = reset_demo_table(settings)
    n1, n2 = 20, 10

    table = _append(catalog, table, _seed_rows(n1, seed=1))
    table = catalog.load_table(DEMO_IDENTIFIER)
    s1 = table.current_snapshot().snapshot_id
    before_columns = [f.name for f in table.schema().fields]

    with table.update_schema() as update:
        update.add_column(EVOLVED_COLUMN, StringType(), doc="post-hoc DQ grade")
    table = catalog.load_table(DEMO_IDENTIFIER)
    after_columns = [f.name for f in table.schema().fields]

    rows2 = _seed_rows(n2, seed=2)
    for row in rows2:
        row[EVOLVED_COLUMN] = "B"
    table = _append(catalog, table, rows2)
    table = catalog.load_table(DEMO_IDENTIFIER)
    s2 = table.current_snapshot().snapshot_id

    # Primary proof = before/after columns + counts. The old-snapshot read is shown for
    # completeness (the new column is absent/null there, so the "B" count is 0).
    s1_arrow = table.scan(snapshot_id=s1).to_arrow()
    current_arrow = table.scan().to_arrow()
    return SchemaEvolutionResult(
        added_column=EVOLVED_COLUMN,
        before_columns=before_columns,
        after_columns=after_columns,
        s1_snapshot_id=s1,
        s2_snapshot_id=s2,
        s1_row_count=s1_arrow.num_rows,
        s1_value_count=_count_value(s1_arrow, EVOLVED_COLUMN, "B"),
        current_value_count=_count_value(current_arrow, EVOLVED_COLUMN, "B"),
        n_batch2=n2,
    )


def demo_partition_evolution(settings: Settings) -> PartitionEvolutionResult:
    catalog, table = reset_demo_table(settings)
    rows_a, rows_b = 24, 12

    # Batch A under the initial spec (day(event_time), spec_id 0), spanning two days.
    table = _append(catalog, table, _seed_rows(rows_a, seed=1, days=2))
    table = catalog.load_table(DEMO_IDENTIFIER)
    before_spec = str(table.spec())
    files_before = _file_specs(table)

    # Evolve the spec (metadata-only) for FUTURE writes: replace day(event_time) with
    # hour(event_time). pyiceberg's writer rejects two partition fields on the same source
    # column ("redundant partitions"), so this is a replace (drop day, add hour), not a stack.
    with table.update_spec() as update:
        update.remove_field("event_day")
        update.add_field("event_time", HourTransform(), "event_hour")
    table = catalog.load_table(DEMO_IDENTIFIER)
    after_spec = str(table.spec())
    spec_ids = sorted(table.specs().keys())
    files_after_evolution = _file_specs(table)  # must equal files_before — no rewrite

    # Batch B under the new spec.
    table = _append(catalog, table, _seed_rows(rows_b, seed=2, days=1, base_date="2026-06-17"))
    table = catalog.load_table(DEMO_IDENTIFIER)
    files_final = _file_specs(table)

    before_paths = {p for p, _ in files_before}
    final_by_path = {p: s for p, s in files_final}
    pre_unchanged = (
        files_after_evolution == files_before  # untouched immediately after evolution
        and all(final_by_path.get(p) == 0 for p in before_paths)  # still spec 0 at the end
    )
    new_file_spec_ids = sorted({s for p, s in files_final if p not in before_paths})

    return PartitionEvolutionResult(
        before_spec=before_spec,
        after_spec=after_spec,
        spec_ids=spec_ids,
        files_before=files_before,
        files_after_evolution=files_after_evolution,
        files_final=files_final,
        pre_files_unchanged=pre_unchanged,
        new_file_spec_ids=new_file_spec_ids,
        rows_a=rows_a,
        rows_b=rows_b,
        total_rows=table.scan().to_arrow().num_rows,
    )


def demo_time_travel(settings: Settings) -> TimeTravelResult:
    catalog, table = reset_demo_table(settings)
    n1, n2 = 20, 15

    table = _append(catalog, table, _seed_rows(n1, seed=1))
    table = catalog.load_table(DEMO_IDENTIFIER)
    s1 = table.current_snapshot()
    s1_id, s1_ts = s1.snapshot_id, s1.timestamp_ms

    time.sleep(0.05)  # ensure S2 commits at a strictly later ms (robust timestamp lookup)
    table = _append(catalog, table, _seed_rows(n2, seed=2))
    table = catalog.load_table(DEMO_IDENTIFIER)
    s2_id = table.current_snapshot().snapshot_id

    # Primary proof: explicit snapshot-id reads (deterministic).
    con = duckdb.connect()
    con.register("at_s1", table.scan(snapshot_id=s1_id).to_arrow())
    con.register("current", table.scan().to_arrow())
    rows_at_s1 = con.execute("select count(*) from at_s1").fetchone()[0]
    rows_current = con.execute("select count(*) from current").fetchone()[0]

    # Secondary: resolve a snapshot by timestamp (informational).
    as_of = table.snapshot_as_of_timestamp(s1_ts)
    return TimeTravelResult(
        s1_snapshot_id=s1_id,
        s2_snapshot_id=s2_id,
        n1=n1,
        n_total=n1 + n2,
        rows_at_s1=int(rows_at_s1),
        rows_current=int(rows_current),
        as_of_snapshot_id=as_of.snapshot_id if as_of else None,
        history=[(e.snapshot_id, e.timestamp_ms) for e in table.history()],
    )


# ----------------------------------------------------------------------------- render
def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def format_catalog(r: CatalogDemoResult) -> str:
    return "\n".join(
        [
            "=== DEMO: catalog discovery / table loading ===",
            f"namespaces        : {r.namespaces}",
            f"tables in 'demo'  : {r.demo_tables}",
            f"loaded table      : {r.identifier}",
            f"location          : {r.location}",
            f"schema columns    : {len(r.schema_columns)}",
            f"current snapshot  : {r.current_snapshot_id}",
            f"snapshot count    : {r.snapshot_count}",
        ]
    )


def format_schema_evolution(r: SchemaEvolutionResult) -> str:
    return "\n".join(
        [
            "=== DEMO: schema evolution (add nullable column) ===",
            f"added column            : {r.added_column} (nullable)",
            f"before ({len(r.before_columns)} cols)      : {r.before_columns}",
            f"after  ({len(r.after_columns)} cols)      : {r.after_columns}",
            f"snapshot S1 (pre-add)   : {r.s1_snapshot_id}  ({r.s1_row_count} rows)",
            f"snapshot S2 (post-add)  : {r.s2_snapshot_id}",
            f"'{r.added_column}'=B @ S1   : {r.s1_value_count}  (column added after S1 -> 0)",
            f"'{r.added_column}'=B now    : {r.current_value_count}  (= batch2 rows: {r.n_batch2})",
        ]
    )


def format_partition_evolution(r: PartitionEvolutionResult) -> str:
    lines = [
        "=== DEMO: partition evolution (no rewrite) ===",
        f"before spec : {' '.join(r.before_spec.split())}",
        f"after  spec : {' '.join(r.after_spec.split())}",
        f"all spec ids: {r.spec_ids}",
        f"data files after batch A ({len(r.files_before)}):",
    ]
    lines += [f"  spec {s}  {_basename(p)}" for p, s in r.files_before]
    lines.append(f"data files right after spec update ({len(r.files_after_evolution)}):")
    lines += [f"  spec {s}  {_basename(p)}" for p, s in r.files_after_evolution]
    lines.append(f"pre-existing files unchanged (no rewrite): {r.pre_files_unchanged}")
    lines.append(f"data files after batch B ({len(r.files_final)}):")
    lines += [f"  spec {s}  {_basename(p)}" for p, s in r.files_final]
    lines.append(f"new-file spec ids after batch B : {r.new_file_spec_ids}")
    lines.append(f"total rows (A={r.rows_a} + B={r.rows_b}) : {r.total_rows}")
    return "\n".join(lines)


def format_time_travel(r: TimeTravelResult) -> str:
    lines = ["=== DEMO: time-travel / snapshot queries ===", "history (snapshot_id, timestamp_ms):"]
    lines += [f"  {sid}  {ts}" for sid, ts in r.history]
    lines += [
        f"S1 snapshot : {r.s1_snapshot_id}",
        f"S2 snapshot : {r.s2_snapshot_id}",
        f"rows @ S1 by snapshot_id (DuckDB) : {r.rows_at_s1}  (expected {r.n1})",
        f"rows now  by snapshot_id (DuckDB) : {r.rows_current}  (expected {r.n_total})",
        f"snapshot_as_of_timestamp(S1.ts)   : {r.as_of_snapshot_id}  (resolves to S1)",
    ]
    return "\n".join(lines)
