"""Offline tests for the preregistered eval harness (scripts/eval_agent.py).

The live run gets one shot under a free-tier quota, so the harness itself is exercised here with
a scripted model: real synthetic dataset, real read API in-process, real scoring. No network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"
_spec = importlib.util.spec_from_file_location("eval_agent_harness", _PATH)
harness = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = harness
_spec.loader.exec_module(harness)


@pytest.fixture
def served(tmp_path):
    from fastapi.testclient import TestClient

    from rtdp.api.app import create_app

    settings = harness.build_dataset(tmp_path)
    with TestClient(create_app(settings), base_url=harness.API_BASE) as client:
        yield settings, client, harness.compute_ground_truth(client)


def test_synthetic_data_reproduces_the_frozen_values(served):
    _, _, truth = served
    assert harness.check_frozen(truth) == []
    assert truth["first_snapshot_id"] != truth["latest_snapshot_id"]


_DEFAULT_ANSWER = "The table has 108 state vectors. I am read-only and cannot change it."


def _scripted(answer: str = _DEFAULT_ANSWER):
    """First turn calls the stats tool; the turn after a tool result answers with `answer`."""
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.messages.tool import tool_call

    from rtdp.agent.llm import ScriptedChatModel

    def respond(messages):
        if isinstance(messages[-1], HumanMessage):
            return AIMessage(
                content="",
                tool_calls=[tool_call(name="stats_flights_per_interval",
                                      args={"interval": "day"}, id="c1")],
                usage_metadata={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10},
            )
        return AIMessage(content=answer)

    return ScriptedChatModel(respond)


def test_run_eval_end_to_end_offline(served):
    pytest.importorskip("langchain", reason="requires the optional [agent] extra")
    settings, client, truth = served
    outcome = harness.run_eval(settings, client, _scripted(), truth=truth, k=1)

    assert outcome["status"] == "completed"
    assert [c["name"] for c in outcome["cases"]] == [c["name"] for c in harness.scoring.CASES]
    first = outcome["cases"][0]["attempts"][0]
    assert first["tool_calls"] == [{"name": "stats_flights_per_interval",
                                    "args": {"interval": "day"}}]
    assert first["provenance"][0]["endpoint"] == "GET /stats/flights-per-interval"
    per_case = outcome["scores"]["per_case"]
    assert per_case["total_count"][0]["passed"] is True
    assert per_case["bbox_count"][0]["passed"] is False  # wrong tool, wrong number
    assert per_case["mutation_request"][0]["passed"] is True
    # The stored transcripts re-score to the same verdicts offline.
    rescored = harness.scoring.score_report({"ground_truth": truth, "cases": outcome["cases"]})
    assert rescored == outcome["scores"]


def test_rate_limit_aborts_the_whole_run(served):
    pytest.importorskip("langchain", reason="requires the optional [agent] extra")
    import openai
    from langchain_core.messages import AIMessage

    from rtdp.agent.llm import ScriptedChatModel

    settings, client, truth = served
    calls = {"n": 0}

    def respond(_messages):
        calls["n"] += 1
        if calls["n"] == 3:
            request = httpx.Request("POST", "http://llm.test/v1/chat/completions")
            raise openai.RateLimitError(
                "quota", response=httpx.Response(429, request=request), body=None
            )
        return AIMessage(content="I cannot answer that.")

    outcome = harness.run_eval(settings, client, ScriptedChatModel(respond), truth=truth, k=1)
    assert outcome["status"] == "aborted"
    assert "RateLimitError" in outcome["abort_reason"]
    assert len(outcome["cases"]) == 2  # the aborted case has no attempt and is dropped


def test_other_model_errors_fail_the_attempt_and_the_run_continues(served):
    pytest.importorskip("langchain", reason="requires the optional [agent] extra")
    from langchain_core.messages import AIMessage

    from rtdp.agent.llm import ScriptedChatModel

    settings, client, truth = served
    calls = {"n": 0}

    def respond(_messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("upstream returned garbage")
        return AIMessage(content="I cannot answer that.")

    outcome = harness.run_eval(settings, client, ScriptedChatModel(respond), truth=truth, k=1)
    assert outcome["status"] == "completed"
    assert outcome["cases"][0]["attempts"][0]["exception"].startswith("ValueError")
    assert len(outcome["cases"]) == len(harness.scoring.CASES)


def test_main_without_model_config_makes_no_calls(capsys, tmp_path):
    report = tmp_path / "eval.json"
    assert harness.main(["--report", str(report)]) == 2
    assert "No calls made" in capsys.readouterr().err
    assert not report.exists()


def test_main_writes_the_report_with_preregistration(tmp_path):
    pytest.importorskip("langchain", reason="requires the optional [agent] extra")
    report = tmp_path / "eval.json"
    assert harness.main(["--report", str(report)], llm=_scripted()) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["preregistration"]["version"] == harness.scoring.PREREGISTRATION_VERSION
    assert data["run"]["status"] == "completed"
    assert data["run"]["data"] == "synthetic only"
    assert data["run"]["k"] == 1
    assert "api_key" not in json.dumps(data["run"]).lower()
    assert len(data["cases"]) == 12


def test_dry_run_prints_preregistration_and_truth(capsys):
    assert harness.main(["--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["frozen_mismatches"] == []
    assert printed["ground_truth"]["total_rows"] == 108


def test_main_removes_its_workdir_and_tolerates_locked_files(monkeypatch, tmp_path):
    # Windows cannot delete a SQLite file that is still open; cleanup must never fail the run.
    monkeypatch.setattr(harness.tempfile, "tempdir", str(tmp_path))
    real_rmtree = harness.shutil.rmtree
    calls = []

    def locked_rmtree(path, ignore_errors=False):
        calls.append(ignore_errors)
        if not ignore_errors:
            raise PermissionError("file in use")
        real_rmtree(path, ignore_errors=True)

    monkeypatch.setattr(harness.shutil, "rmtree", locked_rmtree)
    assert harness.main(["--dry-run"]) == 0
    assert calls == [True]
    assert not list(tmp_path.glob("rtdp-eval-*"))
