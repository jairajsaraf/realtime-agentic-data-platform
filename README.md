# Real-time agentic data platform — Stage 1: Iceberg lakehouse

A locally reproducible, batch-ingested [Apache Iceberg](https://iceberg.apache.org/)
lakehouse. Stage 1 demonstrates real Iceberg table-format capabilities — a
catalog, schema evolution, partition evolution, snapshot/time-travel, and
ingestion-time data-quality checks — not a generic CSV-to-Parquet batch job.

> Stage 1 of a larger platform. Streaming ingestion, agents, orchestration,
> dashboards, and cloud deployment are **out of scope** here (see
> [Out of scope](#out-of-scope-for-stage-1)).

## Engine & catalog decision

| Choice | What | Why |
| --- | --- | --- |
| **Engine** | pyiceberg (writer) + DuckDB (read/SQL) | Pure-Python, no JVM — reproducible from a fresh clone on Windows/Linux, light CI. Supports every required Stage-1 demo. |
| **Catalog** | pyiceberg `SqlCatalog` on SQLite | A *real* catalog (atomic compare-and-swap on a metadata pointer), zero extra runtime. Config-swap path to Postgres / a REST catalog later. |
| **Storage** | **LocalStack S3 (primary)**; `file://` is a dev/CI convenience only | Free local S3 emulation; real AWS is a config-only swap. `file://` runs the demos without Docker but is **not** the target architecture. |
| **DQ** | Pandera (warn/fail) | Lightweight, schema-first validation. |

A fuller engine trade-off table (vs PySpark + Iceberg) is in
[`docs/`](#) — summarized: PySpark is more feature-complete but needs a JVM +
winutils on Windows and heavy CI, which conflicts with the
fresh-clone-reproducibility and lightweight-CI constraints. The only pyiceberg
gap (data-file compaction) is out of Stage-1 scope.

## Dataset

OpenSky Network flight **state vectors**.

- **Real public-dataset path:** the live OpenSky REST API, wired as an on-demand
  source (`rtdp ingest --source opensky-live`). It is **opt-in and network-gated**,
  is **never run in CI**, and its output is **never committed** (OpenSky's license
  forbids redistribution). This is the genuine real-data path and preserves the
  Stage-4 live-state-vector direction.
- **Synthetic source:** a deterministic OpenSky-shaped generator used **only** for
  reproducible local/CI verification — it is not real data. It is seeded, spans
  multiple event days, and can inject bad rows to exercise the DQ severities.

Why not commit real OpenSky data: its license is non-commercial/research-only and
non-transferable, and it offers no free historical backfill — so the committed and
CI-tested path is synthetic, while real ingestion runs locally via the live source.

## Table design

`bronze.opensky_state_vectors` — one row per aircraft state observation.

- **Identity:** `icao24` (stable, required), `callsign`, `origin_country`.
- **Event vs ingestion time:** `event_time` (timestamptz observation time),
  `time_position`/`last_contact` (raw epoch), `ingest_time` (write time),
  `ingest_batch_id`, `source_name`.
- **Geospatial:** `latitude`, `longitude`, `baro_altitude`, `geo_altitude`.
- **Kinematics:** `velocity`, `true_track`, `vertical_rate`, `on_ground`,
  `squawk`, `spi`, `position_source`, `category`.

**Partition strategy:** start coarse with a hidden `day(event_time)` transform
(queries filter raw `event_time`; Iceberg prunes partitions). Day granularity
avoids tiny files for sparse backfill and supports future continuous appends
without a rewrite. The partition-evolution demo adds `hour(event_time)` for
future writes only. See `src/rtdp/schema.py`.

## Prerequisites

- **Python 3.12** and **[uv](https://docs.astral.sh/uv/)**.
- **Docker Desktop** — only for the default LocalStack S3 path. The `file://`
  fallback runs every demo without Docker.

## Quick start

```bash
# 1. Install dependencies (creates .venv from uv.lock)
uv sync

# 2a. LocalStack S3 (default): start the emulator
docker compose up -d

# 2b. Or skip Docker entirely — use a local file warehouse:
#     set RTDP_STORAGE_BACKEND=file   (PowerShell: $env:RTDP_STORAGE_BACKEND="file")

# 3. Verify configuration
uv run rtdp info
```

Copy `.env.example` to `.env` to customize the bucket, endpoint, warehouse, or
catalog path. Real AWS S3 is a config-only swap: `RTDP_STORAGE_BACKEND=aws` plus
real credentials/region.

## Running ingestion

One clean path: **source → transform → data-quality checks → append** to the
catalog-backed bronze table. Each successful batch is a new Iceberg snapshot; a
FAIL-level DQ result aborts the write (nothing committed) and exits non-zero.

```bash
# Synthetic (reproducible) into LocalStack S3 — the primary path (needs Docker):
docker compose up -d
uv run rtdp ingest --source synthetic --rows 50

# No Docker? Use the file:// convenience backend (NOT the target architecture):
#   PowerShell:  $env:RTDP_STORAGE_BACKEND="file"
#   bash:        export RTDP_STORAGE_BACKEND=file
uv run rtdp ingest --source synthetic --rows 50

# Real public data (opt-in, network-gated, never committed):
uv run rtdp ingest --source opensky-live

# Demonstrate DQ FAIL handling (injects bad rows; aborts, exits non-zero):
uv run rtdp ingest --inject-failures

# Inspect table state (snapshots, partition spec, sample rows):
uv run python scripts/inspect_table.py
```

Example output (synthetic):

```
Data quality: PASS (54 rows, 0 failures, 3 warnings)
WARN checks:
         column                                         check  failure_case index
           None duplicate (icao24, last_contact) rows present           0.0  None
       velocity                         in_range(0.0, 1500.0)        2200.0    50
position_source                            isin([0, 1, 2, 3])           9.0    51
Wrote 54 rows to bronze.opensky_state_vectors
  snapshot_id    : 4100877081503986423
  snapshot_count : 1
```

### Data-quality severities

| Severity | Behavior | Example checks |
|---|---|---|
| **FAIL** | Aborts the ingest; nothing written; non-zero exit | null `icao24`; `latitude` ∉ [-90, 90]; `longitude` ∉ [-180, 180]; null `event_time`/`last_contact` |
| **WARN** | Reported; ingest continues | `velocity` ∉ [0, 1500]; unknown `position_source`; duplicate `(icao24, last_contact)` |

### How writes work

| Aspect | Value |
|---|---|
| Catalog | `SqlCatalog` (type `sql`) on SQLite |
| Warehouse (LocalStack/AWS — primary) | `s3://<bucket>/<prefix>` (default `s3://lakehouse/warehouse`) |
| Warehouse (`file://` convenience) | `file://<abs>/_warehouse` |
| Table | `bronze.opensky_state_vectors`, partitioned `day(event_time)` |
| Append API | pyiceberg `table.append(pa.Table)`; the Arrow schema is derived from the Iceberg schema so required/optional and `timestamp(us, UTC)` always match |

## Iceberg capability demos

Runnable demonstrations of the four Iceberg capabilities. Each runs against the configured
backend (default LocalStack S3) and uses a **dedicated `demo.opensky_state_vectors` table**
that it resets first (purge + deterministic synthetic seed), so results are reproducible and
the real `bronze` table is never touched.

```bash
uv run rtdp demo catalog              # catalog discovery / table loading
uv run rtdp demo schema-evolution     # add a nullable column; compare old vs new snapshot
uv run rtdp demo partition-evolution  # day(event_time) -> hour(event_time), no data rewrite
uv run rtdp demo time-travel          # read by snapshot id; resolve a snapshot by timestamp
uv run rtdp demo all                  # run all four in sequence
uv run rtdp demo reset                # purge ONLY the demo table
```

- **catalog** — lists namespaces/tables and loads the table from the catalog (proves a real
  catalog, not files pretending to be tables).
- **schema-evolution** — `update_schema().add_column(...)` adds a nullable column; the pre-add
  snapshot has no values for it, the post-add batch does.
- **partition-evolution** — evolves the spec for *future* writes, replacing `day(event_time)`
  with `hour(event_time)` (pyiceberg disallows two partition fields on one source column, so this
  is a replace, not a stack). Pre-existing data files keep `spec_id=0` and are **not rewritten**;
  new appends use `spec_id=1`, proven via `table.inspect.data_files()` (file_path + spec_id).
- **time-travel** — `table.scan(snapshot_id=...)` reads an older snapshot; `snapshot_as_of_timestamp`
  resolves a snapshot by time. The explicit snapshot id is the primary, deterministic proof.

Covered by `tests/test_demos.py` (`file://`) and `tests/test_demos_localstack.py` (LocalStack S3).

## Tests

```bash
uv run pytest -m "not localstack"   # unit/integration on file:// (no Docker)
uv run pytest -m localstack         # S3 path (needs LocalStack running)
```

## Out of scope for Stage 1

Streaming ingestion, agents, orchestration, dashboards, Kafka/Flink, real AWS
deployment, and production IAM. The table/partition design anticipates streaming
appends to the same tables without a rewrite.
