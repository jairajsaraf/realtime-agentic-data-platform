"""Pydantic v2 request/response models for the serving layer.

These are the typed contracts behind the auto-generated OpenAPI schema. ``StateVector``
mirrors the bronze table exactly (``src/rtdp/schema.py``) — field names are never invented.
A future Stage D agent can consume the OpenAPI schema directly as tool definitions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StateVector(BaseModel):
    """One aircraft state observation — mirrors ``bronze.opensky_state_vectors``."""

    icao24: str
    callsign: str | None = None
    origin_country: str | None = None
    event_time: datetime
    time_position: int | None = None
    last_contact: int
    longitude: float | None = None
    latitude: float | None = None
    baro_altitude: float | None = None
    geo_altitude: float | None = None
    on_ground: bool
    velocity: float | None = None
    true_track: float | None = None
    vertical_rate: float | None = None
    squawk: str | None = None
    spi: bool | None = None
    position_source: int | None = None
    category: int | None = None
    ingest_time: datetime
    ingest_batch_id: str
    source_name: str


class FlightsResponse(BaseModel):
    snapshot_id: int | None = Field(description="Snapshot that answered the query.")
    count: int = Field(
        description="Number of items returned in this response (after limit/offset) — "
        "NOT the total number of matching rows."
    )
    items: list[StateVector]


class IntervalBucket(BaseModel):
    bucket_start: datetime = Field(description="UTC start of the interval bucket.")
    group: str | None = Field(default=None, description="group_by value (e.g. origin_country).")
    count: int


class StatsResponse(BaseModel):
    snapshot_id: int | None = Field(description="Snapshot that answered the query.")
    buckets: list[IntervalBucket]


class SnapshotItem(BaseModel):
    snapshot_id: int
    timestamp: datetime
    operation: str | None = None
    summary: dict[str, str]


class SchemaField(BaseModel):
    field_id: int
    name: str
    type: str
    required: bool


class PartitionFieldInfo(BaseModel):
    name: str
    transform: str
    source_id: int
    source_column: str | None = None


class MetaResponse(BaseModel):
    # ``schema`` shadows a deprecated BaseModel attribute, so the Python field is
    # ``table_schema`` with a ``schema`` alias; FastAPI serializes responses by alias.
    model_config = ConfigDict(populate_by_name=True)

    table_identifier: str
    current_snapshot_id: int | None
    snapshot_count: int
    table_schema: list[SchemaField] = Field(alias="schema")
    partition_spec: list[PartitionFieldInfo]


class HealthResponse(BaseModel):
    status: str = Field(description="'ok' when catalog + table are reachable, else 'unavailable'.")
    catalog_reachable: bool
    table_loadable: bool
    current_snapshot_id: int | None = None
    error: str | None = Field(default=None, description="Failure detail when unhealthy.")


class LivenessResponse(BaseModel):
    """Liveness contract: the process is serving. Never reflects catalog/table/data state."""

    status: Literal["alive"] = "alive"
