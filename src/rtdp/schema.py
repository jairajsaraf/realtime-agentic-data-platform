"""Iceberg table design for the bronze OpenSky state-vector table.

Design notes:
- ``event_time`` (observation time) and ``ingest_time`` (write time) are distinct,
  enabling late-data reasoning later.
- ``icao24`` is the stable aircraft identifier.
- Geospatial + kinematic fields are kept close to the OpenSky source shape.
- Initial partitioning is a *hidden* ``day(event_time)`` transform: queries filter
  the raw ``event_time`` column and Iceberg prunes partitions. Day granularity is
  intentionally coarse to avoid tiny files for both sparse backfill and future
  continuous appends. The partition-evolution demo (Phase 3) adds ``hour(event_time)``
  for future writes only, without rewriting existing data.
"""

from __future__ import annotations

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

# Field ids are explicit and stable (Iceberg tracks columns by id, not name).
BRONZE_SCHEMA = Schema(
    NestedField(1, "icao24", StringType(), required=True),
    NestedField(2, "callsign", StringType(), required=False),
    NestedField(3, "origin_country", StringType(), required=False),
    NestedField(4, "event_time", TimestamptzType(), required=True),
    NestedField(5, "time_position", LongType(), required=False),
    NestedField(6, "last_contact", LongType(), required=True),
    NestedField(7, "longitude", DoubleType(), required=False),
    NestedField(8, "latitude", DoubleType(), required=False),
    NestedField(9, "baro_altitude", DoubleType(), required=False),
    NestedField(10, "geo_altitude", DoubleType(), required=False),
    NestedField(11, "on_ground", BooleanType(), required=True),
    NestedField(12, "velocity", DoubleType(), required=False),
    NestedField(13, "true_track", DoubleType(), required=False),
    NestedField(14, "vertical_rate", DoubleType(), required=False),
    NestedField(15, "squawk", StringType(), required=False),
    NestedField(16, "spi", BooleanType(), required=False),
    NestedField(17, "position_source", IntegerType(), required=False),
    NestedField(18, "category", IntegerType(), required=False),
    NestedField(19, "ingest_time", TimestamptzType(), required=True),
    NestedField(20, "ingest_batch_id", StringType(), required=True),
    NestedField(21, "source_name", StringType(), required=True),
)

# Partition field ids start at 1000 by Iceberg convention.
_EVENT_TIME_FIELD_ID = 4

INITIAL_SPEC = PartitionSpec(
    PartitionField(
        source_id=_EVENT_TIME_FIELD_ID,
        field_id=1000,
        transform=DayTransform(),
        name="event_day",
    ),
    spec_id=0,
)
