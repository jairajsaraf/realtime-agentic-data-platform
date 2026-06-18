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
Success: `3 passed` (bucket round-trip, bronze ingest, demo on S3).

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
uv run pytest -m "not localstack"     # -> 34 passed, 3 deselected
uv run ruff check .                    # -> All checks passed!
```

---

## D. Live OpenSky (optional — real public data, NOT run in CI)

```powershell
uv run rtdp ingest --source opensky-live
```
Opt-in and network-gated; hits the real OpenSky API. Anonymous tier works for a snapshot
(set `RTDP_OPENSKY_CLIENT_ID` / `RTDP_OPENSKY_CLIENT_SECRET` for higher limits). Its output is
**never committed** (OpenSky license).

---

## E. Cleanup

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
- [ ] `pytest -m "not localstack"` (34) and `-m localstack` (3) pass; `ruff` clean
- [ ] Real AWS S3 is a config-only swap (`RTDP_STORAGE_BACKEND=aws`)
- [ ] `file://` fallback runs everything without Docker

## Known limitations (Stage 1)

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
- pyiceberg cannot compact / rewrite data files (out of scope for Stage 1).
- **Out of scope:** streaming ingestion, agents, orchestration, dashboards, Kafka/Flink,
  real AWS deployment, production IAM.
```
