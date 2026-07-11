#!/usr/bin/env bash
#
# Session-1 P0 — deploy-mode logic tests for deploy/host_deploy.sh.
#
# Docker-free and network-free: BOTH `docker` and `doppler` (and `curl`) are mocked on PATH, so this
# exercises the production doppler-wrapped deploy path WITHOUT a real bypass. It proves:
#   * normal mode keeps `docker compose pull` + `up -d` (tag defaults, behavior unchanged);
#   * local-pinned is deterministic DIGEST-ONLY, NO pull, NO build (`up -d --pull never --no-build`);
#   * local-pinned FAILS CLOSED on a tag ref, a malformed digest, an empty image var, an active image
#     absent locally, an active image that is a tag, and an unexpected active service (incl. `stream`);
#   * local-pinned FAILS CLOSED (before any image inspection or `up`) when a Compose `stream`
#     (ingestion writer) container is currently RUNNING — queried read-only by the canonical service
#     label; an EXITED `stream` does not block; normal mode never runs this query;
#   * all five digest refs reach Compose unchanged;
#   * an unknown RTDP_DEPLOY_MODE is rejected.
#
# No real secrets, credentials, or host paths — all image refs are generic placeholder digests.
#
# Run: bash deploy/tests/test_deploy_modes.sh   (wired into the CI `docker-smoke` job)

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
host_deploy="${repo_root}/deploy/host_deploy.sh"
[ -f "${host_deploy}" ] || { echo "FATAL: ${host_deploy} not found" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mockbin="${work}/bin"
mkdir -p "${mockbin}"

# --- generic placeholder digest fixtures (NOT real; 64-hex satisfies repo@sha256:<64hex>) ---
HEX="abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"   # 64 lowercase hex chars
HEXUP="${HEX^^}"                                                         # same digits, uppercase A-F
API_D="ghcr.io/example/realtime-agentic-data-platform@sha256:${HEX}"
MINIO_D="minio/minio@sha256:${HEX}"
MC_D="minio/mc@sha256:${HEX}"
CADDY_D="caddy@sha256:${HEX}"
DD_D="gcr.io/datadoghq/agent@sha256:${HEX}"

# --- fake docker: logs argv + a normalized action token; controllable inspect/config output ---
cat > "${mockbin}/docker" <<'FAKE_DOCKER'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "${DOCKER_ARGV_LOG}"

# `docker image inspect --format '{{.Id}}' <img>`
if [ "${1:-}" = "image" ] && [ "${2:-}" = "inspect" ]; then
  img="${@: -1}"
  printf 'inspect %s\n' "${img}" >> "${DOCKER_ACTIONS_LOG}"
  for bad in ${MOCK_INSPECT_FAIL:-}; do
    if [ "${img}" = "${bad}" ]; then exit 1; fi
  done
  printf 'sha256:localid\n'
  exit 0
fi

# `docker ps --filter status=running --filter label=com.docker.compose.service=stream --format ...`
# Command fidelity: only emit the mocked running-stream rows when BOTH required filters are present
# (status=running AND the canonical stream service label) — an arbitrary `docker ps` returns nothing.
if [ "${1:-}" = "ps" ]; then
  has_running=0; has_stream_label=0
  for a in "$@"; do
    case "$a" in
      status=running)                            has_running=1 ;;
      label=com.docker.compose.service=stream)   has_stream_label=1 ;;
    esac
  done
  if [ "${has_running}" = "1" ] && [ "${has_stream_label}" = "1" ]; then
    printf 'ps status=running label=stream\n' >> "${DOCKER_ACTIONS_LOG}"
    if [ -n "${MOCK_PS_STREAM:-}" ]; then printf '%s\n' "${MOCK_PS_STREAM}"; fi
  else
    printf 'ps other\n' >> "${DOCKER_ACTIONS_LOG}"
  fi
  exit 0
fi

# `docker compose ...`
if [ "${1:-}" = "compose" ]; then
  shift
  sub=""; images=0; services=0; nobuild=0; pullnever=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -f) shift 2; continue ;;
      --images)   images=1; shift; continue ;;
      --services) services=1; shift; continue ;;
      --no-build) nobuild=1; shift; continue ;;
      --pull)     pullnever=1; shift 2; continue ;;
      -*)         shift; continue ;;
      *)          [ -z "${sub}" ] && sub="$1"; shift; continue ;;
    esac
  done

  if [ "${sub}" = "config" ] && [ "${images}" = "1" ]; then
    printf 'config-images\n' >> "${DOCKER_ACTIONS_LOG}"
    printf '%s\n' ${MOCK_CONFIG_IMAGES:-}
    exit 0
  fi
  if [ "${sub}" = "config" ] && [ "${services}" = "1" ]; then
    printf 'config-services\n' >> "${DOCKER_ACTIONS_LOG}"
    printf '%s\n' ${MOCK_CONFIG_SERVICES:-}
    exit 0
  fi
  if [ "${sub}" = "config" ]; then
    printf 'config\n' >> "${DOCKER_ACTIONS_LOG}"
    exit 0
  fi
  if [ "${sub}" = "up" ]; then
    printf 'up pullnever=%s nobuild=%s\n' "${pullnever}" "${nobuild}" >> "${DOCKER_ACTIONS_LOG}"
    printf 'RTDP_IMAGE=%s\nRTDP_MINIO_IMAGE=%s\nRTDP_MINIO_MC_IMAGE=%s\nRTDP_CADDY_IMAGE=%s\nRTDP_DATADOG_IMAGE=%s\n' \
      "${RTDP_IMAGE:-}" "${RTDP_MINIO_IMAGE:-}" "${RTDP_MINIO_MC_IMAGE:-}" "${RTDP_CADDY_IMAGE:-}" "${RTDP_DATADOG_IMAGE:-}" \
      >> "${DOCKER_ENV_LOG}"
    exit 0
  fi
  if [ "${sub}" = "pull" ];  then printf 'pull\n'  >> "${DOCKER_ACTIONS_LOG}"; exit 0; fi
  if [ "${sub}" = "build" ]; then printf 'build\n' >> "${DOCKER_ACTIONS_LOG}"; exit 0; fi
  exit 0   # logs / anything else: succeed quietly
fi
exit 0
FAKE_DOCKER

# --- fake doppler: `doppler run [flags] -- <cmd...>` execs <cmd...> (no bypass; mirrors production) ---
cat > "${mockbin}/doppler" <<'FAKE_DOPPLER'
#!/usr/bin/env bash
found=0; cmd=()
for a in "$@"; do
  if [ "${found}" = "1" ]; then cmd+=("$a"); continue; fi
  if [ "$a" = "--" ]; then found=1; fi
done
if [ "${found}" = "1" ] && [ "${#cmd[@]}" -gt 0 ]; then exec "${cmd[@]}"; fi
exit 0
FAKE_DOPPLER

# --- fake curl: the post-deploy liveness probe always passes (nothing is really listening) ---
cat > "${mockbin}/curl" <<'FAKE_CURL'
#!/usr/bin/env bash
exit 0
FAKE_CURL

chmod +x "${mockbin}/docker" "${mockbin}/doppler" "${mockbin}/curl"

# --- harness state ---
pass=0; fail=0
DOCKER_ARGV_LOG="${work}/argv.log"
DOCKER_ACTIONS_LOG="${work}/actions.log"
DOCKER_ENV_LOG="${work}/env.log"
export DOCKER_ARGV_LOG DOCKER_ACTIONS_LOG DOCKER_ENV_LOG

ok()   { printf 'PASS: %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf 'FAIL: %s\n' "$1"; fail=$((fail + 1)); }

# Run host_deploy.sh in a subshell with the mock PATH + a scenario env. Env pairs are given as
# NAME=VALUE args; RTDP_DEPLOY_EXPECTED_SHA is left unset so the checkout guard is skipped. Captures
# the exit code; truncates the per-run logs first.
run_deploy() {
  : > "${DOCKER_ARGV_LOG}"; : > "${DOCKER_ACTIONS_LOG}"; : > "${DOCKER_ENV_LOG}"
  local rc=0
  env -i \
    HOME="${HOME:-/root}" \
    PATH="${mockbin}:/usr/bin:/bin:/usr/local/bin" \
    DOCKER_ARGV_LOG="${DOCKER_ARGV_LOG}" \
    DOCKER_ACTIONS_LOG="${DOCKER_ACTIONS_LOG}" \
    DOCKER_ENV_LOG="${DOCKER_ENV_LOG}" \
    DEPLOY_HEALTH_RETRIES=1 \
    "$@" \
    bash "${host_deploy}" > "${work}/out.log" 2>&1 || rc=$?
  return "${rc}"
}

actions()   { cat "${DOCKER_ACTIONS_LOG}"; }
has_action(){ grep -qx "$1" "${DOCKER_ACTIONS_LOG}"; }

# The five image vars supplied to a valid local-pinned run.
pinned_vars=(
  RTDP_DEPLOY_MODE=local-pinned
  "RTDP_IMAGE=${API_D}"
  "RTDP_MINIO_IMAGE=${MINIO_D}"
  "RTDP_MINIO_MC_IMAGE=${MC_D}"
  "RTDP_CADDY_IMAGE=${CADDY_D}"
  "RTDP_DATADOG_IMAGE=${DD_D}"
)
all_services="api minio minio-init caddy datadog-agent"
all_images="${API_D} ${MINIO_D} ${MC_D} ${CADDY_D} ${DD_D}"

echo "=== deploy-mode tests ==="

# 1) normal mode: pull + up (no --pull never / --no-build), tag defaults untouched.
if run_deploy RTDP_DEPLOY_MODE=normal; then
  if has_action 'pull' && has_action 'up pullnever=0 nobuild=0' && ! grep -q 'pull never\|no-build' "${DOCKER_ARGV_LOG}"; then
    ok "normal mode pulls then 'up -d' with no --pull never/--no-build"
  else
    bad "normal mode action sequence wrong: $(actions | tr '\n' ',')"
  fi
else
  bad "normal mode exited nonzero ($?)"
fi

# 2) local-pinned happy path: no pull, no build, up --pull never --no-build; the config --images
#    multiset equals EXACTLY the five reviewed refs (each validated → one 'image OK' line); and the
#    five digests reach the up process. The exact-five image OK lines assert config --images
#    correspondence (not merely that the variables reached `up`).
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  err=""
  has_action 'up pullnever=1 nobuild=1' || err="${err} missing 'up --pull never --no-build';"
  ! has_action 'pull'  || err="${err} unexpected 'pull';"
  ! has_action 'build' || err="${err} unexpected 'build';"
  grep -qx "RTDP_IMAGE=${API_D}"          "${DOCKER_ENV_LOG}" || err="${err} api digest not propagated;"
  grep -qx "RTDP_MINIO_IMAGE=${MINIO_D}"  "${DOCKER_ENV_LOG}" || err="${err} minio digest not propagated;"
  grep -qx "RTDP_MINIO_MC_IMAGE=${MC_D}"  "${DOCKER_ENV_LOG}" || err="${err} mc digest not propagated;"
  grep -qx "RTDP_CADDY_IMAGE=${CADDY_D}"  "${DOCKER_ENV_LOG}" || err="${err} caddy digest not propagated;"
  grep -qx "RTDP_DATADOG_IMAGE=${DD_D}"   "${DOCKER_ENV_LOG}" || err="${err} datadog digest not propagated;"
  # Exact five-image correspondence: one 'image OK' line per supplied ref, and EXACTLY five total.
  for ref in "${API_D}" "${MINIO_D}" "${MC_D}" "${CADDY_D}" "${DD_D}"; do
    grep -qF "local-pinned image OK: ${ref} " "${work}/out.log" || err="${err} image ${ref} not validated from config --images;"
  done
  n_ok="$(grep -c 'local-pinned image OK:' "${work}/out.log" || true)"
  [ "${n_ok}" = "5" ] || err="${err} expected exactly 5 config --images validations, got ${n_ok};"
  if [ -z "${err}" ]; then ok "local-pinned: no pull/build, up --pull never --no-build, config --images == exactly the 5 refs, all 5 reach compose"
  else bad "local-pinned happy path:${err}"; fi
else
  bad "local-pinned happy path exited nonzero ($?); out: $(cat "${work}/out.log")"
fi

# 3) local-pinned fails when an image variable is a TAG.
if run_deploy "${pinned_vars[@]/RTDP_MINIO_IMAGE=${MINIO_D}/RTDP_MINIO_IMAGE=minio/minio:latest}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "local-pinned accepted a tag image variable (should fail)"
else
  ok "local-pinned rejects a tag image variable"
fi

# 4) local-pinned fails on a MALFORMED digest.
if run_deploy "${pinned_vars[@]/RTDP_CADDY_IMAGE=${CADDY_D}/RTDP_CADDY_IMAGE=caddy@sha256:abc}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "local-pinned accepted a malformed digest (should fail)"
else
  ok "local-pinned rejects a malformed digest"
fi

# 5) local-pinned fails when a required image variable is EMPTY.
if run_deploy "${pinned_vars[@]/RTDP_DATADOG_IMAGE=${DD_D}/RTDP_DATADOG_IMAGE=}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "local-pinned accepted an empty image variable (should fail)"
else
  ok "local-pinned rejects an empty image variable"
fi

# 6) local-pinned fails when an active image is ABSENT locally (inspect fails for it).
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" \
      MOCK_CONFIG_IMAGES="${all_images}" MOCK_INSPECT_FAIL="${CADDY_D}"; then
  bad "local-pinned accepted an image absent locally (should fail)"
else
  ok "local-pinned rejects an active image absent locally"
fi

# 7) local-pinned fails when an active IMAGE is a tag (even though all five vars are digests).
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" \
      MOCK_CONFIG_IMAGES="${API_D} ${MINIO_D} ${MC_D} caddy:2 ${DD_D}"; then
  bad "local-pinned accepted a tag in the active image set (should fail)"
else
  ok "local-pinned rejects a tag in the active image set"
fi

# 8) local-pinned fails when an UNEXPECTED service (stream) is active.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services} stream" \
      MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "local-pinned accepted 'stream' in the active service set (should fail)"
else
  ok "local-pinned rejects an unexpected active service (stream)"
fi

# 9) unknown mode is rejected.
if run_deploy RTDP_DEPLOY_MODE=bogus; then
  bad "unknown RTDP_DEPLOY_MODE was accepted (should fail)"
else
  ok "unknown RTDP_DEPLOY_MODE is rejected"
fi

echo "--- Defect A: strict digest-reference validation (image variables) ---"
# Each case replaces one image variable with a bad value (services/images otherwise valid), so only
# the step-1 variable digest check can fail — proving the anchored is_digest_ref rejection.

# 10) empty repository prefix: @sha256:<hex>.
if run_deploy "${pinned_vars[@]/RTDP_CADDY_IMAGE=${CADDY_D}/RTDP_CADDY_IMAGE=@sha256:${HEX}}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted an empty repository prefix (should fail)"
else ok "rejects an empty repository prefix (@sha256:<hex>)"; fi

# 11) whitespace in the repository: 'bad img@sha256:<hex>'.
if run_deploy "${pinned_vars[@]/RTDP_CADDY_IMAGE=${CADDY_D}/RTDP_CADDY_IMAGE=bad img@sha256:${HEX}}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted whitespace in the repository (should fail)"
else ok "rejects whitespace in the repository ('bad img@sha256:<hex>')"; fi

# 12) an additional '@': repo@extra@sha256:<hex>.
if run_deploy "${pinned_vars[@]/RTDP_CADDY_IMAGE=${CADDY_D}/RTDP_CADDY_IMAGE=caddy@extra@sha256:${HEX}}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted an additional '@' (should fail)"
else ok "rejects an additional '@' (repo@extra@sha256:<hex>)"; fi

# 13) uppercase digest characters.
if run_deploy "${pinned_vars[@]/RTDP_CADDY_IMAGE=${CADDY_D}/RTDP_CADDY_IMAGE=caddy@sha256:${HEXUP}}" \
      MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted an uppercase digest (should fail)"
else ok "rejects an uppercase digest (repo@sha256:<UPPER>)"; fi

echo "--- Defect B: exact active-service multiset ---"

# 14) a missing required service.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="api caddy datadog-agent minio" \
      MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted a missing required service (should fail)"
else ok "rejects a missing required service (minio-init absent)"; fi

# 15) an unexpected (non-stream) service.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services} sidecar" \
      MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted an unexpected service (should fail)"
else ok "rejects an unexpected service (sidecar)"; fi

# 16) a duplicated service.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="api ${all_services}" \
      MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted a duplicated service (should fail)"
else ok "rejects a duplicated service (api twice)"; fi

# 17) an empty service result.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="" \
      MOCK_CONFIG_IMAGES="${all_images}"; then
  bad "accepted an empty service result (should fail)"
else ok "rejects an empty service result"; fi

echo "--- Defect C: exact effective-image multiset ---"

# 18) a missing expected image.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" \
      MOCK_CONFIG_IMAGES="${API_D} ${MINIO_D} ${MC_D} ${CADDY_D}"; then
  bad "accepted a missing expected image (should fail)"
else ok "rejects a missing expected image (datadog absent)"; fi

# 19) an unexpected image.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" \
      MOCK_CONFIG_IMAGES="${all_images} busybox@sha256:${HEX}"; then
  bad "accepted an unexpected image (should fail)"
else ok "rejects an unexpected image (extra digest ref)"; fi

# 20) a duplicated image replacing another required image (datadog -> a second caddy).
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" \
      MOCK_CONFIG_IMAGES="${API_D} ${MINIO_D} ${MC_D} ${CADDY_D} ${CADDY_D}"; then
  bad "accepted a duplicated image replacing a required image (should fail)"
else ok "rejects a duplicated image replacing a required image"; fi

# 21) an empty image result.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" \
      MOCK_CONFIG_IMAGES=""; then
  bad "accepted an empty image result (should fail)"
else ok "rejects an empty image result"; fi

echo "--- Codex P2: running ingestion-writer (stream) runtime guard ---"

# 22) a RUNNING Compose `stream` container blocks local-pinned. The read-only guard fires BEFORE image
#     inspection and before `up`, identifies the running container (id + name), and never pulls/builds.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}" \
      MOCK_PS_STREAM=$'container-id-1\tdeploy\tdeploy-stream-1'; then
  bad "local-pinned deployed while a 'stream' container was running (should fail)"
else
  err=""
  grep -q 'currently RUNNING' "${work}/out.log"  || err="${err} no running-writer error;"
  grep -q 'container-id-1'     "${work}/out.log"  || err="${err} container id not reported;"
  grep -q 'deploy-stream-1'    "${work}/out.log"  || err="${err} container name not reported;"
  has_action 'ps status=running label=stream'     || err="${err} runtime query lacked required filters;"
  ! grep -q '^inspect ' "${DOCKER_ACTIONS_LOG}"    || err="${err} image inspect ran after detection;"
  ! grep -q '^up '      "${DOCKER_ACTIONS_LOG}"    || err="${err} compose up ran after detection;"
  ! has_action 'pull'                             || err="${err} pull ran after detection;"
  ! has_action 'build'                            || err="${err} build ran after detection;"
  if [ -z "${err}" ]; then ok "local-pinned rejects a RUNNING stream writer before inspection/up (no pull/build)"
  else bad "running-stream guard:${err}"; fi
fi

# 23) two RUNNING stream containers block, and the complete captured set is reported before failing.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}" \
      MOCK_PS_STREAM=$'container-id-1\tdeploy\tdeploy-stream-1\ncontainer-id-2\tdeploy\tdeploy-stream-2'; then
  bad "local-pinned deployed while two 'stream' containers were running (should fail)"
else
  err=""
  grep -q 'deploy-stream-1' "${work}/out.log"  || err="${err} first container not reported;"
  grep -q 'deploy-stream-2' "${work}/out.log"  || err="${err} second container not reported;"
  ! grep -q '^up ' "${DOCKER_ACTIONS_LOG}"      || err="${err} compose up ran after detection;"
  if [ -z "${err}" ]; then ok "local-pinned reports and rejects multiple RUNNING stream writers"
  else bad "multiple-stream guard:${err}"; fi
fi

# 24) an EXITED stream is permitted: the running-only query returns nothing (MOCK_PS_STREAM empty), so
#     local-pinned proceeds to `up --pull never --no-build`. Also asserts the runtime query actually
#     carried BOTH required filters (fidelity) — not that any arbitrary `docker ps` returns the mock.
if run_deploy "${pinned_vars[@]}" MOCK_CONFIG_SERVICES="${all_services}" MOCK_CONFIG_IMAGES="${all_images}" \
      MOCK_PS_STREAM=""; then
  err=""
  has_action 'ps status=running label=stream' || err="${err} runtime query lacked required filters;"
  has_action 'up pullnever=1 nobuild=1'        || err="${err} did not reach up --pull never --no-build;"
  if [ -z "${err}" ]; then ok "local-pinned permits an EXITED stream (empty running query) and proceeds to up"
  else bad "exited-stream happy path:${err}"; fi
else
  bad "local-pinned failed with no running stream (should proceed); out: $(cat "${work}/out.log")"
fi

# 25) normal mode is unchanged: it never runs the runtime stream query, even with a stream "running",
#     and still pulls then `up -d`.
if run_deploy RTDP_DEPLOY_MODE=normal MOCK_PS_STREAM=$'container-id-9\tdeploy\tdeploy-stream-9'; then
  err=""
  has_action 'pull'                     || err="${err} normal mode did not pull;"
  has_action 'up pullnever=0 nobuild=0' || err="${err} normal mode did not 'up -d';"
  ! grep -q '^ps ' "${DOCKER_ACTIONS_LOG}" || err="${err} normal mode ran the runtime stream query;"
  if [ -z "${err}" ]; then ok "normal mode skips the runtime stream query and still pulls + 'up -d'"
  else bad "normal-mode stream-guard isolation:${err}"; fi
else
  bad "normal mode exited nonzero with MOCK_PS_STREAM set ($?)"
fi

echo "=== ${pass} passed, ${fail} failed ==="
[ "${fail}" -eq 0 ]
