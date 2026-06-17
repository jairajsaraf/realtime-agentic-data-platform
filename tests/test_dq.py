from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from rtdp.dq import format_report, run_dq
from rtdp.transforms import raw_records_to_bronze

INGEST = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


def _raw(i: int = 0, **over) -> dict:
    base = dict(
        icao24=f"a{i:05x}",
        callsign="DLH9LH",
        origin_country="Germany",
        time_position=1_700_000_000 + i,
        last_contact=1_700_000_050 + i,
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


def _df(raws: list[dict]) -> pd.DataFrame:
    rows = raw_records_to_bronze(raws, ingest_time=INGEST, ingest_batch_id="b", source_name="s")
    return pd.DataFrame(rows)


def test_clean_batch_passes():
    rep = run_dq(_df([_raw(0), _raw(1)]))
    assert rep.ok is True
    assert rep.failures is None
    assert rep.n_failures == 0


def test_null_icao24_fails():
    rep = run_dq(_df([_raw(0), _raw(1, icao24=None)]))
    assert rep.ok is False
    assert "icao24" in set(rep.failures["column"])


def test_out_of_range_latitude_fails():
    rep = run_dq(_df([_raw(0, latitude=999.0)]))
    assert rep.ok is False
    assert "latitude" in set(rep.failures["column"])


def test_out_of_range_longitude_fails():
    rep = run_dq(_df([_raw(0, longitude=-999.0)]))
    assert rep.ok is False
    assert "longitude" in set(rep.failures["column"])


def test_high_velocity_warns_not_fails():
    rep = run_dq(_df([_raw(0, velocity=9999.0)]))
    assert rep.ok is True
    assert rep.warnings is not None
    assert "velocity" in set(rep.warnings["column"])


def test_unknown_position_source_warns():
    rep = run_dq(_df([_raw(0, position_source=9)]))
    assert rep.ok is True
    assert rep.warnings is not None
    assert "position_source" in set(rep.warnings["column"])


def test_duplicate_rows_warn():
    rep = run_dq(_df([_raw(0), _raw(0)]))  # identical icao24 + last_contact
    assert rep.ok is True
    assert rep.warnings is not None
    assert rep.n_warnings >= 1


def test_format_report_mentions_fail():
    rep = run_dq(_df([_raw(0, icao24=None)]))
    text = format_report(rep)
    assert "FAIL" in text
