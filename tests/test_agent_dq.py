"""Unit tests for the agent's read-derived DQ diagnosis (pure functions, no network)."""

from __future__ import annotations

from rtdp import dq as ingest_dq
from rtdp.agent import dq


def _clean_row(**over) -> dict:
    row = {
        "icao24": "abc123",
        "callsign": "TST1",
        "event_time": "2026-06-14T00:00:00Z",
        "last_contact": 1000,
        "on_ground": False,
        "latitude": 10.0,
        "longitude": 20.0,
        "geo_altitude": 1000.0,
        "velocity": 200.0,
        "position_source": 0,
    }
    row.update(over)
    return row


def test_thresholds_mirror_ingest_dq():
    # The shared position-source allowlist must match rtdp.dq exactly.
    assert tuple(dq.KNOWN_POSITION_SOURCES) == tuple(ingest_dq.KNOWN_POSITION_SOURCES)
    # Range/length constants mirror the literals encoded in rtdp.dq's WARN/FAIL schemas.
    assert dq.VELOCITY_RANGE == (0.0, 1500.0)
    assert dq.GEO_ALTITUDE_RANGE == (-1000.0, 80000.0)
    assert dq.LATITUDE_RANGE == (-90.0, 90.0)
    assert dq.LONGITUDE_RANGE == (-180.0, 180.0)
    assert dq.CALLSIGN_LENGTH == (1, 8)


def test_detect_over_speed_warn():
    rows = [_clean_row(), _clean_row(velocity=2200.0)]
    findings = {f.kind: f for f in dq.detect_anomalies(rows)}
    assert "over_speed" in findings
    assert findings["over_speed"].severity == "WARN"
    assert findings["over_speed"].count == 1
    assert findings["over_speed"].examples[0]["velocity"] == 2200.0


def test_detect_fail_level_anomalies():
    rows = [
        _clean_row(icao24=None),
        _clean_row(latitude=999.0),
        _clean_row(position_source=9),
    ]
    findings = {f.kind: f for f in dq.detect_anomalies(rows)}
    assert findings["null_icao24"].severity == "FAIL"
    assert findings["latitude_out_of_range"].severity == "FAIL"
    assert findings["unknown_position_source"].severity == "WARN"


def test_detect_duplicate_state_key():
    rows = [_clean_row(icao24="dup", last_contact=5), _clean_row(icao24="dup", last_contact=5)]
    findings = {f.kind: f for f in dq.detect_anomalies(rows)}
    assert findings["duplicate_state_key"].count == 1


def test_clean_rows_have_no_findings():
    assert dq.detect_anomalies([_clean_row(), _clean_row(icao24="def456")]) == []


def test_propose_remediations_dedup_by_kind():
    # Distinct null_* findings collapse to a single "null_required" proposal; over_speed adds one.
    findings = [
        dq.Finding("null_icao24", "FAIL", 1, "icao24 null"),
        dq.Finding("null_last_contact", "FAIL", 1, "last_contact null"),
        dq.Finding("over_speed", "WARN", 1, "fast"),
    ]
    proposals = dq.propose_remediations(findings)
    assert len(proposals) == 2
    assert all(p.startswith("PROPOSED (requires human approval; not applied):") for p in proposals)


def test_diagnose_flags_anomaly_with_provenance_and_limits():
    rows = [_clean_row(velocity=2200.0)]
    result = dq.diagnose(
        rows, snapshot_id=42, endpoint="GET /flights", returned=1, requested_limit=1000
    )
    assert result["ok"] is False
    assert result["snapshot_id"] == 42
    assert any(f["kind"] == "over_speed" for f in result["findings"])
    assert result["proposals"]
    assert "snapshot 42" in result["limitations"]
    assert "1000" in result["limitations"]


def test_diagnose_ok_when_clean():
    result = dq.diagnose(
        [_clean_row()], snapshot_id=1, endpoint="GET /flights", returned=1, requested_limit=10
    )
    assert result["ok"] is True
    assert result["findings"] == []
    assert result["proposals"] == []


def test_detect_latest_state_gaps():
    older = [_clean_row(icao24="a"), _clean_row(icao24="b")]
    current = [_clean_row(icao24="a")]
    gap = dq.detect_latest_state_gaps(current, older)
    assert gap is not None
    assert gap.kind == "latest_state_gap"
    assert "b" in gap.examples
    assert dq.detect_latest_state_gaps(older, older) is None
