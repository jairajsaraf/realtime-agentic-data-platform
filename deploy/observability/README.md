# Observability templates (E6.3)

Starter, **key-free** Datadog assets for the rtdp single-host demo. These are **templates only** —
nothing here is applied to Datadog automatically, and they contain no API keys, application keys,
org IDs, real hostnames, or domains.

## Files

- `dashboard.json` — a starter dashboard (request rate, 5xx, latency, `/health` check status).
- `monitors.json` — starter monitors: **host down**, **/health failing**, **elevated 5xx**, and
  **elevated latency**. Each is named `[rtdp][TEMPLATE] …` so it is obvious they need validation.

## How these apply (E6.3 — needs a live Datadog key; NOT done in E6.1)

These are reviewable definitions to import and validate once Datadog is live. The app exports OTLP
traces under `service:rtdp` (see `RTDP_OTEL_SERVICE_NAME`); the metric names, tags, and thresholds
here are **starting points to confirm against real data** before relying on them. Example (run by you
in E6.3, with your own keys in the environment — never committed):

```
# Dashboard:
curl -X POST "https://api.${DD_SITE}/api/v1/dashboard" \
  -H "DD-API-KEY: ${DD_API_KEY}" -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  -H "Content-Type: application/json" -d @deploy/observability/dashboard.json

# Monitors: validate and POST each object in monitors.json to /api/v1/monitor
```

## Notes

- **No Terraform** — plain JSON, intentionally reviewable in a PR.
- Queries assume APM traces from the FastAPI instrumentation (`trace.fastapi.request.*`). Confirm the
  exact metric names in your Datadog account during E6.3 — OTLP→Datadog naming can vary by pipeline.
- Replace the `instance:rtdp-health` / `env:prod` tags, thresholds, and notification channels with
  your real values in E6.3. None here are real.
- `/health` and CI never depend on Datadog; these assets are additive and optional.
