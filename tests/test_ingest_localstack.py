from __future__ import annotations

import pytest

from rtdp.config import Settings
from rtdp.ingest import run_ingest
from rtdp.sources.synthetic import SyntheticSource


@pytest.mark.localstack
def test_ingest_to_localstack_s3():
    """End-to-end ingest against LocalStack S3 (default backend). Run via the CI
    integration job or locally with `docker compose up -d` first."""
    settings = Settings(_env_file=None)  # localstack defaults
    assert settings.storage_backend.value == "localstack"

    res = run_ingest(settings, SyntheticSource(n_rows=10, seed=7, inject_warnings=False))
    assert res.dq.ok is True
    assert res.rows_written == 10
    assert res.snapshot_id is not None
    assert res.snapshot_count >= 1
