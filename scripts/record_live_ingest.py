"""Record ONE live OpenSky ingest as counts only (opt-in; network-gated; never run in CI).

Runs the real CLI ingest path (`rtdp ingest --source opensky-live`, via `python -m rtdp.cli`) on
the anonymous OpenSky tier into a throwaway file:// warehouse created outside the repo, then keeps
only aggregate numbers: UTC timestamp, wall seconds, exit code, rows in the batch, rows written,
and the DQ failure/warning case counts. The CLI's output is parsed in memory and never printed or
saved, because its DQ report can quote real record values (failure cases). No records, no
failure-case values, and no warehouse files leave this script; the temp directory is deleted.

OpenSky data is licensed for non-commercial use and must not be redistributed, which is why only
counts are recorded.

    uv run python scripts/record_live_ingest.py            # writes results/opensky_live_run.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "results" / "opensky_live_run.json"
COMMAND = [sys.executable, "-m", "rtdp.cli", "ingest", "--source", "opensky-live"]

_DQ_LINE = re.compile(
    r"Data quality: (?P<status>PASS|FAIL) \((?P<rows>\d+) rows, (?P<failures>\d+) failures, "
    r"(?P<warnings>\d+) warnings\)"
)
_WROTE_LINE = re.compile(r"^Wrote (?P<rows>\d+) rows to ", re.MULTILINE)
# A 6-character hex token with at least one letter looks like an icao24 address: never write one.
_ICAO24_LIKE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{6}\b", re.IGNORECASE)


def parse_counts(stdout: str) -> dict:
    """Aggregate numbers from the CLI output; everything else in it is discarded."""
    dq = _DQ_LINE.search(stdout)
    wrote = _WROTE_LINE.search(stdout)
    return {
        "rows_in_batch": int(dq["rows"]) if dq else None,
        "dq_status": dq["status"] if dq else None,
        "dq_failure_cases": int(dq["failures"]) if dq else None,
        "dq_warning_cases": int(dq["warnings"]) if dq else None,
        "rows_written": int(wrote["rows"]) if wrote else 0,
    }


def ingest_env(workdir: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """file:// warehouse under `workdir`, live source, and NO OpenSky credentials (anonymous)."""
    env = dict(os.environ if base is None else base)
    for key in ("RTDP_OPENSKY_CLIENT_ID", "RTDP_OPENSKY_CLIENT_SECRET"):
        env.pop(key, None)
    env.update(
        RTDP_STORAGE_BACKEND="file",
        RTDP_LOCAL_WAREHOUSE_DIR=str(workdir / "warehouse"),
        RTDP_CATALOG_DB_PATH=str(workdir / "warehouse" / "catalog.db"),
        RTDP_SOURCE="opensky-live",
    )
    return env


def _run_cli(env: dict[str, str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        COMMAND, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def record(
    *,
    run_cli: Callable[[dict[str, str], Path, float], subprocess.CompletedProcess] = _run_cli,
    timeout: float = 120.0,
) -> dict:
    """Run the ingest once in a throwaway directory and return the counts-only record."""
    workdir = Path(tempfile.mkdtemp(prefix="rtdp-opensky-"))
    try:
        recorded = datetime.now(UTC).isoformat(timespec="seconds")
        start = time.perf_counter()
        proc = run_cli(ingest_env(workdir), workdir, timeout)
        wall = round(time.perf_counter() - start, 3)
        counts = parse_counts(proc.stdout or "")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    error = None
    if proc.returncode != 0 and counts["dq_status"] is None:
        # Keep only the exception class name from the traceback; never the message.
        match = re.findall(r"^([A-Za-z_][\w.]*(?:Error|Exception))\b", proc.stderr or "", re.M)
        error = match[-1] if match else "unknown"
    return {
        "recorded_utc": recorded,
        "command": "rtdp ingest --source opensky-live (python -m rtdp.cli; anonymous tier)",
        "exit_code": proc.returncode,
        "wall_seconds": wall,
        **counts,
        "error_class": error,
        "temp_dir_removed": not workdir.exists(),
        "notes": (
            "Counts only. No records, failure-case values, or warehouse files were kept; the "
            "throwaway warehouse lived outside the repo and was deleted. DQ counts are "
            "failure/warning cases (one per failing value), not rows."
        ),
    }


def write_record(data: dict, out: Path) -> None:
    text = json.dumps(data, indent=2) + "\n"
    if _ICAO24_LIKE.search(text):
        raise ValueError("refusing to write: output contains an icao24-like token")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record one live OpenSky ingest (counts only)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    data = record()
    write_record(data, args.out)
    print(json.dumps(data, indent=2))
    return 0 if data["exit_code"] in (0, 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
