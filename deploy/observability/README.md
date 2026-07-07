# Observability assets (E6.3)

**Key-free** Datadog assets for the rtdp single-host demo (Datadog **US5**). Reviewable JSON —
nothing here is applied to Datadog automatically, and they contain no API keys, application keys,
org IDs, real secrets, or domains.

## Files

- `dashboard.json` — dashboard for the demo (request rate, 5xx responses, latency percentiles,
  `/health` check status).
- `monitors.json` — monitors: **[rtdp] Host down**, **[rtdp] /health failing**, and **[rtdp] Elevated
  request latency (p95)**. A dedicated 5xx-rate monitor is **deferred** — Datadog US5 exposes no
  `trace.http.server.request.errors` metric, so an active alert would only ever be no-data. The 5xx
  signal is kept as a dashboard widget instead (request hits filtered by status code).

## How these apply (E6.3 — needs a live Datadog application key; run by you, never in CI)

These are reviewable definitions to import and validate. The app exports OTLP traces under `service:rtdp`
(see `RTDP_OTEL_SERVICE_NAME`) to the Datadog Agent on host `rtdp-demo`, which forwards to Datadog
**US5**. Validate every query in the US5 UI before relying on it. Example (run by you, with your own keys
in the environment — never committed):

```
# Datadog site for this deployment: US5
export DD_SITE=us5.datadoghq.com      # API host: api.us5.datadoghq.com

# Dashboard:
curl -X POST "https://api.${DD_SITE}/api/v1/dashboard" \
  -H "DD-API-KEY: ${DD_API_KEY}" -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  -H "Content-Type: application/json" -d @deploy/observability/dashboard.json

# Monitors: validate and POST each object in monitors.json to /api/v1/monitor
```

## Notes

- **No Terraform** — plain JSON, intentionally reviewable in a PR.
- **Custom spans** (when `RTDP_OTEL_ENABLED=true` + the `[otel]` extra): `rtdp.ingest.batch`
  (ingestion lag + row counts) and `rtdp.agent.tool_call` (tool name/status/latency) surface in APM
  under `service:rtdp` on host `rtdp-demo`. `/health` and CI never depend on this.
- Live metric names (confirmed in US5): request rate uses `trace.http.server.request.hits`; latency uses
  the `trace.http.server.request` **distribution** metric (percentiles enabled, e.g. `p50:`/`p95:`).
  `trace.http.server.request.errors` is **absent**, so the 5xx dashboard widget uses
  `trace.http.server.request.hits{…,http.status_code:5*}` and the dedicated 5xx monitor is deferred.
- The `/health` service check emits `http.can_connect` tagged `instance:rtdp_health` (see
  `deploy/observability/http_check.yaml`). No `env` scope is used — live `env` is unset (`none`).
- Monitors are **notification-free** (no recipients) and thresholds are provisional; add a notification
  channel and revisit thresholds before enabling alerts. **No secrets are committed or printed.**
- `/health` and CI never depend on Datadog; these assets are additive and optional.
