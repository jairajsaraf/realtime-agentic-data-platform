"""Unit tests for the Stage 2B micro-batch path (file:// / in-memory, no Docker)."""

from __future__ import annotations

from rtdp import query
from rtdp.sources.base import PrefetchedSource, RawBatch
from rtdp.sources.synthetic import ContinuousSyntheticSource
from rtdp.stream import run_stream
from rtdp.transforms import dedupe_raw_records


def _no_sleep(_seconds) -> None:
    pass


# ----------------------------------------------------- continuous synthetic generator
def test_continuous_source_stable_fleet():
    src = ContinuousSyntheticSource(fleet_size=4, dup_count=0)
    fleet0 = {r["icao24"] for r in src.fetch().records}
    fleet1 = {r["icao24"] for r in src.fetch().records}
    assert fleet0 == fleet1
    assert len(fleet0) == 4


def test_continuous_source_event_time_advances_monotonically():
    src = ContinuousSyntheticSource(fleet_size=4, step_seconds=60, dup_count=1)
    b0 = src.fetch().records
    b1 = src.fetch().records
    assert min(r["time_position"] for r in b1) > max(r["time_position"] for r in b0)
    # last_contact tracks time_position, so the logical key advances too.
    assert min(r["last_contact"] for r in b1) > max(r["last_contact"] for r in b0)


def test_continuous_source_has_within_batch_duplicate():
    src = ContinuousSyntheticSource(fleet_size=4, dup_count=2)
    recs = src.fetch().records
    assert len(recs) == 6  # 4 fleet rows + 2 duplicates
    assert len(dedupe_raw_records(recs)) == 4  # duplicates collapse on (icao24, last_contact)


def test_continuous_source_is_deterministic():
    a = ContinuousSyntheticSource(fleet_size=3, seed=7)
    b = ContinuousSyntheticSource(fleet_size=3, seed=7)
    assert [a.fetch().records for _ in range(3)] == [b.fetch().records for _ in range(3)]


# --------------------------------------------------------------- PrefetchedSource
def test_prefetched_source_returns_batch_verbatim():
    batch = RawBatch(records=[{"icao24": "abc123", "last_contact": 1}], source_name="s")
    ps = PrefetchedSource(name="s", batch=batch)
    assert ps.fetch() is batch
    assert ps.name == "s"


# --------------------------------------------------------------- run_stream loop
class _EmptySource:
    name = "empty"

    def fetch(self) -> RawBatch:
        return RawBatch(records=[], source_name=self.name)


class _BoomSource:
    name = "boom"

    def fetch(self) -> RawBatch:
        raise RuntimeError("transient source error")


class _FlakySource:
    """Raises on the first fetch, then yields valid continuous batches."""

    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0
        self._inner = ContinuousSyntheticSource(fleet_size=2, dup_count=0)

    def fetch(self) -> RawBatch:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient")
        return self._inner.fetch()


def test_run_stream_appends_n_micro_batches(file_settings):
    src = ContinuousSyntheticSource(fleet_size=3, seed=1, dup_count=1)
    results = run_stream(file_settings, src, interval_seconds=0, max_batches=3, sleep=_no_sleep)
    assert len(results) == 3
    assert [r.snapshot_count for r in results] == [1, 2, 3]
    table = query.load_bronze_table(file_settings)
    assert query.query_latest_state(table, limit=100).count == 3  # one row per aircraft


def test_run_stream_skips_empty_batches_without_creating_snapshots(file_settings):
    skipped: list[int] = []
    results = run_stream(
        file_settings,
        _EmptySource(),
        interval_seconds=0,
        max_batches=3,
        sleep=_no_sleep,
        on_skip=lambda i: skipped.append(i),
    )
    assert results == []
    assert skipped == [0, 1, 2]
    assert query.health(file_settings).table_loadable is False  # table never created


def test_run_stream_survives_per_batch_errors(file_settings):
    errors: list[int] = []
    results = run_stream(
        file_settings,
        _BoomSource(),
        interval_seconds=0,
        max_batches=3,
        sleep=_no_sleep,
        on_error=lambda i, exc: errors.append(i),
    )
    assert results == []
    assert errors == [0, 1, 2]  # loop survived all three errors


def test_run_stream_recovers_after_transient_error(file_settings):
    errors: list[int] = []
    results = run_stream(
        file_settings,
        _FlakySource(),
        interval_seconds=0,
        max_batches=2,
        sleep=_no_sleep,
        on_error=lambda i, exc: errors.append(i),
    )
    assert len(errors) == 1  # first batch failed
    assert len(results) == 1  # second batch appended


def test_run_stream_keyboardinterrupt_returns_partial_results(file_settings):
    def _interrupt(_seconds):
        raise KeyboardInterrupt

    src = ContinuousSyntheticSource(fleet_size=2, dup_count=0)
    # batch 0 appends, then the first sleep raises KeyboardInterrupt -> graceful stop.
    results = run_stream(file_settings, src, interval_seconds=1, max_batches=5, sleep=_interrupt)
    assert len(results) == 1
