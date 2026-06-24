# Deploy assets — single-host topology

Local, additive deploy assets for running the existing `rtdp` surfaces from one Docker image on
a single host. **Nothing here provisions cloud resources or contains real secrets.**

- `Dockerfile` (repo root) — one image; entrypoint is the `rtdp` CLI.
- `docker-compose.yml` — `api` (read API) + `stream` (micro-batch writer), a one-shot `maintain`
  profile, an `s3` profile (MinIO object storage), an `edge` profile (Caddy reverse proxy + HTTPS),
  and an `observability` profile (Datadog Agent OTLP intake).
- `Caddyfile` — Caddy site config; reverse-proxies the public hostname to the `api` service.
- `bootstrap_host.sh` — idempotent host prep (Docker, deploy user, UFW). Run once on a new host.
- `host_deploy.sh` — the host-side deploy action (compose pull + up + `/health` check) the gated CI
  deploy job invokes over SSH.
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

## Edge / TLS (profile `edge`)

Caddy terminates TLS and reverse-proxies the public hostname to the `api` service:

```
RTDP_PUBLIC_HOSTNAME=demo.example.me docker compose -f deploy/docker-compose.yml --profile edge up -d
```

- **Local:** `RTDP_PUBLIC_HOSTNAME` defaults to `localhost`; Caddy uses its internal CA, so test with
  `curl -k https://localhost/health` (or hit the API directly on `http://localhost:8000/health`).
- **Public host:** set `RTDP_PUBLIC_HOSTNAME` to the real domain (DNS A-record → host, ports 80/443
  open) and Caddy provisions/renews a Let's Encrypt certificate automatically. The Namecheap bundled
  certificate is unused.
- On the public host, set `RTDP_API_BIND=127.0.0.1` so the API's port 8000 is **not** exposed to the
  internet (Docker publishes past UFW, so binding to localhost — not just a firewall rule — is what
  keeps it private). Caddy still reaches the API as `api:8000` on the compose network.

## Observability (profile `observability`)

The OpenTelemetry boundary (`src/rtdp/telemetry.py`) exports traces over **OTLP gRPC**. The
`observability` profile runs a **Datadog Agent** that receives them on `datadog-agent:4317` (internal
compose network only — the port is never published). To enable export on the host:

- run the **deployed image, which includes the optional `[otel]` extra** (CI's `publish-image` builds
  it with `--build-arg RTDP_INSTALL_EXTRAS=otel`);
- set `RTDP_OTEL_ENABLED=true` and `RTDP_OTEL_EXPORTER_OTLP_ENDPOINT=http://datadog-agent:4317`;
- supply `DD_API_KEY` (via Doppler) and `DD_SITE` for the Agent.

The Agent **requires `DD_API_KEY` to start**, so the `observability` profile is for the deployed host,
not the default/local path. Telemetry stays **no-op** unless both `RTDP_OTEL_ENABLED=true` and the
`[otel]` extra are present. **`/health` and CI never depend on Datadog**, and there is **no `/metrics`
endpoint**.

## Secrets (Doppler or host env)

All runtime config flows through the existing `RTDP_*` settings. Two equivalent ways to supply it:

- **Doppler (preferred for a deployed host):** `doppler run -- docker compose -f deploy/docker-compose.yml --profile s3 up -d`
- **Host env / local `.env`:** `cp deploy/.env.example .env` (kept gitignored), fill in values, then run compose.

**Secret boundary:**

- **GitHub `production` environment secrets** hold **only the SSH deploy credentials**
  (`DEPLOY_SSH_HOST`, `DEPLOY_SSH_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_SSH_PATH`) used by the deploy job.
- **Doppler** (on the host) injects the **app/runtime `RTDP_*`** secrets (plus `DD_API_KEY` and the
  MinIO/AWS credentials) into the containers via `doppler run -- docker compose ...`.

No keys are stored in the repo; CI never receives app runtime secrets.

## Snapshot maintenance

```
docker compose -f deploy/docker-compose.yml run --rm maintain
```

One-shot, metadata-only snapshot expiration. On a host, schedule via cron/systemd-timer so it does
not overlap a `stream` commit.

> Note: actual provisioning (droplet, MinIO volume, Doppler/Datadog accounts, DNS) is the separately
> gated **E6.2** step — see the "Stage E6 go-live" section of `RUNBOOK.md`. These files only prepare
> the repo; they provision nothing and contain no real secrets.

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`) validates the image and, on `main`, publishes it.
Image validation is kept separate from publishing, and publishing separate from deployment.

- **`docker-smoke`** (every push + PR) — builds the image and runs `scripts/docker_smoke.sh`
  (file:// synthetic seed → `rtdp serve` → `/health` 200) plus a `docker compose config`
  validation. Permissions: `contents: read` only. **Never logs in to or pushes to GHCR; needs no
  secrets** — so pull-request validation requires no cloud credentials.
- **`telemetry-otel`** (every push + PR) — installs the optional `[otel]` extra and runs the
  telemetry suite with coverage so the OpenTelemetry-enabled branches are measured. Permissions:
  `contents: read`; reuses the existing `CODECOV_TOKEN`; the tests never export spans.
- **`publish-image`** (push to `main` only; needs lint/unit + LocalStack integration + smoke) —
  builds and pushes `ghcr.io/<owner>/<repo>:<sha>` and `:latest` using the built-in
  `GITHUB_TOKEN` (`packages: write`). No external/cloud secret.
- **`deploy`** (push to `main` only; needs lint/unit + LocalStack + smoke + `publish-image`;
  `environment: production`) — a **real, gated SSH deploy**. Raw OpenSSH (no third-party action):
  writes `DEPLOY_SSH_KEY` to a 0600 temp file, pins the host key with `ssh-keyscan`, and runs exactly
  one remote command — `bash "$DEPLOY_SSH_PATH/deploy/host_deploy.sh"` (compose pull + up + `/health`
  check). No secrets are printed; it runs only after a human approves the `production` environment.

### Required GitHub settings (manual; not automated here)
- Create a **`production` Environment** with **required reviewers** and restrict deployment branches
  to `main`. The deploy job waits for a reviewer's approval before it runs.
- Add the `production` **environment secrets**: `DEPLOY_SSH_HOST`, `DEPLOY_SSH_USER`,
  `DEPLOY_SSH_KEY` (the private key), `DEPLOY_SSH_PATH` (the repo/deploy path on the host).
- **Recommended:** also add `DEPLOY_SSH_KNOWN_HOSTS` — the host's pinned `known_hosts` entry. When
  set, the deploy verifies the host key against it; otherwise it falls back to in-band `ssh-keyscan`
  (trust-on-first-use) with a warning. Capture/pin it out-of-band in E6.2.
- GHCR packages are **private by default**; make the package public only if the demo image should be
  anonymously pullable (otherwise ensure the host can authenticate to pull it).
- No other repository secrets are required beyond the existing `CODECOV_TOKEN`.

### Manual approval flow
The deploy job is wired but **gated**: on a push to `main` it queues against the `production`
environment and does nothing until a required reviewer approves it in the GitHub UI
(**Actions → the run → Review deployments → Approve**). On approval it SSHes to the host and runs
`host_deploy.sh`; the job **fails if the post-deploy `/health` check fails**. Nothing deploys from a
PR or a feature branch, and CI provisions no infrastructure.
