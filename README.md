# Real-time agentic data platform

A locally reproducible data platform built **stage by stage**. Today it pairs an
[Apache Iceberg](https://iceberg.apache.org/) lakehouse (**Stage 1**) with a
read-only FastAPI serving layer over it (**Stage 2A**).

> "Real-time" and "agentic" name the platform's **direction**, not its current state.
> What exists today is **batch** ingestion into a real Iceberg table plus a **read-only**
> typed HTTP API. Streaming ingestion and agent integration are later stages and are
> **not built yet** (see [Out of scope & roadmap](#out-of-scope--roadmap)).

## Project status

| Stage | Status | What it adds |
| --- | --- | --- |
| **Stage 1 — Iceberg lakehouse** | ✅ Complete | A real catalog-backed Iceberg table with schema evolution, partition evolution, snapshot/time-travel, and ingestion-time data-quality checks — not a generic CSV-to-Parquet job. |
| **Stage 2A — read-only serving layer** | ✅ Complete | A FastAPI service over the bronze table: typed flight reads, geographic bounding-box, interval aggregations, and Iceberg time-travel, behind an auto-generated OpenAPI schema. |
| **Later stages** | ⬜ Planned | Streaming ingestion, agent integration (Stage D), orchestration, dashboards, cloud deployment. |

## Architecture

```
INGESTION  (Stage 1 — batch, via the rtdp CLI)         SERVING  (Stage 2A — read-only HTTP)

  source ── synthetic  (default, deterministic, CI)
  (rtdp.    live OpenSky (opt-in, network-gated, never committed)
   sources)
     │
     ▼
  transform               (rtdp.transforms)
     │
     ▼
  data quality            (Pandera — WARN continues, FAIL aborts the write)
     │
     ▼
  Iceberg table           bronze.opensky_state_vectors          ┐
  catalog: SqlCatalog     storage: LocalStack S3 (primary)      │  read
  on SQLite                        file:// (no-Docker fallback)  │
     │                                                           ▼
     └───────────────────────────────►  query layer  ─────►  FastAPI  ─────►  client
                                         (rtdp.query)         (rtdp.api)       / future
                                         pyiceberg pushdown   typed JSON,      Stage D
                                         + DuckDB SQL          OpenAPI /docs    agent
```

**Dependency direction (one-way):** `api → query → (catalog, config, schema)`. All read
logic lives in `rtdp.query` (transport-agnostic); the FastAPI layer (`rtdp.api`) is a thin
transport over it, so the CLI and a future agent reuse the same functions. Ingestion never
depends on the API, and the API never mutates the table.

## Prerequisites

- **Python 3.12** and **[uv](https://docs.astral.sh/uv/)**.
- **Docker Desktop** — only for the default LocalStack S3 path. The `file://` fallback runs
  everything (ingestion, demos, API, tests) without Docker.

Java/JVM is **not** required — pyiceberg is pure-Python.

## Quick start

End-to-end, copy-pasteable:

```bash
# 1. Install dependencies (creates .venv from uv.lock)
uv sync

# 2. Choose an object-store backend:
#    a) LocalStack S3 (primary) — needs Docker:
docker compose up -d
#    b) ...or no Docker — use a local file warehouse instead:
#       bash:        export RTDP_STORAGE_BACKEND=file
#       PowerShell:  $env:RTDP_STORAGE_BACKEND="file"

# 3. Verify the resolved configuration
uv run rtdp info

# 4. Ingest deterministic synthetic data (each batch is a new Iceberg snapshot)
uv run rtdp ingest --source synthetic --rows 50

# 5. Start the read-only API, then open the interactive docs
uv run rtdp serve            # http://127.0.0.1:8000  (OpenAPI UI at /docs)

# 6. Run the tests (from a second shell)
uv run pytest -m "not localstack"   # unit/integration on file:// (no Docker)
uv run pytest -m localstack         # S3 path (LocalStack must be running)
```

Copy `.env.example` to `.env` to customize the bucket, endpoint, warehouse, or catalog path.
Real AWS S3 is a config-only swap: `RTDP_STORAGE_BACKEND=aws` plus real credentials/region.

---

## Stage 1 — Iceberg lakehouse

### Engine & catalog decision

| Choice | What | Why |
| --- | --- | --- |
| **Engine** | pyiceberg (writer) + DuckDB (read/SQL) | Pure-Python, no JVM — reproducible from a fresh clone on Windows/Linux, light CI. Supports every required Stage-1 demo. |
| **Catalog** | pyiceberg `SqlCatalog` on SQLite | A *real* catalog (atomic compare-and-swap on a metadata pointer), zero extra runtime. Config-swap path to Postgres / a REST catalog later. |
| **Storage** | **LocalStack S3 (primary)**; `file://` is a dev/CI convenience only | Free local S3 emulation; real AWS is a config-only swap. `file://` runs the demos without Docker but is **not** the target architecture. |
| **DQ** | Pandera (warn/fail) | Lightweight, schema-first validation. |

The main alternative considered was **PySpark + Iceberg**: more feature-complete, but it needs
a JVM + winutils on Windows and heavy CI, which conflicts with the fresh-clone-reproducibility
and lightweight-CI constraints. The only pyiceberg gap (data-file compaction) is out of
Stage-1 scope.

### Dataset

OpenSky Network flight **state vectors**.

- **Real public-dataset path:** the live OpenSky REST API, wired as an on-demand source
  (`rtdp ingest --source opensky-live`). It is **opt-in and network-gated**, is **never run in
  CI**, and its output is **never committed** (OpenSky's license forbids redistribution). This
  is the genuine real-data path and preserves the later live-state-vector direction.
- **Synthetic source (default):** a deterministic OpenSky-shaped generator used **only** for
  reproducible local/CI verification — it is not real data. It is seeded, spans multiple event
  days, and can inject bad rows to exercise the DQ severities.

Why not commit real OpenSky data: its license is non-commercial/research-only and
non-transferable, and it offers no free historical backfill — so the committed and CI-tested
path is synthetic, while real ingestion runs locally via the live source.

### Table design

`bronze.opensky_state_vectors` — one row per aircraft state observation.

- **Identity:** `icao24` (stable, required), `callsign`, `origin_country`.
- **Event vs ingestion time:** `event_time` (timestamptz observation time),
  `time_position`/`last_contact` (raw epoch), `ingest_time` (write time), `ingest_batch_id`,
  `source_name`.
- **Geospatial:** `latitude`, `longitude`, `baro_altitude`, `geo_altitude`.
- **Kinematics:** `velocity`, `true_track`, `vertical_rate`, `on_ground`, `squawk`, `spi`,
  `position_source`, `category`.

**Partition strategy:** start coarse with a hidden `day(event_time)` transform (queries filter
raw `event_time`; Iceberg prunes partitions). Day granularity avoids tiny files for sparse
backfill and supports future continuous appends without a rewrite. The partition-evolution demo
adds `hour(event_time)` for future writes only. See `src/rtdp/schema.py`.

### Ingestion

One clean path: **source → transform → data-quality checks → append** to the catalog-backed
bronze table. Each successful batch is a new Iceberg snapshot; a FAIL-level DQ result aborts the
write (nothing committed) and exits non-zero.

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

#### Data-quality severities

| Severity | Behavior | Example checks |
|---|---|---|
| **FAIL** | Aborts the ingest; nothing written; non-zero exit | null `icao24`; `latitude` ∉ [-90, 90]; `longitude` ∉ [-180, 180]; null `event_time`/`last_contact` |
| **WARN** | Reported; ingest continues | `velocity` ∉ [0, 1500]; unknown `position_source`; duplicate `(icao24, last_contact)` |

#### How writes work

| Aspect | Value |
|---|---|
| Catalog | `SqlCatalog` (type `sql`) on SQLite |
| Warehouse (LocalStack/AWS — primary) | `s3://<bucket>/<prefix>` (default `s3://lakehouse/warehouse`) |
| Warehouse (`file://` convenience) | `file://<abs>/_warehouse` |
| Table | `bronze.opensky_state_vectors`, partitioned `day(event_time)` |
| Append API | pyiceberg `table.append(pa.Table)`; the Arrow schema is derived from the Iceberg schema so required/optional and `timestamp(us, UTC)` always match |

### Iceberg capability demos

Runnable demonstrations of the four Iceberg capabilities. Each runs against the configured
backend (default LocalStack S3) and uses a **dedicated `demo.opensky_state_vectors` table** that
it resets first (purge + deterministic synthetic seed), so results are reproducible and the real
`bronze` table is never touched.

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

---

## Stage 2A — read-only serving layer

A FastAPI service exposes typed, queryable reads over the bronze table — flight filters,
geographic bounding-box, interval aggregations, and Iceberg snapshot/time-travel — behind an
auto-generated OpenAPI schema. It is **read-only**: ingestion stays the Stage 1 CLI path, and no
endpoint mutates the table. All read logic lives in `rtdp.query` (transport-agnostic); the API
(`rtdp.api`) is a thin layer over it, so the CLI and a future Stage D agent can reuse the same
functions.

**Why predicate pushdown?** `icao24`/`callsign`/time-window filters are translated into a
pyiceberg `row_filter` and pushed into the scan, so the `day(event_time)` partition pruning fires
at the table-format level instead of materializing the whole table and filtering in DuckDB.
DuckDB is reserved for the SQL-shaped work (bounding-box, `GROUP BY` aggregations, ordered
`LIMIT`/`OFFSET` paging). See the note in `src/rtdp/query.py`.

### Run it

```bash
uv sync                                            # installs fastapi + uvicorn (in uv.lock)
docker compose up -d                               # LocalStack S3 (primary)
#   ...or skip Docker:  export RTDP_STORAGE_BACKEND=file  (PowerShell: $env:...="file")
uv run rtdp ingest --source synthetic --rows 50    # seed some data first (API is read-only)
uv run rtdp serve                                  # http://127.0.0.1:8000
```

Override the bind address with `RTDP_API_HOST` / `RTDP_API_PORT`. Interactive OpenAPI docs render
at **http://127.0.0.1:8000/docs**.

### Endpoints

| Method & path | Purpose |
|---|---|
| `GET /health` | catalog/table reachability (200 healthy, 503 unavailable) |
| `GET /flights` | filter by `icao24`, `callsign`, `start`, `end`; `limit`/`offset` |
| `GET /flights/bbox` | bounding-box (`min_lat,max_lat,min_lon,max_lon`) + optional time window |
| `GET /stats/flights-per-interval` | counts per `hour`\|`day`, optional `group_by=origin_country` |
| `GET /snapshots` | list Iceberg snapshots from table metadata |
| `GET /meta` | table identifier, snapshot pointer/count, schema, partition spec |

Every read/aggregation endpoint accepts mutually-exclusive time-travel selectors
`as_of_snapshot_id` and `as_of_timestamp` (supplying both → HTTP 400; a timestamp before the
first snapshot → 404) and echoes the resolved `snapshot_id`.

In `/flights` and `/flights/bbox`, **`count` is the number of items _returned_ (after
`limit`/`offset`), not the total number of matching rows** — paging avoids a second full-scan
count.

### curl examples

```bash
curl 'http://127.0.0.1:8000/health'
curl 'http://127.0.0.1:8000/flights?callsign=DLH123&limit=5'
curl 'http://127.0.0.1:8000/flights?start=2026-06-14T00:00:00Z&end=2026-06-15T00:00:00Z&limit=5'
curl 'http://127.0.0.1:8000/flights/bbox?min_lat=45&max_lat=55&min_lon=5&max_lon=15&limit=10'
curl 'http://127.0.0.1:8000/stats/flights-per-interval?interval=day&group_by=origin_country'
curl 'http://127.0.0.1:8000/snapshots'
curl 'http://127.0.0.1:8000/meta'
# time-travel: read an older snapshot (id from /snapshots)
curl 'http://127.0.0.1:8000/flights?as_of_snapshot_id=<snapshot-id>&limit=5'
```

**Limitations (local-first):** no auth, rate-limiting, or caching yet, and no write path through
the API. Stage D (agent integration over this API) is intentionally deferred.

---

## Testing

```bash
uv run pytest -m "not localstack"   # unit/integration on file:// (no Docker)
uv run pytest -m localstack         # S3 path (needs LocalStack running)
```

CI runs both jobs on every push (`.github/workflows/ci.yml`): lint + the `file://` suite, and an
integration job against LocalStack S3. See `RUNBOOK.md` for a step-by-step fresh-clone walkthrough.

## Out of scope & roadmap

**Not built yet (intentionally):** streaming ingestion, agents / LLM tool execution, orchestration,
dashboards, Kafka/Flink, real cloud deployment, and production IAM. The table/partition design
anticipates streaming appends to the same tables without a rewrite, and the read API is structured
so a future agent can call it.

**Direction for later stages (not commitments):**

- **Streaming ingestion** — continuous appends alongside the existing batch path.
- **Stage D — agent integration** — an agent that calls this read-only API over its typed OpenAPI
  surface.
- **Orchestration & dashboards**, then **cloud deployment** (real AWS S3 is already a config-only
  swap).
