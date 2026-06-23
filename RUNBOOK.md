# Runbook — fresh clone (Windows PowerShell)

Step-by-step for a new developer bringing the platform up from a fresh clone (Stages 1, 2A, 2B,
and the Stage D agent). The **primary path is LocalStack S3**; a **`file://` fallback** runs
everything without Docker.

## Prerequisites

| Tool | Notes |
|------|-------|
| Git | clone the repo |
| Python 3.12 | `python --version` → 3.12.x |
| uv | `irm https://astral.sh/uv/install.ps1 | iex` (then restart shell so PATH updates) |
| Docker Desktop | required for the primary LocalStack S3 path; must be **running** |

Java/JVM is **not** required (pyiceberg is pure-Python).

## Setup

```powershell
git clone <your-repo-url> realtime-agentic-data-platform
cd realtime-agentic-data-platform
uv sync --frozen
```
Success: `Installed N packages`; a `.venv\` appears. `--frozen` installs the exact `uv.lock` versions.

---

## A. Primary path — LocalStack S3 (default backend)

```powershell
docker compose up -d
docker compose ps
```
Success: `rtdp-localstack ... Up (healthy)`, port `0.0.0.0:4566->4566`.

```powershell
uv run rtdp info
```
Success: `storage_backend : localstack`, `warehouse_location : s3://lakehouse/warehouse`,
`s3_endpoint : http://localhost:4566`.

```powershell
uv run rtdp ingest --source synthetic --rows 50 --seed 42
uv run rtdp ingest --source synthetic --rows 30 --seed 7
```
Success: each prints `Data quality: PASS (...)` then `Wrote N rows ...`; `snapshot_count` is `1`
then `2`. (54 and 34 rows, including a few injected WARN rows.)

DQ failure handling (optional):
```powershell
uv run rtdp ingest --source synthetic --rows 20 --inject-failures
```
Success: `Data quality: FAIL`, `INGEST ABORTED ... nothing written`, exit code `1`.

```powershell
uv run rtdp demo catalog
uv run rtdp demo schema-evolution
uv run rtdp demo partition-evolution
uv run rtdp demo time-travel
```
Success signals:
- **catalog** — namespaces `['bronze','demo']`; loads `demo.opensky_state_vectors` at `s3://...`.
- **schema-evolution** — before 21 cols → after 22 (`data_quality_grade`); `=B @ S1: 0`, `=B now: 10`.
- **partition-evolution** — `before day(...)` → `after hour(...)`; `pre-existing files unchanged (no rewrite): True`; `new-file spec ids: [1]`; total rows 36.
- **time-travel** — `rows @ S1: 20`, `rows now: 35`; `snapshot_as_of_timestamp(S1.ts)` resolves to S1.

```powershell
uv run python scripts/check_s3_objects.py
```
Success: prints `metadata.json`, `.avro`, `.parquet` counts > 0 and `event_day=...` partition keys.

```powershell
uv run pytest -m localstack
```
Success: `5 passed` (bucket round-trip, bronze ingest, demo on S3, read API over S3,
micro-batch append loop over S3).

---

## B. Fallback path — file:// (no Docker)

```powershell
$env:RTDP_STORAGE_BACKEND="file"
uv run rtdp info                      # storage_backend : file ; warehouse file:///.../_warehouse
uv run rtdp ingest --source synthetic --rows 50 --seed 42
uv run rtdp demo all
Remove-Item Env:\RTDP_STORAGE_BACKEND # back to the LocalStack default
```
Same success signals as section A, but the warehouse is `file:///.../_warehouse`. No Docker needed.
This path is a dev/CI convenience — **not** the target architecture.

---

## C. Tests & lint (no Docker)

```powershell
uv run pytest -m "not localstack"     # -> 144 passed, 5 deselected
uv run ruff check .                    # -> All checks passed!
```

---

## D. Serving layer (Stage 2A — read-only API)

Start the FastAPI read API over whichever backend is configured (LocalStack S3 by default,
`file://` if `RTDP_STORAGE_BACKEND=file`). **Ingest some data first** — the API is read-only
and does not create or mutate tables.

```powershell
uv run rtdp ingest --source synthetic --rows 50   # seed the bronze table
uv run rtdp serve                                  # http://127.0.0.1:8000
```
Success: `Uvicorn running on http://127.0.0.1:8000`. Override the bind with
`$env:RTDP_API_HOST` / `$env:RTDP_API_PORT`. Stop with `Ctrl+C`.

Open the interactive OpenAPI docs at **http://127.0.0.1:8000/docs**, or curl the endpoints
(from another shell):

```powershell
curl.exe "http://127.0.0.1:8000/health"
curl.exe "http://127.0.0.1:8000/flights?callsign=DLH123&limit=5"
curl.exe "http://127.0.0.1:8000/flights/bbox?min_lat=45&max_lat=55&min_lon=5&max_lon=15&limit=10"
curl.exe "http://127.0.0.1:8000/stats/flights-per-interval?interval=day&group_by=origin_country"
curl.exe "http://127.0.0.1:8000/snapshots"
curl.exe "http://127.0.0.1:8000/meta"
```
`/health` returns 200 when the catalog/table are reachable, 503 otherwise. Every read endpoint
accepts `as_of_snapshot_id` / `as_of_timestamp` (mutually exclusive → 400) for time-travel and
echoes the resolved `snapshot_id`. Read-only and local-first: no auth/rate-limiting/caching.

---

## E. Incremental micro-batch ingestion (Stage 2B — near-real-time, not true streaming)

```powershell
# scheduled micro-batch loop: poll a source and append one micro-batch per interval
uv run rtdp stream --source synthetic --interval 5 --max-batches 10
```
Success: one line per batch (`batch N: wrote R rows -> snapshot ...; DQ PASS`) and `snapshot_count`
climbs by one per non-empty batch. `--max-batches 0` (default) runs until `Ctrl+C`. Override the
cadence with `--interval` or `RTDP_STREAM_INTERVAL_SECONDS`. The synthetic path uses a deterministic
continuous generator (advancing event-time, stable fleet) — no Docker/network needed on `file://`.

Within-batch duplicates on `(icao24, last_contact)` are dropped before append; empty polls are
skipped (no empty snapshot); transient source errors back off and retry. Bronze stays append-only —
read the **read-time** latest-state (one row per aircraft) via `rtdp.query.query_latest_state`.

Snapshot/metadata-growth maintenance (opt-in, **metadata-only — NOT compaction**):
```powershell
uv run rtdp maintain expire-snapshots --retain 10
```
Success: `Expired N snapshot(s); retained the newest 10. Metadata-only ...`. Removes old snapshots
from the table metadata; does **not** delete data files or compact small files. Current snapshot is
never expired.

Live data (opt-in, network-gated, NEVER in CI): `uv run rtdp stream --source opensky-live --interval 60`.

---

## F. Live OpenSky (optional — real public data, NOT run in CI)

```powershell
uv run rtdp ingest --source opensky-live
```
Opt-in and network-gated; hits the real OpenSky API. Anonymous tier works for a snapshot
(set `RTDP_OPENSKY_CLIENT_ID` / `RTDP_OPENSKY_CLIENT_SECRET` for higher limits). Its output is
**never committed** (OpenSky license).

---

## G. Agent (Stage D — natural-language agent over the read API)

The agent calls the **running Stage 2A API over HTTP** as its tools. Start the API first
(section D), then point the agent at a model endpoint. The agent is **read-only and
human-in-the-loop**: it proposes remediations but never mutates data, runs ingestion, or expires
snapshots.

### 1. Start the read-only API (the agent's tool surface)

```powershell
uv run rtdp ingest --source synthetic --rows 80   # seed data (includes WARN rows for DQ)
uv run rtdp serve                                  # http://127.0.0.1:8000  (leave running)
```

### 2. Configure a model endpoint (open dev model or frontier — config only)

```powershell
# Example: an OpenAI-compatible open-model endpoint (NVIDIA NIM). No keys are committed.
$env:RTDP_AGENT_BASE_URL = "https://integrate.api.nvidia.com/v1"
$env:RTDP_AGENT_MODEL    = "meta/llama-3.1-8b-instruct"
$env:RTDP_AGENT_API_KEY  = "<your key>"
# Swap to a frontier provider by changing these three vars only. Optional:
#   $env:RTDP_AGENT_API_URL (read API base, default http://127.0.0.1:8000)
#   $env:RTDP_AGENT_MAX_TURNS (model round-trips), $env:RTDP_AGENT_MAX_TOOL_CALLS (tool budget)
#   $env:RTDP_AGENT_TIMEOUT_SECONDS
```

### 3. Ask (one-shot) or run interactively (from a second shell)

```powershell
uv run rtdp agent "How many records are there, and which snapshot answered?"
uv run rtdp agent "Are there any data-quality issues? Propose fixes."
uv run rtdp agent --interactive          # REPL; type 'quit' to exit
uv run rtdp agent "..." --json           # machine-readable: answer + provenance + tokens
```
Success: an answer followed by a `Sources: <endpoint> @ snapshot <id>` line. Exit code `2` with a
clear message if the API is unreachable (start `rtdp serve`) or no model is configured.

**HITL data-quality workflow:** for DQ questions the agent calls a read-only `diagnose_data_quality`
tool that re-derives anomalies (over-speed, unknown `position_source`, out-of-range
coordinates/altitude, nulls, duplicate state keys) from the rows the API returns, and prints
`PROPOSED (requires human approval; not applied): ...` remediations. **You** decide whether to act
(e.g. adjust an ingest filter); the agent never applies anything. Diagnosis is a **bounded sample**
(one queried window, capped by the API row limit, over the snapshots queried) and says so.

### 4. Fake tests vs live eval

- **Fake-LLM unit tests (CI; no network/keys):**
  ```powershell
  uv run pytest -m "not localstack" -k agent
  ```
  Drives the agent with a deterministic stub LLM — no model calls.
- **Opt-in live eval (never in CI):** with the API running and the model vars set above —
  ```powershell
  uv run python scripts/eval_agent.py --report _agent_eval_report.json
  ```
  Runs a few cases and prints `PASS/FAIL` each with **grounding/faithfulness** (claims match cited
  tool results + snapshot), **tool-call correctness** (right endpoint), **latency**, and **token
  usage**; failures are listed explicitly. The JSON report path is git-ignored and contains no keys.
  A non-zero exit means at least one case failed — read the failures, don't just trust a pass.

---

## H. Stage E — containerized single-host topology & observability (ops-only)

Stage E runs the existing surfaces from **one Docker image** on a single host, with an optional
telemetry boundary and a CI/CD scaffold. It is **additive and ops-only** — no data-plane change,
no new endpoint (incl. **no `/metrics`**), and the public demo serves **synthetic data only**.

### 1. Build the image and smoke it (no secrets / network / MinIO)

```powershell
docker build -t rtdp:local .
# Git Bash on Windows rewrites container paths; disable MSYS path conversion for the smoke script:
$env:MSYS_NO_PATHCONV="1"; $env:MSYS2_ARG_CONV_EXCL="*"
bash scripts/docker_smoke.sh                       # build -> seed (file://) -> serve -> /health 200
```
Success: `OK: /health returned 200` then a JSON health body. The same image exposes every runtime
as a command: `serve` (read API), `stream` (micro-batch writer), `maintain expire-snapshots`
(one-shot), `agent "<q>"` (on-demand). `SKIP_BUILD=1` smokes a prebuilt image.

### 2. Single-host Docker Compose topology

```powershell
docker compose -f deploy/docker-compose.yml up -d --build           # file:// (default; no secrets)
docker compose -f deploy/docker-compose.yml --profile s3 up -d      # self-hosted MinIO (aws backend)
```
`api` + `stream` share one volume; the API is at http://localhost:8000 (`/health` returns 503 until
the first stream batch creates the table, then 200). For the MinIO path, set
`RTDP_STORAGE_BACKEND=aws` and point `RTDP_S3_ENDPOINT_URL` at the MinIO service (real AWS stays the
default when no endpoint is set). The catalog is **local SQLite on the shared volume** in every
backend -> single-writer: keep one `stream` and schedule `maintain` not to overlap it. One-shot
maintenance: `docker compose -f deploy/docker-compose.yml run --rm maintain`. See `deploy/README.md`.

### 3. Telemetry boundary (no-op unless enabled AND installed)

Logging is stdlib structured logging by default. OpenTelemetry tracing turns on only when
`RTDP_OTEL_ENABLED=true` **and** the optional `[otel]` extra is installed; otherwise the boundary is
a no-op (and degrades gracefully with a warning if enabled but the extra is absent).
```powershell
uv sync --extra otel                                    # opt in to the OTel SDK + OTLP exporter
uv run --extra otel -- pytest tests/test_telemetry.py   # OTel-enabled branch tests
```
Settings (`RTDP_*`): `RTDP_OTEL_ENABLED`, `RTDP_OTEL_SERVICE_NAME`,
`RTDP_OTEL_EXPORTER_OTLP_ENDPOINT` (provider-agnostic — a collector or the Datadog Agent),
`RTDP_LOG_FORMAT` (`text`|`json`), `RTDP_LOG_LEVEL`. There is **no `/metrics` endpoint**.

### 4. Secrets & the agent model

Inject secrets at run time — **Doppler** (`doppler run -- docker compose ...`) or a local,
gitignored `.env` (copy `deploy/.env.example`). No keys are committed; there is no Python Doppler
dependency. The agent's LLM stays **external and config-driven** (`RTDP_AGENT_*`; NVIDIA Build/NIM
intended) — not self-hosted, no GPU/inference provisioning, and **neither CI nor `/health` depends
on it**.

### 5. CI/CD (validate -> publish -> gated no-op deploy)

`.github/workflows/ci.yml` (least-privilege; top-level `contents: read`): `docker-smoke` and
`telemetry-otel` run on every push + PR with **no secrets**; `publish-image` pushes to GHCR via the
built-in `GITHUB_TOKEN` on `main` only; `deploy` (push to `main`, `environment: production`) is a
**no-op placeholder**. Provisioning the `production` environment + required reviewers and wiring a
real deploy are manual and separately approved — **nothing has been provisioned**.

---

## I. Cleanup

```powershell
docker compose down                   # stop + remove LocalStack (its S3 data is ephemeral)
# optional: remove local state (gitignored)
Remove-Item -Recurse -Force _warehouse, _demo, .localstack -ErrorAction SilentlyContinue
```

---

## Definition of Done checklist

- [ ] `uv sync --frozen` installs from a fresh clone (Python 3.12, no JVM)
- [ ] Iceberg tables are catalog-backed and stored in LocalStack S3
- [ ] Ingestion writes real Iceberg snapshots (not just partitioned Parquet)
- [ ] DQ warn/fail with clear output (`--inject-failures` aborts, exit 1)
- [ ] Schema-evolution demo works (add nullable column; old vs new snapshot)
- [ ] Partition-evolution demo works **without rewriting** existing data
- [ ] Time-travel / snapshot demo works
- [ ] `pytest -m "not localstack"` (144) and `-m localstack` (5) pass; `ruff` clean
- [ ] Real AWS S3 is a config-only swap (`RTDP_STORAGE_BACKEND=aws`)
- [ ] `file://` fallback runs everything without Docker
- [ ] `rtdp serve` starts the read-only API; `/docs` renders OpenAPI for all six endpoints
- [ ] `rtdp stream` runs the scheduled micro-batch loop (within-batch dedup, skip-empty,
      read-time latest-state); `rtdp maintain expire-snapshots` bounds snapshot growth (metadata-only)
- [ ] `rtdp agent "<q>"` answers via the read API with endpoint+snapshot citations; fake-LLM agent
      tests are green with no network/keys; the live eval harness is opt-in (never in CI)
- [ ] Stage E: one Docker image with `serve`/`stream`/`maintain`/`agent` entrypoints; single-host
      compose up; `bash scripts/docker_smoke.sh` returns `/health` 200; telemetry stays no-op unless
      `RTDP_OTEL_ENABLED` + the `[otel]` extra; CI publishes to GHCR on `main` and `deploy` is a
      gated no-op (no provisioning done)

## Known limitations

- The committed/CI data path is **synthetic** (OpenSky-shaped). Real OpenSky is **opt-in**
  (`--source opensky-live`), network-gated, never run in CI, never committed.
- LocalStack S3 is **ephemeral** — `docker compose down` discards it; re-run ingestion to repopulate.
  The SQLite catalog pointer lives in `_warehouse\catalog.db` (gitignored). **Gotcha:** if you
  tear LocalStack down and bring it back up without clearing local state, the stale catalog points
  at S3 metadata that no longer exists and the next run fails with `FileNotFoundError`. Run
  `make reset` (or `Remove-Item -Recurse -Force _warehouse, _demo, .localstack`) after a teardown,
  before re-ingesting. Fresh clones and CI are unaffected.
- Partition evolution day→hour is a **replace** (pyiceberg rejects two partition fields on one
  source column); existing data is still not rewritten.
- **Stage 2B micro-batch growth:** each interval appends a snapshot (and small data file), so
  snapshots/metadata and small files accumulate. `rtdp maintain expire-snapshots` bounds
  snapshot/metadata growth, but it is **metadata-only**: pyiceberg can expire snapshots yet
  **cannot compact / rewrite or delete data files** in this build. Data-file compaction is a
  true-engine (e.g. Spark) concern, intentionally **not faked** here — a direct consequence of the
  Stage 1 pyiceberg/no-JVM trade-off.
- **Stage D agent:** consumes the read API only (no direct catalog/query/DuckDB access);
  read-only and human-in-the-loop (proposes remediations, never applies them). DQ diagnosis is
  re-derived from API responses — bounded by the queried window, the API row limit, and available
  snapshots (Pandera WARN/FAIL history is not persisted). Live model calls are opt-in and
  config-driven (`RTDP_AGENT_*`); unit tests use a deterministic fake LLM.
- **Stage E (ops-only):** single-host Docker topology with a **local SQLite catalog on the shared
  volume** (single-writer; one `stream`). The telemetry boundary is **no-op unless** `RTDP_OTEL_ENABLED`
  is set **and** the `[otel]` extra is installed; nothing is exported during tests, and there is **no
  `/metrics` endpoint**. The deployed demo serves **synthetic data only**; the agent LLM is external/
  config-driven (NVIDIA Build/NIM intended) and never gates CI or `/health`. `deploy` is a gated
  no-op — **no host / MinIO volume / secrets manager / observability backend has been provisioned**.
- **Out of scope:** true streaming (Kafka/Flink), RAG/vector search, fine-tuning, autonomous
  remediation (the Stage D agent proposes; a human applies), orchestration, dashboards, real
  provisioned / multi-host (Kubernetes) deployment, production IAM.
```
