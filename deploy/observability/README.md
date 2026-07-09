# Observability assets (E6.3)

**Key-free** Datadog assets for the rtdp single-host demo (Datadog **US5**). Reviewable JSON/YAML —
nothing here is applied to Datadog automatically, and they contain no API keys, application keys, org
IDs, real secrets, or domains. **`/health` and CI never depend on Datadog**; these assets are additive
and optional, and monitor import is a manual operator action (below) — **never run by CI or by this
repo**.

## Files

- `dashboard.json` — dashboard for the demo (request rate, 5xx responses, latency percentiles,
  `/health` check status). Imports via `POST /api/v1/dashboard` (analogous to the monitor runbook
  below).
- `monitors.json` — the three monitors documented next.
- `http_check.yaml` — the Datadog Agent HTTP check for the internal `/health` endpoint. It emits the
  `http.can_connect` service check tagged `instance:rtdp_health` (from `name: rtdp_health`), which the
  `/health` monitor queries.

## The three monitors (`monitors.json`)

All three are **notification-free** (no recipients) and their thresholds are **provisional** — add a
notification channel and revisit thresholds against real operating data before enabling alerts.

| Monitor | Type | Query | Purpose |
|---|---|---|---|
| `[rtdp] Host down` | service check | `"datadog.agent.up".over("host:rtdp-demo").last(2).by("host").count_by_status()` | Alerts when the host/Agent stops reporting to Datadog. |
| `[rtdp] /health failing` | service check | `"http.can_connect".over("instance:rtdp_health").last(3).by("host").count_by_status()` | Alerts when the Agent's HTTP check against the internal `/health` endpoint fails. |
| `[rtdp] Elevated request latency (p95)` | metric alert | `avg(last_5m):p95:trace.http.server.request{service:rtdp} > 1` | Flags elevated p95 request latency for `service:rtdp`. |

Both service-check monitors specify an explicit `.by("host")` grouping in the documented clause order
(`over(...).last(N).by("host").count_by_status()`); official Datadog documentation requires service
check monitors to specify a grouping. The metric-alert monitor needs no grouping clause.

A dedicated **5xx-rate monitor is intentionally deferred**: the required trace-*error* metric has not
been confirmed available for this US5 deployment, so an active alert could only ever be no-data until
that metric is verified in the live org. The 5xx signal is kept as a **dashboard widget** instead
(request hits filtered by `http.status_code`). Reintroduce a 5xx monitor only after confirming a
suitable error metric is available in your org.

### Validation status

Repository inspection and official Datadog documentation found **no current `monitors.json` defect**:
the service-check grouping/order fix already landed in commit `76b6592`, giving the documented
`.over(...).last(N).by("host").count_by_status()` structure. **Nothing here has been imported or
live-validated by CI or by this repo.** Final acceptance of the queries — tag resolution, `.by("host")`
grouping behavior, metric availability, thresholds, and no-data behavior — and the import itself
**remain pending your manual validation in the live US5 UI** (next section).

## Monitor validation + import runbook (manual — run by you; never in CI)

> These commands are for **you** to run manually with your own credentials. They are **never executed
> by CI or by this repository**, and no keys are stored in any file here. This is an operator action,
> not deployment automation.

### 1. Prerequisites

- A **Datadog US5** organization.
- `DD_SITE=us5.datadoghq.com` (REST API host: `api.us5.datadoghq.com`).
- `DD_API_KEY` and `DD_APP_KEY` supplied through **your shell environment** (never in repo files). The
  application key / service account must have the **`monitors_write`** permission (monitor management
  via the API requires the application key in addition to the API key).
- `curl` and `jq`.
- **No credentials stored in the repository** — export them in your shell only, for the current session.

```bash
export DD_SITE=us5.datadoghq.com          # REST API host: api.us5.datadoghq.com
# export DD_API_KEY / DD_APP_KEY in your shell yourself — do NOT write them into any file.
```

### 2. Validate every query in the US5 UI **before** importing

Open the US5 monitor UI (Monitors → New Monitor) and paste each query into the matching monitor type,
then confirm the preview resolves before you import anything:

- **`[rtdp] Host down`** — confirm the scope filter `host:rtdp-demo` resolves and `.by("host")` yields a
  single host group.
- **`[rtdp] /health failing`** — confirm `instance:rtdp_health` resolves (this tag is emitted by the
  Agent check in `http_check.yaml`) and `.by("host")` yields a single host group.
- **`[rtdp] Elevated request latency (p95)`** — confirm the `trace.http.server.request` **distribution**
  metric (with percentiles, e.g. `p95:`) resolves for `service:rtdp` and shows current data.
- Confirm the **thresholds** and **no-data** behavior are acceptable for each (thresholds are
  provisional).
- **Stop** if the UI rejects any query, and reconcile the definition before importing.

### 3. Import and record IDs — one `POST` per monitor (single loop)

This **single loop both imports the monitors and records their IDs**: it `POST`s each object in
`monitors.json` exactly once to `/api/v1/monitor`, captures each response once, extracts
`{id, name, type}` once, prints that summary, and appends it to one local operator record — all in the
same iteration. Do **not** run a second creation loop; a repeat `POST` creates duplicate monitors (see
Section 4).

Authenticate with environment-variable references only. `curl --fail-with-body` under `set -euo
pipefail` makes any `4xx`/`5xx` response **stop the loop** rather than be mistaken for success. The
operator-record path is configurable and defaults to a file **outside this repository**:

```bash
set -euo pipefail

# Local operator record of the returned IDs (JSON Lines: one {id, name, type} object per line).
# Configurable; defaults outside the repo.
MONITOR_IDS_FILE="${MONITOR_IDS_FILE:-$HOME/rtdp-monitor-ids.local.jsonl}"
: > "$MONITOR_IDS_FILE"   # initialize/truncate so IDs from a prior run are not mixed in

jq -c '.[]' deploy/observability/monitors.json | while read -r monitor; do
  curl -sS --fail-with-body -X POST "https://api.${DD_SITE}/api/v1/monitor" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -H "DD-API-KEY: ${DD_API_KEY}" \
    -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
    -d "$monitor" \
  | jq '{id, name, type}' \
  | tee -a "$MONITOR_IDS_FILE"
done
```

Notes:
- One monitor object per request (`jq -c '.[]'` emits each array element on its own line); each is
  `POST`ed **once**, and `tee -a` both prints the `{id, name, type}` summary and appends it to
  `MONITOR_IDS_FILE` in the same iteration — there is no second `POST`.
- Do **not** add `set -x` or otherwise echo/log the header values — that would print your credentials.
- On a non-2xx response, `--fail-with-body` prints Datadog's error body and the pipeline returns
  non-zero, so `set -euo pipefail` halts before the next object is sent.

### 4. The operator record of returned IDs

The single loop in Section 3 appends each created monitor's `{id, name, type}` to `MONITOR_IDS_FILE`
(default: `$HOME/rtdp-monitor-ids.local.jsonl`, **outside this repository**) as **newline-delimited
JSON (JSON Lines)** — one object per line, not a single JSON document. That file is your only record of
the created monitor IDs — this section is about **protecting** it, not creating monitors again:

- Keep it outside the repo (the default) or in a gitignored location; **never commit org-specific
  monitor IDs**.
- Do not paste its contents into the repository, PRs, or issues.

**Warning:** `POST /api/v1/monitor` always **creates a new monitor** — there is no upsert. Re-running
the Section 3 loop creates **duplicate** monitors. Import once. To update an existing monitor later,
use `PUT /api/v1/monitor/{id}` with a captured `id`, or remove duplicates in the US5 UI.

**Partial-import warning:** each successful `POST` creates its monitor immediately, so if a later
request fails the import is **partially complete** and the JSONL record holds the monitors already
created. Do **not** blindly rerun the entire creation loop. First inspect the JSONL record and the live
US5 monitor list, then either:

- remove the partially created monitors and restart the loop once, or
- submit only the definitions that were not created.

Use `PUT /api/v1/monitor/{id}` only to update an existing captured monitor — never to resume creation
of a monitor that was never created.

### 5. Post-import verification

- Open each created monitor in the US5 UI and confirm its `name`, `type`, `query`, `tags`, `thresholds`,
  `.by("host")` grouping (service checks), notification-free state, and no-data settings.
- Confirm exactly **three** rtdp monitors exist — **each exactly once** (no duplicates from a repeated
  import).
- Keep the provisional thresholds until real operating data justifies tuning.

### 6. Caveats

- Monitors are **notification-free**; add a notification channel before enabling alerts.
- Thresholds are **provisional**; revisit them against real operating data.
- The dedicated **5xx-rate monitor stays deferred** (see above); the 5xx signal remains a dashboard
  widget.
- Import is an **explicit operator action**, not deployment automation; it does **not** make `/health`
  or CI depend on Datadog.
- `/health` and CI remain **Datadog-independent**.

## Notes

- **No Terraform** — plain JSON/YAML, intentionally reviewable in a PR.
- **Custom spans** (when `RTDP_OTEL_ENABLED=true` + the `[otel]` extra): `rtdp.ingest.batch`
  (ingestion lag + row counts) and `rtdp.agent.tool_call` (tool name/status/latency) surface in APM
  under `service:rtdp` on host `rtdp-demo`. `/health` and CI never depend on this.
- The app exports OTLP traces under `service:rtdp` (see `RTDP_OTEL_SERVICE_NAME`) to the Datadog Agent
  on host `rtdp-demo`, which forwards to Datadog **US5**.
- **Metric names the assets assume** (confirm in the US5 UI before relying on them): request rate uses
  `trace.http.server.request.hits`; latency uses the `trace.http.server.request` **distribution** metric
  (percentiles enabled, e.g. `p50:`/`p95:`). The required trace-*error* metric has not been confirmed
  available for this US5 deployment, so the 5xx dashboard widget uses
  `trace.http.server.request.hits{…,http.status_code:5*}` and the dedicated 5xx monitor is deferred —
  confirm metric availability in the live org. None of this is validated by this repo or CI — verify it
  live in US5.
- The `/health` service check emits `http.can_connect` tagged `instance:rtdp_health` (see
  `http_check.yaml`). No `env` scope is used.
- **No secrets are committed or printed.**
