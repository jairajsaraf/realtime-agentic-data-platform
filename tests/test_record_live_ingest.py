"""Offline tests for scripts/record_live_ingest.py (no network; the CLI run is faked)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "record_live_ingest.py"
_spec = importlib.util.spec_from_file_location("record_live_ingest", _PATH)
recorder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorder)

# Synthetic stand-in for the CLI output, including a WARN table that quotes values.
_STDOUT = """Data quality: PASS (1203 rows, 0 failures, 7 warnings)
WARN checks:
 column   check   failure_case  index
 velocity in_range 2200.0       50

Wrote 1203 rows to bronze.opensky_state_vectors
  snapshot_id    : 42
  snapshot_count : 1
"""


def _fake_run(stdout: str, returncode: int = 0, stderr: str = "", seen: dict | None = None):
    def run(env, cwd, timeout):
        if seen is not None:
            seen.update(env=env, cwd=cwd, cwd_existed=cwd.exists())
        return subprocess.CompletedProcess(recorder.COMMAND, returncode, stdout, stderr)

    return run


def test_parse_counts_keeps_numbers_only():
    assert recorder.parse_counts(_STDOUT) == {
        "rows_in_batch": 1203,
        "dq_status": "PASS",
        "dq_failure_cases": 0,
        "dq_warning_cases": 7,
        "rows_written": 1203,
    }


def test_dq_abort_records_zero_rows_written():
    out = "Data quality: FAIL (900 rows, 3 failures, 1 warnings)\nFAIL checks:\n..."
    assert recorder.parse_counts(out)["rows_written"] == 0


def test_env_forces_anonymous_file_backend(tmp_path):
    base = {"RTDP_OPENSKY_CLIENT_ID": "x", "RTDP_OPENSKY_CLIENT_SECRET": "y", "PATH": "/bin"}
    env = recorder.ingest_env(tmp_path, base)
    assert "RTDP_OPENSKY_CLIENT_ID" not in env and "RTDP_OPENSKY_CLIENT_SECRET" not in env
    assert env["RTDP_STORAGE_BACKEND"] == "file"
    assert env["RTDP_LOCAL_WAREHOUSE_DIR"].startswith(str(tmp_path))
    assert env["RTDP_SOURCE"] == "opensky-live"


def test_record_runs_outside_the_repo_and_deletes_the_temp_dir():
    seen: dict = {}
    data = recorder.record(run_cli=_fake_run(_STDOUT, seen=seen))
    assert seen["cwd_existed"] is True
    assert recorder.REPO_ROOT not in seen["cwd"].parents
    assert not seen["cwd"].exists()
    assert data["temp_dir_removed"] is True
    assert data["rows_written"] == 1203 and data["error_class"] is None
    dumped = json.dumps(data)
    for leaked in ("2200", "velocity", "in_range", "snapshot_id"):  # values from the CLI output
        assert leaked not in dumped


def test_network_failure_keeps_only_the_exception_class():
    stderr = "Traceback (most recent call last):\n  ...\nhttpx.ConnectError: [Errno -2] host x\n"
    data = recorder.record(run_cli=_fake_run("", returncode=1, stderr=stderr))
    assert data["error_class"] == "httpx.ConnectError"
    assert "Errno" not in json.dumps(data)


def test_write_record_refuses_icao24_like_tokens(tmp_path):
    with pytest.raises(ValueError, match="icao24-like"):
        recorder.write_record({"leak": "3c6444"}, tmp_path / "out.json")
    recorder.write_record({"rows_written": 123456}, tmp_path / "ok.json")  # digits only: fine
    assert (tmp_path / "ok.json").exists()
