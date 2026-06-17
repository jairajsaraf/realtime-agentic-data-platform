"""Source interface. Keeps ingestion source-agnostic so OpenSky-live and the
synthetic generator share one transform/DQ/write path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class RawBatch:
    """A batch of raw OpenSky-shaped state-vector records plus provenance.

    Each record is a dict keyed by the OpenSky state-vector field names
    (icao24, callsign, longitude, latitude, baro_altitude, ...).
    """

    records: list[dict]
    source_name: str


@runtime_checkable
class Source(Protocol):
    name: str

    def fetch(self) -> RawBatch:
        """Return one batch of raw records."""
        ...
