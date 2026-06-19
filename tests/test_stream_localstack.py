from __future__ import annotations

import pytest

from rtdp import query
from rtdp.config import Settings
from rtdp.sources.synthetic import ContinuousSyntheticSource
from rtdp.stream import run_stream


@pytest.mark.localstack
def test_stream_appends_micro_batches_to_localstack_s3():
    """Stage 2B micro-batch loop against LocalStack S3 (default backend). Run via the CI
    integration job or locally with `docker compose up -d` first.

    Assertions are robust to a pre-existing/shared bronze table: we check the per-batch
    snapshot_count *delta* and the latest-state for one of this run's stable fleet ids,
    rather than absolute totals."""
    settings = Settings(_env_file=None)  # localstack defaults
    assert settings.storage_backend.value == "localstack"

    src = ContinuousSyntheticSource(fleet_size=3, seed=5, dup_count=1)
    results = run_stream(
        settings, src, interval_seconds=0, max_batches=3, sleep=lambda _s: None
    )

    assert len(results) == 3
    assert all(r.dq.ok for r in results)
    assert all(r.rows_written == 3 for r in results)  # 3 fleet rows after within-batch dedup
    counts = [r.snapshot_count for r in results]
    assert counts[-1] - counts[0] == 2  # three successive appends -> +1 snapshot each

    table = query.load_bronze_table(settings)
    latest = query.query_latest_state(table, icao24="000001", limit=10)
    assert latest.count == 1
    assert latest.items[0]["icao24"] == "000001"
