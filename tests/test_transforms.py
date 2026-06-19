from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa

from rtdp.transforms import (
    BRONZE_COLUMNS,
    bronze_rows_to_arrow,
    build_bronze_row,
    dedupe_raw_records,
    normalize_callsign,
    raw_records_to_bronze,
    to_event_time,
)

INGEST = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


def _raw(**over) -> dict:
    base = dict(
        icao24="abc123",
        callsign="DLH9LH  ",
        origin_country="Germany",
        time_position=1_700_000_000,
        last_contact=1_700_000_050,
        longitude=8.5,
        latitude=50.0,
        baro_altitude=10000.0,
        geo_altitude=10200.0,
        on_ground=False,
        velocity=230.0,
        true_track=180.0,
        vertical_rate=0.0,
        squawk="1000",
        spi=False,
        position_source=0,
        category=2,
    )
    base.update(over)
    return base


def test_to_event_time_prefers_time_position():
    t = to_event_time(1_700_000_000, 1_700_000_050)
    assert t == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert t.tzinfo is not None


def test_to_event_time_fallback_last_contact():
    assert to_event_time(None, 1_700_000_050) == datetime.fromtimestamp(1_700_000_050, tz=UTC)


def test_normalize_callsign():
    assert normalize_callsign("DLH9LH  ") == "DLH9LH"
    assert normalize_callsign("   ") is None
    assert normalize_callsign(None) is None


def test_build_bronze_row_keys_and_mapping():
    row = build_bronze_row(
        _raw(), ingest_time=INGEST, ingest_batch_id="b1", source_name="opensky_synthetic"
    )
    assert set(row.keys()) == set(BRONZE_COLUMNS)
    assert row["icao24"] == "abc123"
    assert row["callsign"] == "DLH9LH"
    assert row["event_time"] == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert row["source_name"] == "opensky_synthetic"


def test_build_bronze_row_missing_optionals_become_none():
    row = build_bronze_row(
        _raw(callsign=None, geo_altitude=None, category=None, squawk=None),
        ingest_time=INGEST,
        ingest_batch_id="b1",
        source_name="s",
    )
    assert row["callsign"] is None
    assert row["geo_altitude"] is None
    assert row["category"] is None
    assert row["squawk"] is None


def test_raw_records_to_bronze_count_and_lineage():
    rows = raw_records_to_bronze(
        [_raw(), _raw(icao24="def456")],
        ingest_time=INGEST,
        ingest_batch_id="b1",
        source_name="s",
    )
    assert len(rows) == 2
    assert all(r["ingest_batch_id"] == "b1" for r in rows)


def test_missing_both_timestamps_yields_null_event_time():
    rows = raw_records_to_bronze(
        [_raw(time_position=None, last_contact=None)],
        ingest_time=INGEST,
        ingest_batch_id="b",
        source_name="s",
    )
    assert rows[0]["event_time"] is None


# --------------------------------------------------------------- within-batch dedup
def test_dedupe_collapses_same_icao24_last_contact_keeping_first():
    first = _raw(icao24="abc123", last_contact=100, callsign="AAA")
    dup = _raw(icao24="abc123", last_contact=100, callsign="BBB")
    out = dedupe_raw_records([first, dup])
    assert len(out) == 1
    assert out[0]["callsign"] == "AAA"  # first occurrence kept


def test_dedupe_keeps_distinct_keys():
    out = dedupe_raw_records(
        [
            _raw(icao24="abc123", last_contact=100),
            _raw(icao24="abc123", last_contact=101),  # same aircraft, later contact
            _raw(icao24="def456", last_contact=100),  # different aircraft
        ]
    )
    assert len(out) == 3


def test_dedupe_passes_through_null_key_records():
    # Records with a null icao24 or last_contact cannot form a key -> never dropped,
    # so DQ still surfaces them.
    out = dedupe_raw_records(
        [
            _raw(icao24=None, last_contact=100),
            _raw(icao24=None, last_contact=100),
            _raw(icao24="abc123", last_contact=None),
        ]
    )
    assert len(out) == 3


def test_dedupe_does_not_mutate_input():
    records = [_raw(icao24="abc123", last_contact=100), _raw(icao24="abc123", last_contact=100)]
    dedupe_raw_records(records)
    assert len(records) == 2  # original list untouched


def test_bronze_rows_to_arrow_schema_and_nullability():
    rows = raw_records_to_bronze([_raw()], ingest_time=INGEST, ingest_batch_id="b", source_name="s")
    tbl = bronze_rows_to_arrow(rows)
    assert tbl.num_rows == 1
    assert set(tbl.schema.names) == set(BRONZE_COLUMNS)
    assert tbl.schema.field("icao24").nullable is False  # required Iceberg field
    assert tbl.schema.field("callsign").nullable is True  # optional
    et = tbl.schema.field("event_time").type
    assert pa.types.is_timestamp(et) and et.tz == "UTC"
