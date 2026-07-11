#!/usr/bin/env bash
#
# Stage E6 — host-side deploy action (the sole remote command run by the gated CI/CD deploy job).
#
# Pulls the published image and (re)starts the single-host compose stack, then verifies liveness.
# Run ON the prepared host, from a checkout/copy of this repo at the deploy path. The CI deploy job
# invokes exactly: `bash "$DEPLOY_SSH_PATH/deploy/host_deploy.sh"`. This script does NOT fetch code
# (no git pull) — the deploy path is prepared/updated out-of-band — and prints no secrets.
#
# Runtime secrets (RTDP_*) are injected by Doppler when available (`doppler run -- docker compose`).
# The liveness probe depends only on the API: never on Doppler, Datadog, NVIDIA, or the internet.
#
# Env (all optional):
#   COMPOSE_PROFILES        compose profiles to bring up   (default: s3,edge,observability)
#   DEPLOY_HEALTH_URL       URL to poll for liveness        (default: http://127.0.0.1:8000/livez)
#   DEPLOY_HEALTH_RETRIES   health-check attempts           (default: 60)
#   DEPLOY_HEALTH_INTERVAL  seconds between attempts        (default: 2)
#   RTDP_DEPLOY_NO_DOPPLER  set to 1 to skip the doppler wrapper even if doppler is installed
#   RTDP_DEPLOY_MODE        deploy mode (default: normal). Allow-listed:
#                             normal        registry pull then `up -d` (today's behavior, unchanged).
#                             local-pinned  deterministic, DIGEST-ONLY, NO pull, NO build. Every one of
#                                           the five image refs below (and every effective active image)
#                                           must be a digest ref repo@sha256:<64hex> already present
#                                           locally, else it fails closed before touching the stack. It
#                                           also fails closed if a Compose `stream` (ingestion writer)
#                                           container is currently RUNNING (read-only check; never
#                                           stopped/removed).
#                           Any other value is rejected.
#   RTDP_IMAGE / RTDP_MINIO_IMAGE / RTDP_MINIO_MC_IMAGE / RTDP_CADDY_IMAGE / RTDP_DATADOG_IMAGE
#                           the five service image refs (NON-SECRET). Tags in normal mode; exact digest
#                           refs required in local-pinned. Injected explicitly (see below), not via Doppler.

set -euo pipefail

log() { printf '>> %s\n' "$*"; }

# True only for a strict image DIGEST reference: a non-empty, whitespace-free, single-`@` repository
# followed by @sha256:<exactly 64 lowercase hex>. Anchored both ends, so it rejects an empty repo
# prefix, whitespace anywhere in the repo, an extra `@`, a tag, a short/long digest, uppercase hex,
# and any trailing characters. Used for both the five image variables and every active Compose image.
is_digest_ref() { [[ "$1" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]; }

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

# --- checkout-alignment guard (verify-only, fail closed) ---
# This script deploys the compose file + mounted config (observability/http_check.yaml, Caddyfile) FROM
# THE HOST CHECKOUT, while the image is the CI-approved SHA-pinned ref. A stale checkout would run the
# approved image against stale orchestration/config with no signal. When the CI deploy passes the approved
# commit SHA (RTDP_DEPLOY_EXPECTED_SHA), refuse to proceed unless the host checkout matches it.
# Verify-only: this does NOT modify the checkout (updating it stays an out-of-band step) — it turns silent
# drift into an early, safe failure. No-op when the var is unset (manual/local runs), so it stays
# backward compatible.
expected_sha="${RTDP_DEPLOY_EXPECTED_SHA:-}"
if [ -n "${expected_sha}" ]; then
  if ! host_sha="$(git -C "${repo_root}" rev-parse HEAD 2>/dev/null)"; then
    echo "ERROR: RTDP_DEPLOY_EXPECTED_SHA is set but ${repo_root} is not a git checkout;" \
         "cannot verify deploy alignment." >&2
    exit 1
  fi
  if [ "${host_sha}" != "${expected_sha}" ]; then
    echo "ERROR: host checkout ${host_sha} does not match the approved commit ${expected_sha}." >&2
    echo "       Update ${repo_root} to the approved commit out-of-band (git fetch + checkout)," \
         "then re-run the deploy." >&2
    exit 1
  fi
  # Reject a dirty worktree (Codex PR #10 P2) — a matching HEAD with local edits would still deploy
  # unapproved config. Fail closed if the status command itself fails; do not print the file list
  # (avoid leaking filenames into logs). Mirrors the authoritative check in the CI deploy wrapper.
  if ! dirty_status="$(git -C "${repo_root}" status --porcelain)"; then
    echo "ERROR: failed to inspect host checkout worktree state." >&2
    exit 1
  fi
  if [ -n "${dirty_status}" ]; then
    echo "ERROR: host checkout ${repo_root} has uncommitted or untracked changes; refusing to deploy." >&2
    exit 1
  fi
  log "Checkout alignment OK: HEAD ${host_sha} == approved ${expected_sha}."
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
# Preserve the ambient, CI-supplied RTDP_IMAGE (the approved SHA-pinned ref) so a same-named Doppler
# secret can't override it: --preserve-env is scoped to that ONE var, so all other RTDP_*/DD_* secrets
# still inject normally. RTDP_IMAGE is deployment-controlled and must NOT be stored as a Doppler secret.
runner=()
if [ "${RTDP_DEPLOY_NO_DOPPLER:-0}" != "1" ] && command -v doppler >/dev/null 2>&1; then
  runner=(doppler run --preserve-env="RTDP_IMAGE" --)
  log "Injecting runtime secrets via 'doppler run --' (preserving ambient RTDP_IMAGE)."
else
  log "Not using Doppler (unavailable or disabled); relying on ambient env / local .env."
fi

log "Profiles: ${COMPOSE_PROFILES}"

# --- deploy mode (allow-listed; default = normal) ---
# normal:       today's behavior — `docker compose pull` then `up -d` (registry pull on mutable tags).
# local-pinned: deterministic, DIGEST-ONLY, NO pull, NO build. Every effective active image must be a
#               digest ref (repo@sha256:<64 lowercase hex>) that is ALREADY present locally; fail closed
#               otherwise. The five image refs are injected explicitly via `env` AFTER the doppler
#               wrapper (non-secret; not stored in Doppler), so image identity does not depend on Doppler.
# Any other value is rejected (fail closed).
deploy_mode="${RTDP_DEPLOY_MODE:-normal}"
case "${deploy_mode}" in
  normal)
    log "Deploy mode: normal (registry pull enabled)."
    log "Pulling images..."
    "${runner[@]}" "${compose[@]}" pull

    log "Starting/updating the stack..."
    "${runner[@]}" "${compose[@]}" up -d
    ;;
  local-pinned)
    log "==> DEPLOY MODE: LOCAL-PINNED (deterministic; DIGEST-ONLY; NO pull, NO build) <=="
    # Explicit, non-secret image refs injected AFTER the doppler wrapper (independent of Doppler). Use
    # `-` (not `:-`) so an unset var stays empty and FAILS the digest check below rather than silently
    # falling back to a tag default.
    img_env=(env
      "RTDP_IMAGE=${RTDP_IMAGE-}"
      "RTDP_MINIO_IMAGE=${RTDP_MINIO_IMAGE-}"
      "RTDP_MINIO_MC_IMAGE=${RTDP_MINIO_MC_IMAGE-}"
      "RTDP_CADDY_IMAGE=${RTDP_CADDY_IMAGE-}"
      "RTDP_DATADOG_IMAGE=${RTDP_DATADOG_IMAGE-}")

    # 1) Each of the five image variables must itself be a strict digest ref repo@sha256:<64 lc hex>.
    for v in RTDP_IMAGE RTDP_MINIO_IMAGE RTDP_MINIO_MC_IMAGE RTDP_CADDY_IMAGE RTDP_DATADOG_IMAGE; do
      val="${!v-}"
      if ! is_digest_ref "${val}"; then
        echo "ERROR: ${v} must be a digest ref 'repo@sha256:<64hex>'; got '${val:-<empty>}'." >&2
        exit 1
      fi
    done

    # 2) The effective active SERVICE multiset (for the selected profiles) must equal EXACTLY the five
    #    expected production services — a missing required service, an unexpected service (incl.
    #    `stream`), a duplicate, or an empty result all fail closed. Compare sorted line lists WITH
    #    duplicates preserved (no `sort -u`, which would hide a duplicate). LC_ALL=C for a stable order.
    expected_services_norm="$(printf '%s\n' api caddy datadog-agent minio minio-init | LC_ALL=C sort)"
    if ! active_services="$("${runner[@]}" "${img_env[@]}" "${compose[@]}" config --services)"; then
      echo "ERROR: 'docker compose config --services' failed while resolving the active service set." >&2
      exit 1
    fi
    active_services_norm="$(printf '%s\n' "${active_services}" | sed '/^[[:space:]]*$/d' | LC_ALL=C sort)"
    fail=0
    if [ "${active_services_norm}" != "${expected_services_norm}" ]; then
      echo "ERROR: local-pinned active service set must equal exactly the five expected services." >&2
      echo "       expected: $(printf '%s ' ${expected_services_norm})" >&2
      echo "       observed: $(printf '%s ' ${active_services_norm:-<empty>})" >&2
      fail=1
    fi

    # 3) Runtime writer guard (fail closed BEFORE any image inspection or `up`). Step 2 only proves the
    #    active Compose CONFIG excludes `stream`; it cannot see a `stream` container that was started
    #    earlier (explicitly, or via the `ingestion` profile) and is STILL RUNNING — `up` for the
    #    selected profiles does not touch such an out-of-profile container (no --remove-orphans). Query
    #    the Docker Engine directly by the canonical Compose service label, independent of the current
    #    profile selection and of `docker compose config`/`ps`. READ-ONLY: it never stops, removes, or
    #    modifies the container; it only refuses to deploy and tells the operator to stop ingestion
    #    first. status=running means an exited `stream` does NOT block. The host is dedicated to this
    #    project, so any RUNNING Compose `stream` service is an unsafe concurrent writer.
    if ! running_stream="$(docker ps --filter status=running \
        --filter label=com.docker.compose.service=stream \
        --format '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t{{.Names}}')"; then
      echo "ERROR: failed to query the Docker Engine for running 'stream' containers; refusing to deploy." >&2
      exit 1
    fi
    if [ -n "${running_stream//[[:space:]]/}" ]; then
      echo "ERROR: a Compose 'stream' (ingestion writer) container is currently RUNNING; refusing local-pinned deploy." >&2
      echo "       Running stream container(s) [id<TAB>project<TAB>name]:" >&2
      printf '%s\n' "${running_stream}" | sed 's/^/         /' >&2
      echo "       Stop ingestion separately before deploying; this guard will not stop it automatically." >&2
      exit 1
    fi
    log "Runtime writer guard OK: no running Compose 'stream' container."

    # 4) The effective active IMAGE multiset must equal EXACTLY the five supplied digest refs (sorted,
    #    duplicates preserved — no `sort -u`, so a duplicate-image render is caught). Capture first so a
    #    failed render or an empty active set fails closed (an empty set would otherwise skip the
    #    per-image loop below and pass silently).
    if ! active_images="$("${runner[@]}" "${img_env[@]}" "${compose[@]}" config --images)"; then
      echo "ERROR: 'docker compose config --images' failed while resolving the active image set." >&2
      exit 1
    fi
    if [ -z "${active_images//[[:space:]]/}" ]; then
      echo "ERROR: no active images resolved for profiles '${COMPOSE_PROFILES}'; refusing to deploy." >&2
      exit 1
    fi
    # Exact multiset correspondence to the five supplied refs (missing/unexpected/duplicate fails).
    expected_images_norm="$(printf '%s\n' "${RTDP_IMAGE-}" "${RTDP_MINIO_IMAGE-}" "${RTDP_MINIO_MC_IMAGE-}" \
      "${RTDP_CADDY_IMAGE-}" "${RTDP_DATADOG_IMAGE-}" | LC_ALL=C sort)"
    active_images_norm="$(printf '%s\n' "${active_images}" | sed '/^[[:space:]]*$/d' | LC_ALL=C sort)"
    if [ "${active_images_norm}" != "${expected_images_norm}" ]; then
      echo "ERROR: local-pinned active image set must correspond exactly to the five supplied digest refs." >&2
      echo "       expected: $(printf '%s ' ${expected_images_norm})" >&2
      echo "       observed: $(printf '%s ' ${active_images_norm})" >&2
      fail=1
    fi
    # 5) each active image is a strict digest ref AND present locally (no pull).
    while IFS= read -r img; do
      [ -n "${img}" ] || continue
      if ! is_digest_ref "${img}"; then
        echo "ERROR: active image is not a digest ref: ${img}" >&2
        fail=1
        continue
      fi
      if id="$(docker image inspect --format '{{.Id}}' "${img}" 2>/dev/null)"; then
        log "local-pinned image OK: ${img} (local id ${id})"
      else
        echo "ERROR: active image not present locally (no pull in local-pinned): ${img}" >&2
        fail=1
      fi
    done <<< "${active_images}"

    if [ "${fail}" -ne 0 ]; then
      echo "ERROR: local-pinned preconditions failed; refusing to deploy." >&2
      exit 1
    fi

    log "Starting/updating the stack (no pull, no build)..."
    "${runner[@]}" "${img_env[@]}" "${compose[@]}" up -d --pull never --no-build
    ;;
  *)
    echo "ERROR: unknown RTDP_DEPLOY_MODE='${deploy_mode}' (allowed: normal, local-pinned)." >&2
    exit 1
    ;;
esac

# --- post-deploy liveness check (plain HTTP against the local API; no external dependency) ---
# Probes /livez (liveness): 200 whenever the API process serves, independent of whether a table
# exists yet — so a fresh host (or recreated rtdp-state) deploys without starting ingestion. Data
# readiness stays on /health (Datadog's http_check). DEPLOY_HEALTH_URL keeps its name; only the
# default value changed.
health_url="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/livez}"
retries="${DEPLOY_HEALTH_RETRIES:-60}"
interval="${DEPLOY_HEALTH_INTERVAL:-2}"
log "Liveness check: ${health_url} (up to ${retries} attempts, ${interval}s apart)"

attempt=1
while [ "${attempt}" -le "${retries}" ]; do
  if curl -fs -o /dev/null "${health_url}"; then
    log "OK: liveness passed on attempt ${attempt}."
    exit 0
  fi
  sleep "${interval}"
  attempt=$((attempt + 1))
done

echo "ERROR: liveness did not pass after ${retries} attempts: ${health_url}" >&2
# Surface recent API logs to aid diagnosis (no secrets are logged by the app at INFO).
"${compose[@]}" logs --tail 50 api >&2 || true
exit 1
