"""Pure, testable transforms: raw OpenSky-shaped records -> bronze rows -> Arrow.

Kept free of I/O so they can be unit-tested directly. A "raw record" is a dict keyed
by OpenSky state-vector field names (see ``sources``); a "bronze row" is a dict whose
keys are exactly the columns of :data:`rtdp.schema.BRONZE_SCHEMA`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa
from pyiceberg.io.pyarrow import schema_to_pyarrow

from .schema import BRONZE_SCHEMA

# Derived once from the Iceberg schema so nullability, field-ids and the
# timestamp("us", tz="UTC") mapping are guaranteed to match the table on append.
_BRONZE_ARROW_SCHEMA = schema_to_pyarrow(BRONZE_SCHEMA)

# Bronze column order (kept in sync with BRONZE_SCHEMA via the assertion below).
BRONZE_COLUMNS: list[str] = [f.name for f in BRONZE_SCHEMA.fields]


def bronze_arrow_schema() -> pa.Schema:
    """The PyArrow schema the bronze table expects on append."""
    return _BRONZE_ARROW_SCHEMA


def to_event_time(time_position: int | float | None, last_contact: int | float | None) -> datetime:
    """Event (observation) time as a UTC datetime.

    Prefers ``time_position`` (last position update); falls back to ``last_contact``
    (last message received). Raises if both are missing — a record with no time at
    all is invalid and should be caught upstream/by DQ.
    """
    epoch = time_position if time_position is not None else last_contact
    if epoch is None:
        raise ValueError("record has neither time_position nor last_contact")
    return datetime.fromtimestamp(int(epoch), tz=UTC)


def normalize_callsign(raw: str | None) -> str | None:
    """OpenSky callsigns are space-padded (e.g. ``'DLH9LH  '``); trim, empty -> None."""
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def build_bronze_row(
    raw: dict,
    *,
    ingest_time: datetime,
    ingest_batch_id: str,
    source_name: str,
) -> dict:
    """Map one raw OpenSky-shaped record to a bronze row dict (all 21 columns)."""
    return {
        "icao24": raw.get("icao24"),
        "callsign": normalize_callsign(raw.get("callsign")),
        "origin_country": raw.get("origin_country"),
        "event_time": to_event_time(raw.get("time_position"), raw.get("last_contact")),
        "time_position": raw.get("time_position"),
        "last_contact": raw.get("last_contact"),
        "longitude": raw.get("longitude"),
        "latitude": raw.get("latitude"),
        "baro_altitude": raw.get("baro_altitude"),
        "geo_altitude": raw.get("geo_altitude"),
        "on_ground": raw.get("on_ground"),
        "velocity": raw.get("velocity"),
        "true_track": raw.get("true_track"),
        "vertical_rate": raw.get("vertical_rate"),
        "squawk": raw.get("squawk"),
        "spi": raw.get("spi"),
        "position_source": raw.get("position_source"),
        "category": raw.get("category"),
        "ingest_time": ingest_time,
        "ingest_batch_id": ingest_batch_id,
        "source_name": source_name,
    }


def dedupe_raw_records(records: list[dict]) -> list[dict]:
    """Drop within-batch duplicates keyed on ``(icao24, last_contact)``, keeping the
    first occurrence.

    Used by the Stage 2B micro-batch loop so repeated state rows in a single poll
    collapse to one logical observation per aircraft-contact (the same key the
    ``unique_state_key`` DQ warning uses). Records missing either key (``icao24`` or
    ``last_contact`` is ``None``) cannot form a key and are passed through unchanged,
    so DQ still surfaces them rather than having them silently dropped. The input list
    and its records are not mutated.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for record in records:
        icao24 = record.get("icao24")
        last_contact = record.get("last_contact")
        if icao24 is None or last_contact is None:
            out.append(record)
            continue
        key = (icao24, last_contact)
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def raw_records_to_bronze(
    records: list[dict],
    *,
    ingest_time: datetime,
    ingest_batch_id: str,
    source_name: str,
) -> list[dict]:
    """Map a batch of raw records to bronze rows.

    ``event_time`` derivation can fail for a record missing both timestamps; such a
    record is dropped here with its time fields, and the missing-time case is also a
    FAIL-level DQ rule on ``event_time`` for any record that slips through with a null.
    """
    rows: list[dict] = []
    for raw in records:
        try:
            rows.append(
                build_bronze_row(
                    raw,
                    ingest_time=ingest_time,
                    ingest_batch_id=ingest_batch_id,
                    source_name=source_name,
                )
            )
        except ValueError:
            # No usable timestamp — represent as a row with null event_time so DQ
            # surfaces it as a FAIL rather than silently swallowing the record.
            row = build_bronze_row(
                {**raw, "time_position": 0, "last_contact": 0},
                ingest_time=ingest_time,
                ingest_batch_id=ingest_batch_id,
                source_name=source_name,
            )
            row["event_time"] = None
            row["time_position"] = raw.get("time_position")
            row["last_contact"] = raw.get("last_contact")
            rows.append(row)
    return rows


def bronze_rows_to_arrow(rows: list[dict]) -> pa.Table:
    """Build the PyArrow table the bronze Iceberg table expects on append."""
    return pa.Table.from_pylist(rows, schema=_BRONZE_ARROW_SCHEMA)
