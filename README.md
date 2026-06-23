# Real-time agentic data platform

[![codecov](https://codecov.io/github/jairajsaraf/realtime-agentic-data-platform/graph/badge.svg?token=PX1S7LTMYW)](https://codecov.io/github/jairajsaraf/realtime-agentic-data-platform)

A locally reproducible data platform built **stage by stage**. Today it pairs an
[Apache Iceberg](https://iceberg.apache.org/) lakehouse (**Stage 1**) with a read-only FastAPI
serving layer (**Stage 2A**), scheduled micro-batch ingestion (**Stage 2B**), and a
natural-language **agent** that answers questions and diagnoses data quality by calling that read
API as its tools (**Stage D**).

> "Real-time" and "agentic" name the platform's **direction** — the words now have honest, narrow
> backing, not their maximal meaning. "Real-time" is **scheduled micro-batch** ingestion into a
> real Iceberg table (near-real-time, **not** true Kafka/Flink streaming). "Agentic" is a
> **read-only, human-in-the-loop** agent over the typed HTTP API — it proposes remediations but
> never mutates data, and there is no autonomous action, RAG, or fine-tuning. True streaming,
> RAG/vector search, and dashboards remain **not built yet** (see
> [Out of scope & roadmap](#out-of-scope--roadmap)).

## Project status

| Stage | Status | What it adds |
| --- | --- | --- |
| **Stage 1 — Iceberg lakehouse** | ✅ Complete | A real catalog-backed Iceberg table with schema evolution, partition evolution, snapshot/time-travel, and ingestion-time data-quality checks — not a generic CSV-to-Parquet job. |
| **Stage 2A — read-only serving layer** | ✅ Complete | A FastAPI service over the bronze table: typed flight reads, geographic bounding-box, interval aggregations, and Iceberg time-travel, behind an auto-generated OpenAPI schema. |
| **Stage 2B — incremental micro-batch ingestion** | ✅ Complete | A scheduled `rtdp stream` loop that polls a source and appends micro-batches via the existing writer (within-batch dedup, skip-empty, read-time latest-state view, opt-in snapshot expiration). Near-real-time micro-batch — **not** true streaming. |
| **Stage D — agentic layer** | ✅ Complete | A natural-language agent (`rtdp agent`) that answers flight questions and diagnoses data quality by calling the Stage 2A HTTP API as tools. Strictly read-only and human-in-the-loop — proposes remediations, never mutates. Deterministic fake-LLM tests; opt-in live eval harness. |
| **Stage E — productionization & observability** | 🚧 In progress | One Docker image (multi-entrypoint: serve/stream/maintain/agent), a single-host Docker Compose topology (file:// or self-hosted MinIO via the `aws` backend), an optional telemetry boundary (`[otel]` extra, no-op by default), and a CI/CD pipeline that smoke-tests the image, publishes to GHCR on `main`, and gates a **no-op** `deploy`. **Ops-only and additive** — no new data features or endpoints; deployment is a protected-environment no-op (no provisioning has been done). |
| **Later stages** | ⬜ Planned | True streaming (Kafka/Flink), RAG/vector search, fine-tuning, orchestration, dashboards, real-AWS / multi-host (Kubernetes) deployment. |

## Architecture

```
INGESTION (Stage 1 batch / Stage 2B micro-batch CLI)   SERVING (Stage 2A HTTP)   AGENT (Stage D)

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
     └───────────────────────────────►  query layer  ─────►  FastAPI  ─────►  client / CLI
                                         (rtdp.query)         (rtdp.api)          ▲
                                         pyiceberg pushdown   typed JSON,         │ HTTP tool calls
                                         + DuckDB SQL         OpenAPI /docs        │
                                                                         Stage D agent (rtdp.agent)
                                                                         NL question → tools → answer
```

**Dependency direction (one-way):** `agent → API → query → (catalog, config, schema)`. All read
logic lives in `rtdp.query` (transport-agnostic); the FastAPI layer (`rtdp.api`) is a thin
transport over it. The **Stage D agent (`rtdp.agent`) is just another HTTP client of the API** —
it never imports `rtdp.query` or touches the catalog/DuckDB directly. Ingestion never depends on
the API, and neither the API nor the agent mutates the table.

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
the API. The Stage D agent consumes this API as its tool surface (see below).

---

## Stage 2B — incremental micro-batch ingestion

`rtdp stream` turns the one-shot batch ingest into a **scheduled micro-batch loop**: it polls a
source on a timer and appends one micro-batch per interval, **reusing the unchanged Stage 1
`run_ingest` writer**. This is **near-real-time micro-batch ingestion, not true streaming** —
Kafka/Flink-style streaming remains out of scope.

Each tick fetches one batch, **de-duplicates within the batch** on the logical key
`(icao24, last_contact)`, **skips empty polls** (so an empty OpenSky response never mints an empty
snapshot), and appends via `run_ingest`. The loop backs off on transient source errors (e.g.
OpenSky rate limits) and stops cleanly on Ctrl+C. Bronze stays an **append-only event log**;
"latest state per aircraft" is a **read-time** concern, exposed as `rtdp.query.query_latest_state`
(a DuckDB `ROW_NUMBER()` window over bronze — a thin silver-style read model, no row updates, no
MERGE).

### Run it

```bash
docker compose up -d                                  # LocalStack S3 (or export RTDP_STORAGE_BACKEND=file)
uv run rtdp stream --source synthetic --interval 5 --max-batches 10
#   live data (opt-in, network-gated, NEVER in CI — see the OpenSky note above):
#   uv run rtdp stream --source opensky-live --interval 60
```

`--max-batches 0` (the default) runs until interrupted. Tune the cadence with `--interval` or
`RTDP_STREAM_INTERVAL_SECONDS`. The synthetic path uses a deterministic *continuous* generator
that advances event-time across batches, so CI exercises the loop with no Docker/network.

### Snapshot maintenance (metadata-only)

Micro-batch appends accumulate snapshots (and small data files). Bound snapshot/metadata growth
with an explicit, opt-in maintenance command:

```bash
uv run rtdp maintain expire-snapshots --retain 10     # keep the newest 10 snapshots
```

This expires old snapshots from the **table metadata only** — it does **not** delete data files
and is **not compaction**. Data-file compaction/rewrite is a true-engine (e.g. Spark) capability,
intentionally **not faked** here; it ties back to the Stage 1 pyiceberg/no-JVM trade-off
(small-file accumulation is a known limitation of this pure-Python build). The current snapshot is
never expired.

---

## Stage D — agentic layer over the read API

`rtdp agent` is a natural-language agent that answers questions about the flight data and
diagnoses data quality **by calling the Stage 2A HTTP API as its tools**. It is strictly an API
client: it never imports `rtdp.query` or reaches into the catalog/DuckDB, and it is **read-only
and human-in-the-loop** — it can *propose* remediations but never mutates tables, data, schemas,
or files, and adds no write surface (`agent → API → query → catalog`).

- **Tools from OpenAPI.** Tool definitions are derived from the live `/openapi.json` (a curated
  read-only allowlist of the GET endpoints), with a static fallback. There is deliberately **no
  write tool**, so the agent is structurally incapable of changing anything.
- **Grounded answers.** Every answer cites which endpoint and which Iceberg snapshot id produced
  it; provenance is taken from the tool results themselves, not from the model's prose.
- **DQ diagnosis (read-derived).** Pandera WARN/FAIL history is not persisted and there is no DQ
  endpoint, so the agent **re-derives** anomalies (over-speed, unknown `position_source`,
  out-of-range coordinates/altitude, null required fields, duplicate state keys) from the rows the
  API returns and proposes fixes for a human to apply. This is a **bounded sample** — one queried
  window, capped by the API row limit, over the snapshots queried — and the agent says so.
- **Config-driven LLM.** The model sits behind a thin OpenAI-compatible client (built on the
  existing `httpx`; no model SDK). Endpoint, key, and model name come from `RTDP_AGENT_*` settings,
  so an open dev model (e.g. an NVIDIA NIM endpoint) and a frontier model are a config swap — no
  keys are committed.

### Run it

```bash
# 1. Seed data and start the read-only API (the agent talks to it over HTTP):
uv run rtdp ingest --source synthetic --rows 80
uv run rtdp serve                                   # http://127.0.0.1:8000

# 2. Configure an OpenAI-compatible model endpoint (example: NVIDIA NIM):
#   PowerShell:  $env:RTDP_AGENT_BASE_URL="https://integrate.api.nvidia.com/v1"
#                $env:RTDP_AGENT_MODEL="meta/llama-3.1-8b-instruct"
#                $env:RTDP_AGENT_API_KEY="<your key>"

# 3. Ask (one-shot), or run interactively:
uv run rtdp agent "How many records are there, and which snapshot answered?"
uv run rtdp agent --interactive
```

Swap to a frontier provider by pointing `RTDP_AGENT_BASE_URL` / `RTDP_AGENT_MODEL` at any other
OpenAI-compatible endpoint — no code change. **Testing split:** unit tests drive the agent with a
deterministic **fake LLM** (no network, no keys; runs in CI), while a separate **opt-in** live
eval harness (`scripts/eval_agent.py`, network-gated, never in CI) reports grounding/faithfulness,
tool-call correctness, latency, and token usage against a real model. See `RUNBOOK.md`.

---

## Stage E — productionization & observability

Stage E packages the existing surfaces for an always-on, monitored **single-host** demo. It is
**ops-only and additive**: no data-plane, schema, writer, DQ, or query change, and **no new API
endpoint** (in particular **no `/metrics`**, `/agent`, `/flights/latest`, or `/dq/*`). The deployed
demo serves **synthetic data only** — never live OpenSky.

### One image, multiple entrypoints

A single Docker image (`Dockerfile`) wraps the `rtdp` CLI; the container command selects the runtime:

| Command | Role |
|---|---|
| `rtdp serve` | read-only API (the only externally exposed service) |
| `rtdp stream` | the sole writer — one micro-batch snapshot per interval (synthetic source) |
| `rtdp maintain expire-snapshots` | one-shot, metadata-only snapshot expiration |
| `rtdp agent "<q>"` | on-demand agent question (external, config-driven LLM) |

```bash
docker build -t rtdp:local .
bash scripts/docker_smoke.sh           # build → seed (file://) → serve → GET /health == 200
```

### Single-host topology (Docker Compose)

`deploy/docker-compose.yml` runs `api` + `stream` (and a one-shot `maintain` profile) from the one
image, sharing a single volume. The catalog stays **local SQLite on that shared volume in every
backend**, so the host is **single-writer** — run one `stream` replica and schedule `maintain` not
to overlap it.

```bash
docker compose -f deploy/docker-compose.yml up -d --build            # file:// (no secrets)
docker compose -f deploy/docker-compose.yml --profile s3 up -d       # self-hosted MinIO
```

Object storage is config-driven: set `RTDP_STORAGE_BACKEND=aws` and point `RTDP_S3_ENDPOINT_URL`
at the MinIO service to use an S3-compatible store (real AWS stays the default when no endpoint is
set — see `deploy/README.md`). The MinIO console binds to `127.0.0.1` only.

### Telemetry boundary (no-op by default)

`rtdp.telemetry` is the single observability seam: **stdlib structured logging** always, and
**OpenTelemetry tracing only when `RTDP_OTEL_ENABLED=true` AND the optional `[otel]` extra is
installed**. The default install pulls in **no** OpenTelemetry packages, so CI and the default
runtime stay dependency-free; if the extra is missing while enabled, the boundary logs a warning
and degrades to no-op (the app always boots). Nothing is exported until spans are produced.

```bash
uv sync --extra otel       # opt in to the OpenTelemetry SDK + OTLP exporter
```

Settings (all under the existing `RTDP_*` / `Settings` surface): `RTDP_OTEL_ENABLED`,
`RTDP_OTEL_SERVICE_NAME`, `RTDP_OTEL_EXPORTER_OTLP_ENDPOINT` (provider-agnostic — point it at a
collector or the Datadog Agent), `RTDP_LOG_FORMAT` (`text`|`json`), `RTDP_LOG_LEVEL`. There is **no
`/metrics` scrape endpoint**.

### CI/CD

`.github/workflows/ci.yml` keeps image validation, publishing, and deployment separate and
least-privilege (top-level `permissions: contents: read`):

- **`docker-smoke`** (every push + PR) — builds the image and asserts `/health == 200` on the
  file:// synthetic backend, plus `docker compose config` validation. **No secrets, no GHCR
  login/push** — pull-request validation needs no cloud credentials.
- **`telemetry-otel`** (every push + PR) — runs the telemetry suite under the `[otel]` extra and
  uploads coverage so the OTel-enabled branches are measured. Key-free.
- **`publish-image`** (push to `main` only) — builds and pushes `ghcr.io/<owner>/<repo>:<sha>` and
  `:latest` using the built-in `GITHUB_TOKEN` (`packages: write`). No external secret.
- **`deploy`** (push to `main` only; `environment: production`) — a **no-op placeholder**. Real
  deployment is intentionally not wired; the protected `production` environment and its required
  reviewers are configured manually in repo settings, and a real deploy is separately approved.

### Secrets & the agent LLM

Runtime config flows through `RTDP_*` settings; secrets are injected at run time (**Doppler**
preferred for a host, or a local gitignored `.env`) — **no keys are committed and there is no
Python Doppler dependency**. The agent's model stays **external and config-driven** via the
OpenAI-compatible boundary (NVIDIA Build/NIM is the intended option) — **not** self-hosted, no
GPU/inference provisioning, and **neither CI nor `/health` ever depends on it**.

> Stage E status: E1 (image) → E2 (telemetry boundary) → E3 (deploy assets) → E4 (CI/CD scaffold)
> → E5 (docs) are in place. **No host, MinIO volume, secrets manager, or observability backend has
> been provisioned**; the `production` environment, deploy secrets, and the real deploy mechanism
> are deferred and separately gated.

---

## Testing

```bash
uv run pytest -m "not localstack"   # unit/integration on file:// (no Docker)
uv run pytest -m localstack         # S3 path (needs LocalStack running)
```

CI runs both jobs on every push (`.github/workflows/ci.yml`): lint + the `file://` suite, and an
integration job against LocalStack S3. See `RUNBOOK.md` for a step-by-step fresh-clone walkthrough.

## Out of scope & roadmap

**Not built yet (intentionally):** true streaming (Kafka/Flink), RAG / vector search, model
fine-tuning, autonomous remediation (the Stage D agent *proposes*; a human applies), orchestration,
dashboards, **real provisioned cloud / multi-host (Kubernetes) deployment**, production IAM, and
data-file compaction. (Stage E adds **single-host containerization + a gated CI/CD scaffold**, but
performs no provisioning and keeps `deploy` a no-op — see [Stage E](#stage-e--productionization--observability).)
The table/partition design anticipates continued appends without a rewrite, and the read API
doubles as the agent's tool surface.

**Direction for later stages (not commitments):**

- **True streaming** — a continuous (Kafka/Flink-style) pipeline, beyond Stage 2B's scheduled
  micro-batch loop.
- **Retrieval (RAG) / vector search** and **richer agent autonomy** beyond the current read-only,
  human-in-the-loop agent.
- **Orchestration & dashboards**, then **real provisioned / multi-host deployment** beyond Stage
  E's single-host containerized demo (real AWS S3 is already a config-only swap).
