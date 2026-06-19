# syntax=docker/dockerfile:1
#
# One image, many entrypoints. The `rtdp` console script exposes every runtime as a
# subcommand, so the same image serves the read API, runs the micro-batch stream loop,
# runs one-shot snapshot maintenance, or answers an on-demand agent question — selected
# by overriding the command (see deploy/docker-compose.yml).

# ---- builder: resolve + install dependencies and the project with uv ----
FROM python:3.12-slim AS builder

# uv pinned to the same version CI uses (.github/workflows/ci.yml).
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=3.12

WORKDIR /app

# Layer 1: dependencies only — cached unless pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: the project itself (src/ layout; README is referenced by pyproject metadata).
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

# ---- runtime: slim image carrying only the resolved venv + source ----
FROM python:3.12-slim AS runtime

# Run as a non-root user. Pre-create the /data mountpoint owned by that user so a fresh
# named volume mounted there inherits writable ownership (Docker seeds an empty named
# volume from the image path, ownership included) — the read API/stream/maintain write
# the SQLite catalog + warehouse under /data.
RUN useradd --create-home --uid 10001 rtdp \
    && mkdir -p /data \
    && chown rtdp:rtdp /data
WORKDIR /app

COPY --from=builder --chown=rtdp:rtdp /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    RTDP_API_HOST=0.0.0.0 \
    RTDP_API_PORT=8000

USER rtdp
EXPOSE 8000

# `rtdp stream` performs a clean shutdown only on SIGINT (KeyboardInterrupt); Docker/systemd
# send SIGTERM by default, so map the stop signal to SIGINT for a graceful loop exit.
STOPSIGNAL SIGINT

ENTRYPOINT ["rtdp"]
CMD ["serve"]
