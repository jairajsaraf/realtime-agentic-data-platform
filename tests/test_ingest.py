from __future__ import annotations

from rtdp.catalog import build_catalog
from rtdp.ingest import run_ingest
from rtdp.sources.synthetic import SyntheticSource


def _rows_in_table(settings) -> int:
    table = build_catalog(settings).load_table(settings.table_identifier)
    return table.scan().to_arrow().num_rows


def test_ingest_creates_table_and_snapshot(file_settings):
    res = run_ingest(file_settings, SyntheticSource(n_rows=20, seed=1, inject_warnings=False))
    assert res.dq.ok is True
    assert res.rows_written == 20
    assert res.snapshot_count == 1
    assert res.snapshot_id is not None
    assert _rows_in_table(file_settings) == 20


def test_ingest_twice_creates_two_snapshots(file_settings):
    run_ingest(file_settings, SyntheticSource(n_rows=10, seed=1, inject_warnings=False))
    res2 = run_ingest(file_settings, SyntheticSource(n_rows=15, seed=2, inject_warnings=False))
    assert res2.snapshot_count == 2
    assert _rows_in_table(file_settings) == 25


def test_ingest_dq_failure_aborts_without_writing(file_settings):
    run_ingest(file_settings, SyntheticSource(n_rows=10, seed=1, inject_warnings=False))
    res = run_ingest(
        file_settings,
        SyntheticSource(n_rows=5, seed=1, inject_warnings=False, inject_failures=True),
    )
    assert res.dq.ok is False
    assert res.rows_written == 0
    assert res.snapshot_count == 1  # unchanged — failing batch not committed
    assert _rows_in_table(file_settings) == 10


def test_ingest_with_warnings_still_writes(file_settings):
    res = run_ingest(file_settings, SyntheticSource(n_rows=10, seed=1, inject_warnings=True))
    assert res.dq.ok is True
    assert res.dq.warnings is not None
    assert res.rows_written > 10  # warn rows are written, not dropped
