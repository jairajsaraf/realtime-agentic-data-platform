"""Ingestion-time data-quality checks with warn/fail severities.

pandera 0.31 has no per-check severity that covers structural rules (null/dtype/unique),
so severity is expressed with two schemas, both validated lazily:

- :data:`FAIL_SCHEMA` — hard rules. Any violation aborts the ingest (nothing written).
- :data:`WARN_SCHEMA` — soft rules. Violations are reported but do not abort.

Both produce a uniform ``failure_cases`` frame (columns: schema_context, column, check,
check_number, failure_case, index), surfaced via :func:`format_report`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

KNOWN_POSITION_SOURCES = [0, 1, 2, 3]  # ADS-B, ASTERIX, MLAT, FLARM

# --- FAIL: violating any of these aborts the ingest -------------------------------
FAIL_SCHEMA = pa.DataFrameSchema(
    {
        "icao24": pa.Column(str, nullable=False),  # required stable id
        "event_time": pa.Column(nullable=False),  # timestamp validity (null -> invalid)
        "last_contact": pa.Column(nullable=False),
        "on_ground": pa.Column(nullable=False),
        "ingest_time": pa.Column(nullable=False),
        "ingest_batch_id": pa.Column(str, nullable=False),
        "source_name": pa.Column(str, nullable=False),
        "latitude": pa.Column(float, pa.Check.in_range(-90.0, 90.0), nullable=True, coerce=True),
        "longitude": pa.Column(float, pa.Check.in_range(-180.0, 180.0), nullable=True, coerce=True),
    },
    strict=False,
)

# --- WARN: reported, never aborts -------------------------------------------------
WARN_SCHEMA = pa.DataFrameSchema(
    {
        "velocity": pa.Column(
            float, pa.Check.in_range(0.0, 1500.0), nullable=True, required=False, coerce=True
        ),
        "geo_altitude": pa.Column(
            float, pa.Check.in_range(-1000.0, 80000.0), nullable=True, required=False, coerce=True
        ),
        "position_source": pa.Column(
            checks=pa.Check.isin(KNOWN_POSITION_SOURCES), nullable=True, required=False
        ),
        "callsign": pa.Column(str, pa.Check.str_length(1, 8), nullable=True, required=False),
    },
    checks=[
        pa.Check(
            lambda df: bool(not df.duplicated(subset=["icao24", "last_contact"]).any()),
            error="duplicate (icao24, last_contact) rows present",
            name="unique_state_key",
        )
    ],
    strict=False,
)


@dataclass
class DQReport:
    n_rows: int
    failures: pd.DataFrame | None  # None when no FAIL-level violations
    warnings: pd.DataFrame | None  # None when no WARN-level violations
    ok: bool  # True when there are no FAIL-level violations

    @property
    def n_failures(self) -> int:
        return 0 if self.failures is None else len(self.failures)

    @property
    def n_warnings(self) -> int:
        return 0 if self.warnings is None else len(self.warnings)


def run_dq(df: pd.DataFrame) -> DQReport:
    """Validate a bronze-row DataFrame. Never raises — the caller decides on failure."""
    failures: pd.DataFrame | None = None
    warnings: pd.DataFrame | None = None

    try:
        FAIL_SCHEMA.validate(df, lazy=True)
    except SchemaErrors as exc:
        failures = exc.failure_cases

    try:
        WARN_SCHEMA.validate(df, lazy=True)
    except SchemaErrors as exc:
        warnings = exc.failure_cases

    return DQReport(n_rows=len(df), failures=failures, warnings=warnings, ok=failures is None)


def _summarize(frame: pd.DataFrame, limit: int = 10) -> str:
    cols = ["column", "check", "failure_case", "index"]
    present = [c for c in cols if c in frame.columns]
    head = frame[present].head(limit).to_string(index=False)
    extra = "" if len(frame) <= limit else f"\n  ... (+{len(frame) - limit} more)"
    return head + extra


def format_report(report: DQReport) -> str:
    """Human-readable DQ summary for CLI output."""
    status = "PASS" if report.ok else "FAIL"
    lines = [
        f"Data quality: {status} ({report.n_rows} rows, "
        f"{report.n_failures} failures, {report.n_warnings} warnings)"
    ]
    if report.failures is not None:
        lines.append("FAIL checks:")
        lines.append(_summarize(report.failures))
    if report.warnings is not None:
        lines.append("WARN checks:")
        lines.append(_summarize(report.warnings))
    return "\n".join(lines)
