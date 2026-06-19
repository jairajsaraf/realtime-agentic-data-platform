# Stage 1 Runbook — fresh clone (Windows PowerShell)

Step-by-step for a new developer bringing the Iceberg lakehouse up from a fresh clone.
The **primary path is LocalStack S3**; a **`file://` fallback** runs everything without Docker.

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
uv run pytest -m "not localstack"     # -> 97 passed, 5 deselected
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

## G. Cleanup

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
- [ ] `pytest -m "not localstack"` (97) and `-m localstack` (5) pass; `ruff` clean
- [ ] Real AWS S3 is a config-only swap (`RTDP_STORAGE_BACKEND=aws`)
- [ ] `file://` fallback runs everything without Docker
- [ ] `rtdp serve` starts the read-only API; `/docs` renders OpenAPI for all six endpoints
- [ ] `rtdp stream` runs the scheduled micro-batch loop (within-batch dedup, skip-empty,
      read-time latest-state); `rtdp maintain expire-snapshots` bounds snapshot growth (metadata-only)

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
- **Out of scope:** true streaming (Kafka/Flink), agents, orchestration, dashboards,
  real AWS deployment, production IAM.
```
