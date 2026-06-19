"""Unit tests for snapshot-expiration maintenance (file:// backend, no Docker)."""

from __future__ import annotations

import time

import pytest

from rtdp import query
from rtdp.ingest import run_ingest
from rtdp.maintenance import expire_snapshots
from rtdp.sources.synthetic import ContinuousSyntheticSource


def _ingest_n(settings, n: int) -> None:
    src = ContinuousSyntheticSource(fleet_size=2, seed=1, dup_count=0)
    for _ in range(n):
        run_ingest(settings, src)
        time.sleep(0.01)  # distinct commit timestamps (mirrors the two_snapshots idiom)


def test_expire_snapshots_retains_last_n_and_keeps_current(file_settings):
    _ingest_n(file_settings, 5)
    table = query.load_bronze_table(file_settings)
    assert len(table.metadata.snapshots) == 5
    current_id = table.current_snapshot().snapshot_id

    expired = expire_snapshots(table, retain_last=2)
    assert len(expired) == 3

    after = query.load_bronze_table(file_settings)  # reload to see committed metadata
    assert len(after.metadata.snapshots) == 2
    assert after.current_snapshot().snapshot_id == current_id  # current never expired
    # Table is still readable at the current snapshot.
    assert query.query_flights(after, limit=10).snapshot_id == current_id


def test_expire_snapshots_noop_when_within_retain(file_settings):
    _ingest_n(file_settings, 2)
    table = query.load_bronze_table(file_settings)
    assert expire_snapshots(table, retain_last=5) == []
    assert len(query.load_bronze_table(file_settings).metadata.snapshots) == 2


def test_expire_snapshots_validates_retain_last(file_settings):
    _ingest_n(file_settings, 1)
    table = query.load_bronze_table(file_settings)
    with pytest.raises(ValueError):
        expire_snapshots(table, retain_last=0)
