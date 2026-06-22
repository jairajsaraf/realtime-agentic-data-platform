#!/usr/bin/env bash
#
# Stage E container smoke test: build the image, seed a table on the file:// backend, start
# the read API, and assert GET /health returns 200. No secrets, no MinIO, no Datadog, no
# external LLM. Mirrors the env-driven file:// pattern in tests/test_cli.py and the /health
# check in tests/test_api.py. Used locally and by the CI build-image job (Stage E phase E4).
#
# Usage:  bash scripts/docker_smoke.sh
# Env:    IMAGE (default rtdp:smoke), PORT (default 8000), SKIP_BUILD=1 (smoke a prebuilt image)

set -euo pipefail

IMAGE="${IMAGE:-rtdp:smoke}"
PORT="${PORT:-8000}"
CONTAINER="rtdp-smoke-$$"
DATA_VOL="rtdp-smoke-data-$$"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$DATA_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo ">> SKIP_BUILD=1 — smoking prebuilt image: $IMAGE"
else
  echo ">> building image: $IMAGE"
  docker build -t "$IMAGE" .
fi

echo ">> seeding a table on the file:// backend (so /health can report ok)"
docker run --rm -v "$DATA_VOL":/data \
  -e RTDP_STORAGE_BACKEND=file \
  -e RTDP_LOCAL_WAREHOUSE_DIR=/data/warehouse \
  -e RTDP_CATALOG_DB_PATH=/data/warehouse/catalog.db \
  "$IMAGE" ingest --rows 50

echo ">> starting the read API"
docker run -d --name "$CONTAINER" -v "$DATA_VOL":/data \
  -e RTDP_STORAGE_BACKEND=file \
  -e RTDP_LOCAL_WAREHOUSE_DIR=/data/warehouse \
  -e RTDP_CATALOG_DB_PATH=/data/warehouse/catalog.db \
  -e RTDP_API_HOST=0.0.0.0 -e RTDP_API_PORT="$PORT" \
  -p "$PORT":"$PORT" "$IMAGE" serve

echo ">> waiting for GET /health == 200"
code=""
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
  if [ "$code" = "200" ]; then
    echo "OK: /health returned 200"
    curl -s "http://127.0.0.1:$PORT/health"; echo
    exit 0
  fi
  sleep 2
done

echo "FAIL: /health did not return 200 in time (last code: ${code:-none})" >&2
docker logs "$CONTAINER" || true
exit 1
