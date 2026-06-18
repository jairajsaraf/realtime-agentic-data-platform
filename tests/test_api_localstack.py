"""API over the primary LocalStack S3 backend. Run via the CI integration job or
locally with `docker compose up -d` first (see RUNBOOK)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rtdp.api import create_app
from rtdp.config import Settings
from rtdp.ingest import run_ingest
from rtdp.sources.synthetic import SyntheticSource


@pytest.mark.localstack
def test_api_reads_over_localstack_s3():
    settings = Settings(_env_file=None)  # localstack defaults
    assert settings.storage_backend.value == "localstack"

    # Seed via the Stage 1 ingestion path (not through the API — the API is read-only).
    res = run_ingest(settings, SyntheticSource(n_rows=10, seed=7, inject_warnings=False))
    assert res.snapshot_id is not None

    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        flights = client.get("/flights", params={"limit": 5})
        assert flights.status_code == 200
        body = flights.json()
        assert body["count"] <= 5
        assert body["snapshot_id"] is not None
