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
