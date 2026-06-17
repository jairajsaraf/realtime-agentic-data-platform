from __future__ import annotations

from pyiceberg.transforms import DayTransform

from rtdp.schema import BRONZE_SCHEMA, INITIAL_SPEC


def test_bronze_schema_columns():
    names = {f.name for f in BRONZE_SCHEMA.fields}
    for expected in ["icao24", "event_time", "ingest_time", "latitude", "longitude", "velocity"]:
        assert expected in names

    assert BRONZE_SCHEMA.find_field("icao24").required is True
    assert BRONZE_SCHEMA.find_field("event_time").required is True
    assert BRONZE_SCHEMA.find_field("ingest_time").required is True
    assert BRONZE_SCHEMA.find_field("callsign").required is False


def test_initial_partition_spec_is_day_event_time():
    assert INITIAL_SPEC.spec_id == 0
    fields = INITIAL_SPEC.fields
    assert len(fields) == 1

    pf = fields[0]
    assert pf.source_id == BRONZE_SCHEMA.find_field("event_time").field_id
    assert isinstance(pf.transform, DayTransform)
    assert pf.name == "event_day"
