"""Unit tests for rtdp.query against the file:// backend (no Docker)."""

from __future__ import annotations

import time

import pytest
from pyiceberg.expressions import (
    AlwaysTrue,
    And,
    EqualTo,
    GreaterThanOrEqual,
    LessThanOrEqual,
)

from rtdp import query
from rtdp.ingest import run_ingest
from rtdp.sources.synthetic import SyntheticSource


def _seed(settings, n_rows, seed):
    return run_ingest(settings, SyntheticSource(n_rows=n_rows, seed=seed, inject_warnings=False))


@pytest.fixture
def seeded(file_settings):
    """One snapshot of 30 clean synthetic rows. Returns (settings, table)."""
    _seed(file_settings, 30, 1)
    return file_settings, query.load_bronze_table(file_settings)


# --------------------------------------------------------- predicate pushdown
# The point of these is to prove supported filters become pyiceberg row_filter
# expressions (pushed into the scan), not DuckDB-only post-filtering.
def test_build_row_filter_empty_is_always_true():
    assert isinstance(query.build_row_filter(), AlwaysTrue)


def test_build_row_filter_icao24_is_equality():
    assert query.build_row_filter(icao24="abc123") == EqualTo("icao24", "abc123")


def test_build_row_filter_callsign_is_equality():
    assert query.build_row_filter(callsign="DLH9LH") == EqualTo("callsign", "DLH9LH")


def test_build_row_filter_time_bounds_are_range_predicates(seeded):
    from datetime import UTC, datetime

    start = datetime(2026, 6, 14, tzinfo=UTC)
    end = datetime(2026, 6, 15, tzinfo=UTC)
    assert query.build_row_filter(start=start) == GreaterThanOrEqual("event_time", start)
    assert query.build_row_filter(end=end) == LessThanOrEqual("event_time", end)


def test_build_row_filter_combo_is_conjunction():
    from datetime import UTC, datetime

    expr = query.build_row_filter(
        icao24="abc123",
        start=datetime(2026, 6, 14, tzinfo=UTC),
        end=datetime(2026, 6, 15, tzinfo=UTC),
    )
    assert isinstance(expr, And)
    assert not isinstance(expr, AlwaysTrue)


# ------------------------------------------------------------------- filters
def test_query_flights_filter_icao24(seeded):
    settings, table = seeded
    allf = query.query_flights(table, limit=1000)
    target = allf.items[0]["icao24"]
    expected = sum(1 for i in allf.items if i["icao24"] == target)

    res = query.query_flights(table, icao24=target, limit=1000)
    assert res.count == expected
    assert all(i["icao24"] == target for i in res.items)


def test_query_flights_filter_callsign(seeded):
    settings, table = seeded
    allf = query.query_flights(table, limit=1000)
    target = allf.items[0]["callsign"]
    expected = sum(1 for i in allf.items if i["callsign"] == target)

    res = query.query_flights(table, callsign=target, limit=1000)
    assert res.count == expected
    assert all(i["callsign"] == target for i in res.items)


def test_query_flights_time_window(seeded):
    settings, table = seeded
    allf = query.query_flights(table, limit=1000)
    times = sorted(i["event_time"] for i in allf.items)
    split = times[len(times) // 2]
    expected = sum(1 for t in times if t >= split)

    res = query.query_flights(table, start=split, limit=1000)
    assert res.count == expected
    assert all(i["event_time"] >= split for i in res.items)
    assert res.count < allf.count  # the window genuinely narrowed the result


def test_query_flights_limit_offset_paging(seeded):
    settings, table = seeded
    page1 = query.query_flights(table, limit=10, offset=0)
    page2 = query.query_flights(table, limit=10, offset=10)
    assert page1.count == 10
    assert page2.count == 10
    ids1 = {(i["icao24"], i["event_time"]) for i in page1.items}
    ids2 = {(i["icao24"], i["event_time"]) for i in page2.items}
    assert ids1.isdisjoint(ids2)  # non-overlapping pages


# ---------------------------------------------------------------------- bbox
def test_query_bbox_wide_returns_all_positioned(seeded):
    settings, table = seeded
    allf = query.query_flights(table, limit=1000)
    positioned = sum(
        1 for i in allf.items if i["latitude"] is not None and i["longitude"] is not None
    )
    bb = query.query_bbox(table, min_lat=-90, max_lat=90, min_lon=-180, max_lon=180, limit=1000)
    assert bb.count == positioned


def test_query_bbox_narrow_is_subset(seeded):
    settings, table = seeded
    allf = query.query_flights(table, limit=1000)
    bb = query.query_bbox(table, min_lat=0, max_lat=10, min_lon=0, max_lon=10, limit=1000)
    assert bb.count <= allf.count
    for i in bb.items:
        assert 0 <= i["latitude"] <= 10
        assert 0 <= i["longitude"] <= 10


# ------------------------------------------------------------- aggregations
def test_query_stats_hour_and_day_sum_to_total(seeded):
    settings, table = seeded
    total = query.query_flights(table, limit=1000).count
    for interval in ("hour", "day"):
        res = query.query_stats_per_interval(table, interval=interval)
        assert res.buckets
        assert sum(b["count"] for b in res.buckets) == total
        assert all(b["bucket_start"].tzinfo is not None for b in res.buckets)


def test_query_stats_day_has_fewer_or_equal_buckets_than_hour(seeded):
    settings, table = seeded
    hourly = query.query_stats_per_interval(table, interval="hour")
    daily = query.query_stats_per_interval(table, interval="day")
    assert len(daily.buckets) <= len(hourly.buckets)


def test_query_stats_group_by_origin_country(seeded):
    settings, table = seeded
    total = query.query_flights(table, limit=1000).count
    res = query.query_stats_per_interval(table, interval="day", group_by="origin_country")
    assert res.buckets
    assert all(b["group"] is not None for b in res.buckets)
    assert sum(b["count"] for b in res.buckets) == total


def test_query_stats_rejects_bad_interval(seeded):
    settings, table = seeded
    with pytest.raises(ValueError):
        query.query_stats_per_interval(table, interval="week")


# ------------------------------------------------------------- time-travel
@pytest.fixture
def two_snapshots(file_settings):
    r1 = _seed(file_settings, 20, 1)
    time.sleep(0.05)  # ensure S2 commits at a strictly later ms
    r2 = _seed(file_settings, 15, 2)
    return file_settings, query.load_bronze_table(file_settings), r1, r2


def test_time_travel_by_snapshot_id(two_snapshots):
    settings, table, r1, r2 = two_snapshots
    at_s1 = query.query_flights(table, as_of_snapshot_id=r1.snapshot_id, limit=1000)
    current = query.query_flights(table, limit=1000)
    assert at_s1.count == 20
    assert at_s1.snapshot_id == r1.snapshot_id
    assert current.count == 35
    assert current.snapshot_id == r2.snapshot_id


def test_time_travel_by_timestamp(two_snapshots):
    settings, table, r1, r2 = two_snapshots
    snaps = query.list_snapshots(table)
    s1_ts = next(s.timestamp for s in snaps if s.snapshot_id == r1.snapshot_id)
    at_ts = query.query_flights(table, as_of_timestamp=s1_ts, limit=1000)
    assert at_ts.count == 20
    assert at_ts.snapshot_id == r1.snapshot_id


def test_resolve_snapshot_conflict_raises(two_snapshots):
    from datetime import UTC, datetime

    settings, table, r1, r2 = two_snapshots
    with pytest.raises(query.AsOfConflictError):
        query.resolve_snapshot_id(
            table, as_of_snapshot_id=r1.snapshot_id, as_of_timestamp=datetime.now(UTC)
        )


def test_resolve_snapshot_unknown_id_raises(two_snapshots):
    settings, table, r1, r2 = two_snapshots
    with pytest.raises(query.SnapshotNotFoundError):
        query.resolve_snapshot_id(table, as_of_snapshot_id=1)


def test_resolve_snapshot_before_history_raises(two_snapshots):
    from datetime import UTC, datetime

    settings, table, r1, r2 = two_snapshots
    with pytest.raises(query.SnapshotNotFoundError):
        query.resolve_snapshot_id(table, as_of_timestamp=datetime(2000, 1, 1, tzinfo=UTC))


# ------------------------------------------------------------- metadata/health
def test_list_snapshots(two_snapshots):
    settings, table, r1, r2 = two_snapshots
    snaps = query.list_snapshots(table)
    assert len(snaps) == 2
    assert {s.snapshot_id for s in snaps} == {r1.snapshot_id, r2.snapshot_id}
    assert all(s.operation == "append" for s in snaps)
    assert all(s.timestamp.tzinfo is not None for s in snaps)


def test_table_meta(seeded):
    settings, table = seeded
    m = query.table_meta(table)
    assert m.table_identifier == settings.table_identifier
    assert len(m.schema) == 21
    assert {f["name"] for f in m.schema} >= {"icao24", "event_time", "origin_country"}
    assert m.partition_spec == [
        {"name": "event_day", "transform": "day", "source_id": 4, "source_column": "event_time"}
    ]
    assert m.snapshot_count == 1
    assert m.current_snapshot_id is not None


def test_health_ok(seeded):
    settings, table = seeded
    h = query.health(settings)
    assert h.status == "ok"
    assert h.catalog_reachable is True
    assert h.table_loadable is True
    assert h.current_snapshot_id is not None
    assert h.error is None


def test_health_unavailable_when_table_missing(file_settings):
    # Catalog is reachable (file backend) but the bronze table was never created.
    h = query.health(file_settings)
    assert h.status == "unavailable"
    assert h.table_loadable is False
    assert h.error is not None
