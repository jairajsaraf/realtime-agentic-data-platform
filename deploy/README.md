# Deploy assets — single-host topology

Local, additive deploy assets for running the existing `rtdp` surfaces from one Docker image on
a single host. **Nothing here provisions cloud resources or contains real secrets.**

- `Dockerfile` (repo root) — one image; entrypoint is the `rtdp` CLI.
- `docker-compose.yml` — `api` (read API) + `stream` (micro-batch writer), a one-shot `maintain`
  profile, and an `s3` profile (MinIO object storage).
- `.env.example` — placeholder environment; copy to a local, gitignored `.env` or inject via a
  secrets manager. Never commit a real `.env`.

## File:// backend (default — runnable with no secrets)

```
docker compose -f deploy/docker-compose.yml up -d --build
```

`api` and `stream` share one volume; the SQLite catalog and warehouse live on it. The API is on
`http://localhost:8000` (`/docs`, `/health`); `/health` returns 503 until the first stream batch
creates the table, then 200.

## S3-compatible backend (self-hosted MinIO via the `aws` backend)

The `rtdp` `aws` backend honors `RTDP_S3_ENDPOINT_URL` when it is explicitly set, so it can target
MinIO without any LocalStack involvement (real AWS remains the default when no endpoint is set).

1. Provide config/secrets (see `.env.example`). Set `RTDP_STORAGE_BACKEND=aws`, point
   `RTDP_S3_ENDPOINT_URL` at the MinIO service, and set `RTDP_AWS_ACCESS_KEY_ID` /
   `RTDP_AWS_SECRET_ACCESS_KEY` equal to the MinIO root credentials.
2. Bring up the `s3` profile (MinIO + a one-shot `minio-init` that creates the bucket):

   ```
   docker compose -f deploy/docker-compose.yml --profile s3 up -d --build
   ```

The MinIO **console is bound to `127.0.0.1:9001` (private/local-only)**; the S3 API (`:9000`) is
internal to the compose network only. The catalog stays local SQLite on the shared volume in all
backends (single-writer; keep one `stream` replica and schedule `maintain` not to overlap it).

## Secrets (Doppler or host env)

All runtime config flows through the existing `RTDP_*` settings. Two equivalent ways to supply it:

- **Doppler (preferred for a deployed host):** `doppler run -- docker compose -f deploy/docker-compose.yml --profile s3 up -d`
- **Host env / local `.env`:** `cp deploy/.env.example .env` (kept gitignored), fill in values, then run compose.

No keys are stored in the repo; CI never receives app runtime secrets.

## Snapshot maintenance

```
docker compose -f deploy/docker-compose.yml run --rm maintain
```

One-shot, metadata-only snapshot expiration. On a host, schedule via cron/systemd-timer so it does
not overlap a `stream` commit.

> Note: actual provisioning (host, MinIO volumes, secrets manager, observability backend) is a
> later, separately-gated step. These files only describe how to run the stack locally / on a host.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) validates the image and, on `main`, publishes it.
Image validation is kept separate from publishing, and publishing separate from deployment.

- **`docker-smoke`** (every push + PR) — builds the image and runs `scripts/docker_smoke.sh`
  (file:// synthetic seed → `rtdp serve` → `/health` 200) plus a `docker compose config`
  validation. Permissions: `contents: read` only. **Never logs in to or pushes to GHCR; needs no
  secrets** — so pull-request validation requires no cloud credentials.
- **`publish-image`** (push to `main` only; needs lint/unit + LocalStack integration + smoke) —
  builds and pushes `ghcr.io/<owner>/<repo>:<sha>` and `:latest` using the built-in
  `GITHUB_TOKEN` (`packages: write`). No external/cloud secret.
- **`deploy`** (push to `main` only; needs all of the above; `environment: production`) — a
  **no-op placeholder** today; it performs no real deployment.

### Required GitHub settings (manual; not automated here)
- Create a **`production` Environment** with **required reviewers** (optionally restrict
  deployment branches to `main`) before any real deploy step is added.
- GHCR packages are **private by default**; make the package public only if the demo image
  should be anonymously pullable.
- No repository secrets are required beyond the existing `CODECOV_TOKEN`.

### Enabling a real deploy (separately approved)
The real mechanism (e.g. SSH to the host + `docker compose pull && docker compose up -d`) is
intentionally **not** wired. To enable it later: configure the `production` environment, add
`DEPLOY_SSH_HOST`, `DEPLOY_SSH_USER`, `DEPLOY_SSH_KEY`, and `DEPLOY_PATH` as environment secrets,
and replace the placeholder step in the `deploy` job. Until then, deployment stays manual/gated.
