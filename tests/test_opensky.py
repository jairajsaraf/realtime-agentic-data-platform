from __future__ import annotations

from rtdp.sources.opensky import OpenSkyLiveSource


def test_state_to_dict_positional_mapping():
    state = [
        "abc123",
        "DLH9LH  ",
        "Germany",
        1_700_000_000,
        1_700_000_050,
        8.5,
        50.0,
        10000.0,
        False,
        230.0,
        180.0,
        0.0,
        [1, 2],
        10200.0,
        "1000",
        False,
        0,
        2,
    ]
    rec = OpenSkyLiveSource.state_to_dict(state)
    assert rec["icao24"] == "abc123"
    assert rec["origin_country"] == "Germany"
    assert rec["geo_altitude"] == 10200.0
    assert rec["position_source"] == 0
    assert rec["category"] == 2
    assert "sensors" not in rec  # dropped


def test_state_to_dict_handles_short_state_without_category():
    state = [
        "abc123",
        "DLH ",
        "Germany",
        1_700_000_000,
        1_700_000_050,
        8.5,
        50.0,
        10000.0,
        False,
        230.0,
        180.0,
        0.0,
        None,
        10200.0,
        "1000",
        False,
        0,
    ]
    rec = OpenSkyLiveSource.state_to_dict(state)
    assert rec["category"] is None
    assert rec["position_source"] == 0
