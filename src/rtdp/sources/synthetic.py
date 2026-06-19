"""Deterministic synthetic OpenSky-shaped batch generator.

This source exists ONLY for reproducible local/CI verification — it is not real data.
The real public-dataset path is the opt-in live OpenSky source (see ``opensky.py``).

The generator is seeded and derives event-times from a fixed base date (never ``now``),
so a given (seed, n_rows, base_date) always yields the same batch. Rows span multiple
days/hours so Phase-3 partition-evolution and time-travel demos have data. It can inject
WARN-level rows (over-speed, unknown position_source, duplicates) and FAIL-level rows
(null icao24, out-of-range coordinates) to exercise the DQ severities.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

from .base import RawBatch

_AIRLINES = ["DLH", "BAW", "AFR", "UAL", "AAL", "SWR", "KLM", "RYR"]
_COUNTRIES = [
    "Germany",
    "United Kingdom",
    "France",
    "United States",
    "Switzerland",
    "Netherlands",
    "Ireland",
]


def _complete_record(epoch: int, icao24: str | None, **over) -> dict:
    """A fully-populated, valid OpenSky-shaped record; override fields to taint it."""
    rec = dict(
        icao24=icao24,
        callsign="TST123",
        origin_country="Testland",
        time_position=epoch,
        last_contact=epoch + 5,
        longitude=0.0,
        latitude=0.0,
        baro_altitude=1000.0,
        geo_altitude=1100.0,
        on_ground=False,
        velocity=200.0,
        true_track=90.0,
        vertical_rate=0.0,
        squawk="1000",
        spi=False,
        position_source=0,
        category=1,
    )
    rec.update(over)
    return rec


class SyntheticSource:
    name = "opensky_synthetic"

    def __init__(
        self,
        *,
        n_rows: int = 50,
        seed: int = 42,
        days: int = 3,
        base_date: str = "2026-06-14",
        inject_warnings: bool = True,
        inject_failures: bool = False,
    ) -> None:
        self.n_rows = n_rows
        self.seed = seed
        self.days = days
        self.base_date = base_date
        self.inject_warnings = inject_warnings
        self.inject_failures = inject_failures

    def fetch(self) -> RawBatch:
        rng = random.Random(self.seed)
        base = datetime.fromisoformat(self.base_date).replace(tzinfo=UTC)
        base_epoch = int(base.timestamp())
        span = self.days * 86400

        records: list[dict] = []
        for _ in range(self.n_rows):
            tpos = base_epoch + rng.randint(0, span - 1)
            records.append(
                {
                    "icao24": f"{rng.randint(0, 0xFFFFFF):06x}",
                    "callsign": f"{rng.choice(_AIRLINES)}{rng.randint(1, 9999)}",
                    "origin_country": rng.choice(_COUNTRIES),
                    "time_position": tpos,
                    "last_contact": tpos + rng.randint(0, 15),
                    "longitude": round(rng.uniform(-180, 180), 4),
                    "latitude": round(rng.uniform(-90, 90), 4),
                    "baro_altitude": round(rng.uniform(0, 12000), 1),
                    "geo_altitude": round(rng.uniform(0, 12500), 1),
                    "on_ground": rng.random() < 0.05,
                    "velocity": round(rng.uniform(0, 300), 1),
                    "true_track": round(rng.uniform(0, 360), 1),
                    "vertical_rate": round(rng.uniform(-15, 15), 1),
                    "squawk": f"{rng.randint(0, 7777):04d}",
                    "spi": False,
                    "position_source": rng.choice([0, 0, 0, 1, 2]),
                    "category": rng.randint(0, 20),
                }
            )

        if self.inject_warnings:
            records.extend(self._warn_rows(base_epoch))
        if self.inject_failures:
            records.extend(self._fail_rows(base_epoch))

        return RawBatch(records=records, source_name=self.name)

    @staticmethod
    def _warn_rows(base_epoch: int) -> list[dict]:
        t = base_epoch + 3600
        return [
            _complete_record(t, "wwwww1", velocity=2200.0),  # over-speed -> WARN
            _complete_record(t + 1, "wwwww2", position_source=9),  # unknown source -> WARN
            _complete_record(t + 2, "dddddd", last_contact=t + 50),  # duplicate pair ...
            _complete_record(
                t + 2, "dddddd", last_contact=t + 50
            ),  # ... same id+last_contact -> WARN
        ]

    @staticmethod
    def _fail_rows(base_epoch: int) -> list[dict]:
        t = base_epoch + 7200
        return [
            _complete_record(t, None),  # null icao24 -> FAIL
            _complete_record(
                t + 1, "fffff1", latitude=999.0, longitude=999.0
            ),  # bad coords -> FAIL
        ]


def _continuous_record(icao24: str, tpos: int, rng: random.Random) -> dict:
    """A valid OpenSky-shaped record for one aircraft at observation epoch ``tpos``.

    All values stay within the DQ ranges, so a continuous batch passes DQ. ``last_contact``
    equals ``time_position`` so the logical key advances in lockstep with ``event_time``.
    """
    return {
        "icao24": icao24,
        "callsign": f"{rng.choice(_AIRLINES)}{rng.randint(1, 9999)}",
        "origin_country": rng.choice(_COUNTRIES),
        "time_position": tpos,
        "last_contact": tpos,
        "longitude": round(rng.uniform(-180, 180), 4),
        "latitude": round(rng.uniform(-90, 90), 4),
        "baro_altitude": round(rng.uniform(0, 12000), 1),
        "geo_altitude": round(rng.uniform(0, 12500), 1),
        "on_ground": False,
        "velocity": round(rng.uniform(0, 300), 1),
        "true_track": round(rng.uniform(0, 360), 1),
        "vertical_rate": round(rng.uniform(-15, 15), 1),
        "squawk": f"{rng.randint(0, 7777):04d}",
        "spi": False,
        "position_source": rng.choice([0, 0, 0, 1, 2]),
        "category": rng.randint(0, 20),
    }


class ContinuousSyntheticSource:
    """Deterministic *continuous* generator for Stage 2B micro-batch ingestion.

    Unlike :class:`SyntheticSource` (which returns the same fixed-window batch every call),
    each ``fetch()`` advances the time window forward, so successive micro-batches have
    strictly increasing ``event_time``. It emits a **stable fleet** of aircraft (one row per
    aircraft per batch) so a read-time "latest state per aircraft" view returns exactly
    ``fleet_size`` rows, and it deliberately includes ``dup_count`` within-batch duplicate
    state rows (same ``(icao24, last_contact)``) to exercise the dedup step. Fully
    reproducible: batch ``i`` depends only on ``(seed, i)`` via an integer-combined seed,
    never on wall-clock or call order.

    It is NOT real data and exists only for reproducible local/CI verification.
    """

    name = "opensky_synthetic_stream"

    def __init__(
        self,
        *,
        fleet_size: int = 8,
        seed: int = 42,
        base_date: str = "2026-06-14",
        step_seconds: int = 60,
        dup_count: int = 1,
    ) -> None:
        if fleet_size < 1:
            raise ValueError("fleet_size must be >= 1")
        if step_seconds < 1:
            raise ValueError("step_seconds must be >= 1")
        self._fleet = [f"{n:06x}" for n in range(1, fleet_size + 1)]
        self._fleet_size = fleet_size
        self._seed = seed
        self._base_epoch = int(datetime.fromisoformat(base_date).replace(tzinfo=UTC).timestamp())
        self._step_seconds = step_seconds
        self._dup_count = dup_count
        self._batch_index = 0

    def fetch(self) -> RawBatch:
        i = self._batch_index
        # Integer-combined seed (random.Random does not accept tuple seeds): batch i is
        # reproducible regardless of how many fetch() calls preceded it.
        rng = random.Random(self._seed * 1_000_003 + i)
        # Window base advances by step_seconds * fleet_size per batch. The within-batch span
        # is fleet_size-1 < that step, so batch i+1's minimum event_time strictly exceeds
        # batch i's maximum -> monotonically advancing across batches.
        base = self._base_epoch + i * self._step_seconds * self._fleet_size
        records = [_continuous_record(icao, base + k, rng) for k, icao in enumerate(self._fleet)]
        # Within-batch duplicate(s) of the first aircraft's state row (identical key).
        for _ in range(self._dup_count):
            records.append(dict(records[0]))
        self._batch_index += 1
        return RawBatch(records=records, source_name=self.name)
