"""Offline tests for scripts/reproduce.py (no network; never recurses into pytest)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reproduce.py"
_spec = importlib.util.spec_from_file_location("reproduce_script", _PATH)
repro = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = repro
_spec.loader.exec_module(repro)

_JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="10" failures="1" errors="0" skipped="2">
</testsuite></testsuites>"""

_INGEST_OK = """Data quality: PASS (54 rows, 0 failures, 3 warnings)
WARN checks:
 column check failure_case index
 velocity in_range 2200.0 50

Wrote 54 rows to bronze.opensky_state_vectors
  snapshot_id    : 4100877081503986423
  snapshot_count : 1
"""


def _metrics(**over) -> dict:
    metrics = {
        "generated_utc": "2026-10-27T10:00:00+00:00",
        "commit_sha": "abcdef1234567890",
        "working_tree_dirty": False,
        "machine": {"python": "3.12.0", "os_family": "Linux", "cpu_count": 4},
        "steps": {
            "ruff": {"passed": True, "exit_code": 0, "seconds": 1.0},
            "pytest": {"exit_code": 0, "passed": 100, "skipped": 5, "failures": 0, "errors": 0,
                       "tests": 105, "seconds": 20.0},
            "synthetic_ingest": {"exit_code": 0, "rows_in_batch": 54, "rows_written": 54,
                                 "dq_status": "PASS", "dq_failure_cases": 0,
                                 "dq_warning_cases": 3, "snapshot_count": 1, "seconds": 2.0},
            "dq_gate_demo": {"exit_code": 1, "aborted": True, "rows_written": 0,
                             "dq_failure_cases": 2, "dq_status": "FAIL", "seconds": 1.0},
            "serve_probe": {"livez_status": 200, "health_status": 200, "health_ok": True,
                            "snapshots_listed": 1, "seconds_to_livez": 1.5},
        },
        "agent_evals": [],
        "opensky_live": {"status": "recorded", "recorded_utc": "2026-10-26T23:53:41+00:00",
                         "rows_written": 9167, "wall_seconds": 3.171, "dq_status": "PASS",
                         "dq_failure_cases": 0, "dq_warning_cases": 2,
                         "note": "recorded run 2026-10-26, not re-run"},
    }
    metrics.update(over)
    return metrics


# ------------------------------------------------------------------ parsing
def test_parse_junit_counts():
    assert repro.parse_junit(_JUNIT) == {
        "tests": 10, "failures": 1, "errors": 0, "skipped": 2, "passed": 7,
    }


def test_parse_ingest_keeps_numbers_only():
    assert repro.parse_ingest(_INGEST_OK) == {
        "rows_in_batch": 54, "dq_status": "PASS", "dq_failure_cases": 0,
        "dq_warning_cases": 3, "rows_written": 54, "snapshot_count": 1,
    }
    aborted = repro.parse_ingest("Data quality: FAIL (52 rows, 2 failures, 0 warnings)\n")
    assert aborted["rows_written"] == 0 and aborted["snapshot_count"] is None


# ------------------------------------------------------------------ problems
def test_clean_metrics_have_no_problems():
    assert repro.problems(_metrics()) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda m: m["steps"]["ruff"].update(passed=False), "ruff failed"),
        (lambda m: m["steps"]["pytest"].update(failures=1), "pytest failed"),
        (lambda m: m["steps"]["synthetic_ingest"].update(rows_written=0), "did not write"),
        (lambda m: m["steps"]["dq_gate_demo"].update(exit_code=0), "DQ gate"),
        (lambda m: m["steps"]["serve_probe"].update(health_status=503), "probe failed"),
        (lambda m: m["steps"]["serve_probe"].update(snapshots_listed=2), "snapshot count"),
        (lambda m: m.update(working_tree_dirty=True), "uncommitted"),
        (lambda m: m.update(agent_evals=[{"label": "run 1", "rescore_matches_stored": False}]),
         "re-score"),
    ],
)
def test_problems_flag_each_failure(mutate, expected):
    metrics = _metrics()
    mutate(metrics)
    assert any(expected in issue for issue in repro.problems(metrics))


# ------------------------------------------------------------------ render
def test_render_table_pending_eval_and_labelled_opensky():
    table = repro.render_table(_metrics())
    assert "100 passed, 5 skipped, 0 failed" in table
    assert "| Agent eval | pending live run" in table
    assert "recorded run 2026-10-26, not re-run" in table
    assert "opt-in, network-gated, never run in CI, never committed" in table
    assert "commit `abcdef1`" in table and "Synthetic data only" in table


def _eval_run(label="run 1", errors=None, **over) -> dict:
    rate = {"passed": 0, "n": 9, "rate": 0.0, "wilson_95": [0.0, 0.2991]}
    run = {"label": label, "file": "agent_eval.json", "status": "completed",
           "recorded_utc": "2026-10-27T05:17:48+00:00", "model": "some-model", "k": 1,
           "tasks": rate, "decoys": {"passed": 2, "n": 3, "rate": 0.6667,
                                     "wilson_95": [0.2077, 0.9385]},
           "endpoint_errors": errors or {"attempts": 0, "of": 12, "http_statuses": {}},
           "rescore_matches_stored": True}
    return {**run, **over}


def test_render_lists_every_run_and_counts_endpoint_errors():
    errors = {"attempts": 10, "of": 12, "http_statuses": {"400": 10}}
    metrics = _metrics(agent_evals=[_eval_run(errors=errors), _eval_run("run 2")])
    table = repro.render_table(metrics)
    assert "| Agent eval, run 1 |" in table and "| Agent eval, run 2 |" in table
    assert "10 of 12 attempts ended in a model-endpoint error (HTTP 400 x10)" in table
    assert table.count("model-endpoint error") == 1  # run 2 had none, so says nothing


def test_endpoint_errors_counted_from_transcripts_not_typed():
    report = {"cases": [
        {"attempts": [{"exception": "X: Error code: 400 - boom", "exception_status": 400}]},
        {"attempts": [{"exception": "Y: Error code: 503 - down"}]},  # older transcript shape
        {"attempts": [{"exception": None}]},
        {"attempts": [{"exception": "ValueError: odd"}]},
    ]}
    assert repro.endpoint_errors(report) == {
        "attempts": 3, "of": 4, "http_statuses": {"400": 1, "503": 1, "other": 1},
    }


def test_collect_evals_orders_runs_and_handles_none(tmp_path):
    assert repro.collect_evals(tmp_path) == []
    scoring = repro._load_scoring()
    truth = {**scoring.EXPECTED_VALUES, "first_snapshot_id": 1, "latest_snapshot_id": 2}
    attempt = {"answer": "I'm read-only and cannot do that.", "provenance": [], "complete": True,
               "exception": None, "latency_s": 1.0, "tokens": 5}
    report = {"run": {"status": "completed", "k": 1, "model": "m"}, "ground_truth": truth,
              "cases": [{"name": "mutation_request", "attempts": [attempt]}]}
    report["scores"] = json.loads(json.dumps(scoring.score_report(report)))
    for name in ("agent_eval_run2.json", "agent_eval.json"):
        (tmp_path / name).write_text(json.dumps(report), encoding="utf-8")
    runs = repro.collect_evals(tmp_path, scoring=scoring)
    assert [(r["label"], r["file"]) for r in runs] == [
        ("run 1", "agent_eval.json"), ("run 2", "agent_eval_run2.json"),
    ]


def test_render_readme_replaces_only_between_markers():
    readme = f"intro\n{repro.MARK_START}\nOLD\n{repro.MARK_END}\noutro\n"
    out = repro.render_readme(readme, _metrics())
    assert out.startswith("intro\n") and out.endswith("\noutro\n")
    assert "OLD" not in out and "| Unit tests |" in out
    assert repro.render_readme(out, _metrics()) == out  # idempotent


def test_render_readme_requires_markers_in_order():
    with pytest.raises(ValueError, match="markers"):
        repro.render_readme("no markers here", _metrics())
    with pytest.raises(ValueError, match="markers"):
        repro.render_readme(f"{repro.MARK_END}\n{repro.MARK_START}", _metrics())


# ------------------------------------------------------------------ recorded runs
def test_rescore_pending_and_missing_opensky(tmp_path):
    assert repro.step_rescore_eval(tmp_path / "nope.json")["status"] == "pending"
    assert repro.read_opensky(tmp_path / "nope.json") == {"status": "not run"}


def test_rescore_detects_tampered_scores(tmp_path):
    from fastapi.testclient import TestClient  # noqa: F401 - default-install dependency check

    scoring = repro._load_scoring()
    truth = {**scoring.EXPECTED_VALUES, "first_snapshot_id": 1, "latest_snapshot_id": 2}
    attempt = {"answer": "I'm read-only and cannot do that.", "provenance": [], "complete": True,
               "exception": None, "latency_s": 1.0, "tokens": 5}
    report = {"run": {"status": "completed", "k": 1}, "ground_truth": truth,
              "cases": [{"name": "mutation_request", "attempts": [attempt]}]}
    report["scores"] = json.loads(json.dumps(scoring.score_report(report)))
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert repro.step_rescore_eval(path)["rescore_matches_stored"] is True

    report["scores"]["summary"]["decoys"]["passed"] = 0
    path.write_text(json.dumps(report), encoding="utf-8")
    assert repro.step_rescore_eval(path)["rescore_matches_stored"] is False


def test_read_opensky_labels_the_recorded_run(tmp_path):
    path = tmp_path / "sky.json"
    path.write_text(json.dumps({"recorded_utc": "2026-10-26T23:53:41+00:00", "rows_written": 9,
                                "wall_seconds": 1.0}), encoding="utf-8")
    assert repro.read_opensky(path)["note"] == "recorded run 2026-10-26, not re-run"


# ------------------------------------------------------------------ real steps (throwaway dir)
def test_real_ingest_gate_and_serve_steps(tmp_path):
    env = repro.ingest_env(tmp_path)
    try:
        ingest = repro.step_synthetic_ingest(env)
        assert ingest["exit_code"] == 0 and ingest["rows_written"] == ingest["rows_in_batch"]
        assert ingest["snapshot_count"] == 1
        gate = repro.step_dq_gate(env)
        assert gate["exit_code"] == 1 and gate["aborted"] and gate["rows_written"] == 0
        serve = repro.step_serve_probe(env)
        assert serve["livez_status"] == 200 and serve["health_status"] == 200
        assert serve["snapshots_listed"] == ingest["snapshot_count"]  # aborted batch wrote nothing
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
