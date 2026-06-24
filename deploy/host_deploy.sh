#!/usr/bin/env bash
#
# Stage E6 — host-side deploy action (the sole remote command run by the gated CI/CD deploy job).
#
# Pulls the published image and (re)starts the single-host compose stack, then verifies /health.
# Run ON the prepared host, from a checkout/copy of this repo at the deploy path. The CI deploy job
# invokes exactly: `bash "$DEPLOY_SSH_PATH/deploy/host_deploy.sh"`. This script does NOT fetch code
# (no git pull) — the deploy path is prepared/updated out-of-band — and prints no secrets.
#
# Runtime secrets (RTDP_*) are injected by Doppler when available (`doppler run -- docker compose`).
# /health depends only on the API: never on Doppler, Datadog, NVIDIA, or the internet.
#
# Env (all optional):
#   COMPOSE_PROFILES        compose profiles to bring up   (default: s3,edge,observability)
#   DEPLOY_HEALTH_URL       URL to poll for readiness       (default: http://127.0.0.1:8000/health)
#   DEPLOY_HEALTH_RETRIES   health-check attempts           (default: 60)
#   DEPLOY_HEALTH_INTERVAL  seconds between attempts        (default: 2)
#   RTDP_DEPLOY_NO_DOPPLER  set to 1 to skip the doppler wrapper even if doppler is installed

set -euo pipefail

log() { printf '>> %s\n' "$*"; }

# --- locate the repo root from this script's path, then run from there so the compose `build:`
#     context (..) and the relative ./Caddyfile both resolve correctly ---
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cd "${repo_root}"

compose_file="deploy/docker-compose.yml"
if [ ! -f "${compose_file}" ]; then
  echo "ERROR: ${compose_file} not found under ${repo_root}." >&2
  exit 1
fi

# Profiles compose should activate (compose reads COMPOSE_PROFILES natively). Exported so both the
# `pull` and `up` invocations see the same set.
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-s3,edge,observability}"

# Fail closed: on the deploy path Caddy (the `edge` profile) fronts the API, so port 8000 must never
# be published publicly. Default the host bind to loopback unless explicitly overridden — a missing
# env var must not expose 8000 (Docker publishes past UFW). Caddy still reaches the API as api:8000.
export RTDP_API_BIND="${RTDP_API_BIND:-127.0.0.1}"

# Required tooling.
for tool in docker curl; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "ERROR: '${tool}' is required but not installed." >&2
    exit 1
  fi
done

# --- build the command prefixes as arrays (no eval, no string re-parsing) ---
compose=(docker compose -f "${compose_file}")

# Wrap compose with `doppler run --` only when Doppler is available and not explicitly disabled.
runner=()
if [ "${RTDP_DEPLOY_NO_DOPPLER:-0}" != "1" ] && command -v doppler >/dev/null 2>&1; then
  runner=(doppler run --)
  log "Injecting runtime secrets via 'doppler run --'."
else
  log "Not using Doppler (unavailable or disabled); relying on ambient env / local .env."
fi

log "Profiles: ${COMPOSE_PROFILES}"
log "Pulling images..."
"${runner[@]}" "${compose[@]}" pull

log "Starting/updating the stack..."
"${runner[@]}" "${compose[@]}" up -d

# --- post-deploy health check (plain HTTP against the local API; no external dependency) ---
health_url="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/health}"
retries="${DEPLOY_HEALTH_RETRIES:-60}"
interval="${DEPLOY_HEALTH_INTERVAL:-2}"
log "Health check: ${health_url} (up to ${retries} attempts, ${interval}s apart)"

attempt=1
while [ "${attempt}" -le "${retries}" ]; do
  if curl -fs -o /dev/null "${health_url}"; then
    log "OK: /health passed on attempt ${attempt}."
    exit 0
  fi
  sleep "${interval}"
  attempt=$((attempt + 1))
done

echo "ERROR: /health did not pass after ${retries} attempts: ${health_url}" >&2
# Surface recent API logs to aid diagnosis (no secrets are logged by the app at INFO).
"${compose[@]}" logs --tail 50 api >&2 || true
exit 1
