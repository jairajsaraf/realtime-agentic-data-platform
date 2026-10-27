"""Offline tests for deploy/hf-space/app.py (default install: gradio is NOT needed)."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[1] / "deploy" / "hf-space" / "app.py"
_spec = importlib.util.spec_from_file_location("hf_space_app", _APP)
space = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = space
_spec.loader.exec_module(space)


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    instance = space.Backend(tmp_path_factory.mktemp("space"))
    yield instance
    instance.close()


def _total(result: dict) -> int:
    return sum(bucket["count"] for bucket in result["response"]["buckets"])


# ------------------------------------------------------------------ ZeroGPU startup stub
def test_startup_stub_is_inert_without_the_spaces_package():
    pytest.importorskip("fastapi")
    if "spaces" in sys.modules and sys.modules["spaces"] is not None:
        pytest.skip("the real `spaces` package is installed here")
    assert space._startup_check_stub() is None  # decorator fell back to a pass-through
    assert space._gpu(lambda: 1)() == 1
    assert space._gpu(duration=1)(lambda: 2)() == 2


# ------------------------------------------------------------------ backend over the real routes
def test_health_and_livez(backend):
    health = backend.call("/health")
    assert health["status"] == 200 and health["response"]["status"] == "ok"
    assert health["endpoint"] == "GET /health"
    assert backend.call("/livez")["status"] == 200


def test_two_snapshots_and_counts(backend):
    snapshots = backend.call("/snapshots")["response"]
    assert len(snapshots) == len(space.SEEDS) == 2
    latest = backend.call("/stats/flights-per-interval", interval="day")
    assert latest["endpoint"] == "GET /stats/flights-per-interval?interval=day"
    assert _total(latest) == sum(b["rows_written"] for b in backend.ingest_summary)


def test_time_travel_reads_the_first_snapshot(backend):
    first = backend.snapshot_choices()[1]
    older = backend.call(
        "/stats/flights-per-interval", interval="day", as_of_snapshot_id=int(first)
    )
    assert _total(older) == backend.ingest_summary[0]["rows_written"]
    assert f"as_of_snapshot_id={first}" in older["endpoint"]


def test_group_by_country_and_flights(backend):
    grouped = backend.call("/stats/flights-per-interval", interval="day", group_by="origin_country")
    assert {b["group"] for b in grouped["response"]["buckets"]}  # at least one country
    rows = backend.call("/flights", limit=3)
    assert rows["status"] == 200 and rows["response"]["count"] == 3


def test_none_params_are_dropped_and_errors_are_reported_not_raised(backend):
    assert backend.call("/flights", limit=1, offset=None)["endpoint"] == "GET /flights?limit=1"
    missing = backend.call("/snapshots/does-not-exist")
    assert missing["status"] == 404
    bad = backend.call("/flights", as_of_snapshot_id=1)  # unknown snapshot id
    assert bad["status"] == 404


def test_dq_summary_comes_from_the_ingest_and_says_so(backend):
    summary = backend.dq_summary()
    assert "not an API endpoint" in summary["endpoint"]
    batches = summary["response"]["batches"]
    assert [b["seed"] for b in batches] == list(space.SEEDS)
    assert all(b["dq_failure_cases"] == 0 and b["rows_written"] == b["rows_in_batch"]
               for b in batches)


def test_concurrent_calls_are_safe(backend):
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: backend.call("/health")["status"], range(24)))
    assert statuses == [200] * 24


def test_snapshot_choice_parsing():
    assert space._as_snapshot_id(None) is None
    assert space._as_snapshot_id(space.LATEST) is None
    assert space._as_snapshot_id("123") == 123


def test_close_removes_the_workdir(tmp_path):
    instance = space.Backend(tmp_path / "w")
    instance.close()
    assert not (tmp_path / "w").exists()
    instance.close()  # a second close is a no-op


# ------------------------------------------------------------------ boundary: API only
_ALLOWED_RTDP = {"rtdp.api.app", "rtdp.config", "rtdp.ingest", "rtdp.sources.synthetic"}
_FORBIDDEN_TOP = {"duckdb", "pyiceberg", "boto3", "pandas", "pandera", "pyarrow"}


def test_ui_reaches_data_only_through_the_api():
    """The Space may build the warehouse and the app, but it must not query the lakehouse itself."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(_APP.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    for module in imported:
        top = module.partition(".")[0]
        assert top not in _FORBIDDEN_TOP, module
        if top == "rtdp":
            assert module in _ALLOWED_RTDP, f"{module} is not an allowed import for the Space"


def test_space_config_front_matter():
    text = (_APP.parent / "README.md").read_text(encoding="utf-8")
    head = text.split("---")[1]
    config = dict(line.split(": ", 1) for line in head.strip().splitlines())
    assert config["sdk"] == "gradio" and config["app_file"] == "app.py"
    assert config["sdk_version"].strip('"') == "6.30.0"
    assert config["python_version"].strip('"') == "3.12.12"
    assert "synthetic data only" in text.lower()


def _locked_version(package: str) -> str:
    lock = (Path(__file__).resolve().parents[1] / "uv.lock").read_text(encoding="utf-8")
    match = re.search(rf'name = "{re.escape(package)}"\nversion = "([^"]+)"', lock)
    assert match, f"{package} not found in uv.lock"
    return match[1]


def test_requirements_pin_pyiceberg_to_the_lock_and_keep_the_commit_placeholder():
    lines = [ln for ln in (_APP.parent / "requirements.txt").read_text().splitlines()
             if ln and not ln.startswith("#")]
    assert lines == [
        f"pyiceberg=={_locked_version('pyiceberg')}",
        "rtdp @ git+https://github.com/jairajsaraf/realtime-agentic-data-platform"
        "@__RTDP_COMMIT_SHA__",
    ]


# ------------------------------------------------------------------ UI (only with gradio)
def test_ui_builds_and_exposes_named_endpoints(backend):
    pytest.importorskip("gradio")
    demo = space.build_ui(backend)
    names = {fn.api_name for fn in demo.fns.values() if fn.api_name}
    assert {"health", "livez", "snapshots", "dq", "counts", "flights"} <= names
