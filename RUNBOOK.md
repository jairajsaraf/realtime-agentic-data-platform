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

### 5. CI/CD (validate -> publish -> gated deploy)

`.github/workflows/ci.yml` (least-privilege; top-level `contents: read`): `docker-smoke` and
`telemetry-otel` run on every push + PR with **no secrets**; `publish-image` pushes to GHCR via the
built-in `GITHUB_TOKEN` on `main` only; `deploy` (push to `main`, `environment: production`) is a
**real, gated SSH deploy** — protected by the `production` environment (required reviewers) and run
only on manual approval. The host, secrets, and observability backend are **provisioned and the demo
is deployed live** (E6.2–E6.3); a checkout-alignment guard keeps an approved image from running
against a stale host checkout (see below).

---

## Stage E6 — single-host go-live

**Status: live.** E6.1 (repo-side automation + Caddy/Datadog compose profiles, host bootstrap +
deploy scripts, the `[otel]` image build path, and a real gated SSH deploy job), **E6.2**
(provisioning + first deploy), and **E6.3** (observability go-live) are **complete**. The single-host
demo is live at **[demo.agentic-data-platform.me](https://demo.agentic-data-platform.me)** — a
read-only API over automatic HTTPS, **synthetic data only** — with the OpenTelemetry → Datadog
boundary validated in Datadog US5 (traces under `service:rtdp`, `/health` service check green). The
deploy stays **gated** (protected `production` environment). This section documents the go-live and
its operations; teardown and cost control are at the end.

### Architecture (as deployed)

```
                         Internet
                            │  DNS A-record: demo.<you>.me -> droplet IP
                            ▼
                 ┌────────────────────────┐  inbound (UFW): only 22 / 80 / 443
                 │ DigitalOcean Ubuntu LTS │
                 │ droplet (no GPU)        │
                 │                         │
                 │  ┌───────────────┐      │  automatic Let's Encrypt TLS
                 │  │ Caddy  (edge) │◀─────┼── :80/:443
                 │  └──────┬────────┘      │     reverse_proxy api:8000
                 │         ▼               │  (compose net; API bound to 127.0.0.1)
                 │  ┌───────────────┐      │
                 │  │ api  (serve)  │      │
                 │  └──────┬────────┘      │
                 │         │ reads         │
                 │  ┌──────┴────────┐      │  appends micro-batches (synthetic only)
                 │  │ stream        │──────┼─▶ shared volume: SQLite catalog + warehouse
                 │  └───────────────┘      │            │
                 │  ┌───────────────┐      │            ▼
                 │  │ MinIO  (s3)   │◀─────┼── Iceberg data (aws backend, RTDP_S3_ENDPOINT_URL)
                 │  └───────────────┘      │
                 │  ┌───────────────┐      │  OTLP gRPC :4317 (internal only)
                 │  │ datadog-agent │◀─────┼── api/stream traces (when RTDP_OTEL_ENABLED=true)
                 │  └──────┬────────┘      │
                 └─────────┼───────────────┘
                           ▼ (E6.3)            Doppler injects RTDP_* / DD_API_KEY on host
                        Datadog                GitHub `production` env secrets = SSH deploy creds only
   deploy: push main -> CI publish image -> [manual approve] -> SSH -> host_deploy.sh
           (verify host checkout == approved SHA -> pull/up/health)
   external (optional): agent LLM via NVIDIA NIM (config-only); /health + CI never depend on it
```

### E6.2 — provisioning + first deploy (manual; human-in-the-loop)

1. **Claim Student Pack offers** (verify availability first): DigitalOcean credit, Datadog free Pro,
   Doppler free Team, Namecheap free `.me`. (NVIDIA Build/NIM optional, agent-only.)
2. **Create one small Ubuntu LTS droplet** (smallest viable size, no GPU); note its public IP.
3. **DNS:** at Namecheap, add an **A-record** for your `.me` host → the droplet IP; wait for it to
   resolve. (If the domain can't be claimed, **stop and ask** before any fallback.)
4. **Doppler:** create a project + config; add `RTDP_*` (storage/MinIO/AWS, OTel endpoint),
   `DD_API_KEY`, `DD_SITE`, `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`, `RTDP_PUBLIC_HOSTNAME`,
   `RTDP_API_BIND=127.0.0.1`, `COMPOSE_PROFILES=s3,edge,observability`. Create a
   **service token** for the host. **Do NOT add `RTDP_IMAGE` to Doppler** — it is
   deployment-controlled: the gated deploy passes the approved commit's SHA-pinned ref and
   `host_deploy.sh` runs `doppler run --preserve-env=RTDP_IMAGE`, so a Doppler secret can't override
   the approved image.
5. **GitHub `production` environment:** create it, add **required reviewers**, restrict to `main`.
6. **Add `production` environment secrets:** `DEPLOY_SSH_HOST` (droplet host/IP), `DEPLOY_SSH_USER`
   (`deploy`), `DEPLOY_SSH_KEY` (the deploy user's **private** key), `DEPLOY_SSH_PATH` (deploy dir,
   e.g. `/opt/rtdp`), and **`DEPLOY_SSH_KNOWN_HOSTS`** (the droplet's pinned `known_hosts` line — see
   the host-key caveat below).
7. **Bootstrap the host:** copy the repo to the host, then `sudo bash deploy/bootstrap_host.sh`; add
   your SSH **public** key to `deploy`'s `authorized_keys`; configure the Doppler service token in the
   `deploy` user's environment.
8. **Advance the host checkout, then approve the deploy:** the CI deploy wrapper verifies the host
   checkout `HEAD` matches the approved commit **and the worktree is clean**, and **fails closed**
   otherwise (untracked files also block; it never `git pull`s for you). First, as `deploy` on the host:
   `cd "$DEPLOY_SSH_PATH" && git fetch origin main && git checkout <approved-sha>` (or `git pull --ff-only`;
   then `git status --short` clean). Then push to `main` and **Actions → run → Review deployments →
   Approve**. The job SSHes in; it **fails if the checkout is stale or dirty, or `/health` doesn't pass**.
9. Browse `https://<your-domain>/health` and `/docs` once DNS + Let's Encrypt settle.

> **Host-key caveat:** without a pinned key the deploy job falls back to `ssh-keyscan`, which is
> **trust-on-first-use bootstrapping, not out-of-band verification**. During E6.2, capture the
> droplet's host key from the DigitalOcean console (or the first interactive SSH login), verify it,
> and store the `known_hosts` line as the **`DEPLOY_SSH_KNOWN_HOSTS`** secret so the deploy verifies
> against it and the first CI connection can't be spoofed.

### E6.3 — observability go-live

With the host up, the `observability` profile runs a **Datadog Agent** that receives app traces over
**OTLP gRPC on `datadog-agent:4317`** (internal to the compose network only — never published) and
runs an HTTP check against the API's internal `/health`:

- **`DD_HOSTNAME`** — set to the configured Datadog host label (the deploy uses `rtdp-demo`, which the
  dashboard and monitors scope to via `host:`). A containerized Agent otherwise can't reliably
  determine its host name on DigitalOcean (no cloud metadata, no mounted Docker socket) and would exit.
- **`http_check`** (`deploy/observability/http_check.yaml`, mounted read-only into the Agent) emits the
  **`http.can_connect`** service check tagged **`instance:rtdp_health`** against
  `http://api:8000/health` — no new host or public surface.
- **Import + validate** the dashboard and monitors from `deploy/observability/` against Datadog **US5**
  — the import steps and validated metric names are in **`deploy/observability/README.md`** (run by you
  with your own keys; never in CI). Traces appear under `service:rtdp`; validate every query in the US5
  UI before relying on it.

`DD_API_KEY` / `DD_SITE` are injected from Doppler at run time. **`/health` and CI never depend on
Datadog**, and there is no `/metrics` endpoint.

### Local verification (no cloud, no real keys)

```powershell
docker compose -f deploy/docker-compose.yml config                       # default renders, key-free
docker compose -f deploy/docker-compose.yml `
  --profile s3 --profile edge --profile observability config             # all profiles render
bash -n deploy/bootstrap_host.sh deploy/host_deploy.sh                   # shell syntax (also run in CI)
RTDP_DEPLOY_EXPECTED_SHA=deadbeef bash deploy/host_deploy.sh             # checkout guard: ERRORs + exits 1 (pre-docker)
# Optional bring-up (file:// synthetic + Caddy):
docker compose -f deploy/docker-compose.yml --profile edge up -d
curl -k https://localhost/health        # Caddy internal CA (or: curl http://localhost:8000/health)
docker compose -f deploy/docker-compose.yml --profile edge down
```
Caddy local TLS uses an internal CA, so `-k` is expected. The Datadog Agent needs a real `DD_API_KEY`,
so leave the `observability` profile **out** of local bring-up (or expect the Agent to error) — that is
an E6.3 concern.

### Teardown & cost control

Do these in order to fully stop the demo and its billing:

- **Remove the advertised demo URL first:** delete the **Live demo** callout from `README.md` and clear
  the live URL from the **GitHub About** description, so the docs never advertise a demo that is about
  to go offline. (README is a docs-only edit; the About field is a GitHub UI change.)
- **Destroy the droplet — this is what stops billing.** DigitalOcean dashboard → Droplet → Destroy.
  **Note:** `docker compose ... down` (even with `-v`) only stops the containers/volumes on the host;
  it does **NOT** stop droplet billing. Only destroying the droplet does.
- **Stop services first if keeping the host:** on the host, `doppler run -- docker compose -f
  deploy/docker-compose.yml down` (add `-v` to also drop volumes); `docker system prune -a` and
  `docker volume prune` to reclaim space.
- **Revoke secrets:** revoke the **Doppler host service token** (and rotate any exposed `RTDP_*` /
  `DD_API_KEY` values); rotate the `DEPLOY_SSH_KEY` and remove the deploy user's authorized key if
  retiring the host.
- **GitHub secret cleanup (optional):** delete the `production` environment secrets.
- **Datadog cleanup (optional):** delete the imported dashboard and monitors; the Agent stops
  reporting once the droplet is gone.
- **DNS decision:** delete the Namecheap A-record (or keep it parked if you plan to redeploy).
- **Watch credit burn:** the DigitalOcean **Billing** page; pick the **smallest viable droplet**.
  *(Exact monthly cost — read it from DigitalOcean's official pricing page when sizing; not asserted
  here.)*

### Security notes

- **Synthetic data only**; **no live OpenSky** in CI or the public demo.
- **No secrets in the repo** — SSH creds live in the GitHub `production` environment; `RTDP_*` and
  `DD_API_KEY` live in Doppler.
- **Caddy handles TLS** (automatic Let's Encrypt); the **Namecheap bundled SSL certificate is unused**.
- **`/health` and CI never depend on** NVIDIA, Datadog, Doppler, MinIO, or the internet.
- **Datadog/observability requires a real `DD_API_KEY`** (from Doppler) and is wired live as of **E6.3**.
- The API's port 8000 is bound to **127.0.0.1** on the host (Caddy fronts it); UFW allows only
  22/80/443 inbound.

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
      `RTDP_OTEL_ENABLED` + the `[otel]` extra; CI publishes to GHCR on `main` and runs a **real,
      gated** SSH deploy — the single-host demo is deployed live behind Caddy TLS with the
      OpenTelemetry → Datadog boundary validated (synthetic data only)

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
  config-driven (NVIDIA Build/NIM intended) and never gates CI or `/health`. `deploy` is a **real,
  gated** SSH deploy; the single-host stack is **provisioned and running live** behind Caddy TLS with
  the OpenTelemetry → Datadog boundary validated — but it stays **single-host** (multi-host /
  Kubernetes / real-AWS remains out of scope).
- **Out of scope:** true streaming (Kafka/Flink), RAG/vector search, fine-tuning, autonomous
  remediation (the Stage D agent proposes; a human applies), orchestration, **product/business-facing
  analytics and operational dashboards** (beyond the live E6.3 Datadog observability dashboard), real
  provisioned / multi-host (Kubernetes) deployment, production IAM.
```
