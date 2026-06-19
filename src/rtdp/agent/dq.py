"""Pure data-quality re-derivation for the Stage D agent.

Stage 1 computes Pandera WARN/FAIL checks at ingest time and does NOT persist the result, and the
Stage 2A API exposes no DQ endpoint. So the agent re-derives anomalies directly from the flight
rows returned by the read API. These functions are pure (``list[dict] -> findings``) and operate
only on API response payloads — they never import the query/catalog layer.

Thresholds intentionally mirror ``rtdp.dq`` (a test asserts parity) but are duplicated here so the
agent stays a self-contained API client. Diagnosis is necessarily BOUNDED: it sees only the rows
the API returned (one queried window, capped by the API row limit, for the snapshots queried), so
it is a sample — not a full-table audit. Every diagnosis states this limit, and it proposes
remediations only; it applies nothing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

# Mirror of rtdp.dq thresholds (parity asserted in tests/test_agent_dq.py).
VELOCITY_RANGE = (0.0, 1500.0)
GEO_ALTITUDE_RANGE = (-1000.0, 80000.0)
KNOWN_POSITION_SOURCES = (0, 1, 2, 3)
LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)
CALLSIGN_LENGTH = (1, 8)
REQUIRED_FIELDS = ("icao24", "event_time", "last_contact", "on_ground")

_REMEDIATIONS = {
    "over_speed": "review the source velocity calibration or add a stricter ingest filter",
    "unknown_position_source": "map or quarantine unknown position_source codes at ingest",
    "geo_altitude_out_of_range": "clamp or reject implausible geo_altitude at ingest",
    "callsign_length": "trim/validate callsign formatting at ingest",
    "latitude_out_of_range": "reject rows with invalid latitude (already a FAIL rule)",
    "longitude_out_of_range": "reject rows with invalid longitude (already a FAIL rule)",
    "null_required": "investigate why a required field is null and fix the upstream source",
    "duplicate_state_key": "verify within-batch dedup on (icao24, last_contact)",
    "latest_state_gap": "confirm whether the missing aircraft are expected to have dropped off",
}


@dataclass
class Finding:
    kind: str
    severity: str  # "WARN" | "FAIL" (mirrors rtdp.dq severities)
    count: int
    detail: str
    examples: list = field(default_factory=list)


def _out_of_range(value, low, high) -> bool:
    return value is not None and not (low <= value <= high)


def detect_anomalies(rows: list[dict]) -> list[Finding]:
    """Re-derive WARN/FAIL-equivalent anomalies from a list of API flight rows."""
    findings: list[Finding] = []

    def collect(predicate, kind, severity, detail, value_key=None):
        hits = [r for r in rows if predicate(r)]
        if not hits:
            return
        examples = []
        for r in hits[:5]:
            ex = {"icao24": r.get("icao24")}
            if value_key is not None:
                ex[value_key] = r.get(value_key)
            examples.append(ex)
        findings.append(Finding(kind, severity, len(hits), detail, examples))

    collect(
        lambda r: _out_of_range(r.get("velocity"), *VELOCITY_RANGE),
        "over_speed", "WARN", f"velocity outside {VELOCITY_RANGE} m/s", "velocity",
    )
    collect(
        lambda r: r.get("position_source") is not None
        and r.get("position_source") not in KNOWN_POSITION_SOURCES,
        "unknown_position_source", "WARN",
        f"position_source not in {KNOWN_POSITION_SOURCES}", "position_source",
    )
    collect(
        lambda r: _out_of_range(r.get("geo_altitude"), *GEO_ALTITUDE_RANGE),
        "geo_altitude_out_of_range", "WARN",
        f"geo_altitude outside {GEO_ALTITUDE_RANGE} m", "geo_altitude",
    )
    collect(
        lambda r: r.get("callsign") is not None
        and not (CALLSIGN_LENGTH[0] <= len(str(r.get("callsign"))) <= CALLSIGN_LENGTH[1]),
        "callsign_length", "WARN", f"callsign length outside {CALLSIGN_LENGTH}", "callsign",
    )
    collect(
        lambda r: _out_of_range(r.get("latitude"), *LATITUDE_RANGE),
        "latitude_out_of_range", "FAIL", f"latitude outside {LATITUDE_RANGE}", "latitude",
    )
    collect(
        lambda r: _out_of_range(r.get("longitude"), *LONGITUDE_RANGE),
        "longitude_out_of_range", "FAIL", f"longitude outside {LONGITUDE_RANGE}", "longitude",
    )
    for name in REQUIRED_FIELDS:
        collect(
            lambda r, f=name: r.get(f) is None,
            f"null_{name}", "FAIL", f"required field '{name}' is null",
        )

    # Duplicate (icao24, last_contact) within the sample. NOTE: bronze is append-only, so the same
    # logical state can legitimately recur across snapshots; flagged WARN with that caveat.
    keys = Counter(
        (r.get("icao24"), r.get("last_contact"))
        for r in rows
        if r.get("icao24") is not None and r.get("last_contact") is not None
    )
    duplicates = sum(c - 1 for c in keys.values() if c > 1)
    if duplicates:
        findings.append(
            Finding(
                "duplicate_state_key", "WARN", duplicates,
                "duplicate (icao24, last_contact) rows in the sample (expected across snapshots; "
                "within a single snapshot may indicate missing dedup)",
            )
        )
    return findings


def detect_latest_state_gaps(current_rows: list[dict], older_rows: list[dict]) -> Finding | None:
    """Aircraft present in an older sample but absent from the current one (a coverage gap)."""
    current = {r.get("icao24") for r in current_rows if r.get("icao24") is not None}
    older = {r.get("icao24") for r in older_rows if r.get("icao24") is not None}
    missing = sorted(older - current)
    if not missing:
        return None
    return Finding(
        "latest_state_gap", "WARN", len(missing),
        "aircraft present in the older snapshot but absent from the current sample",
        list(missing[:5]),
    )


def _remediation_key(kind: str) -> str:
    return "null_required" if kind.startswith("null_") else kind


def propose_remediations(findings: list[Finding]) -> list[str]:
    """One HITL remediation proposal per distinct finding kind. Proposals only — never applied."""
    proposals: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        key = _remediation_key(finding.kind)
        if key in seen:
            continue
        seen.add(key)
        action = _REMEDIATIONS.get(key, "investigate the affected rows and correct the source")
        proposals.append(f"PROPOSED (requires human approval; not applied): {action}.")
    return proposals


def diagnose(
    rows: list[dict],
    *,
    snapshot_id: int | None,
    endpoint: str,
    returned: int,
    requested_limit: int,
) -> dict:
    """Compose a read-only DQ diagnosis from API flight rows: findings + HITL proposals + limits."""
    findings = detect_anomalies(rows)
    limitations = (
        f"Sampled {len(rows)} row(s) from {endpoint} at snapshot {snapshot_id}; bounded by the "
        f"queried window and the API row limit ({requested_limit}). WARN/FAIL history is not "
        "persisted, so this is a re-derived sample, not a full-table audit."
    )
    return {
        "snapshot_id": snapshot_id,
        "endpoint": endpoint,
        "rows_examined": len(rows),
        "returned": returned,
        "requested_limit": requested_limit,
        "findings": [asdict(f) for f in findings],
        "proposals": propose_remediations(findings),
        "limitations": limitations,
        "ok": not findings,
    }
