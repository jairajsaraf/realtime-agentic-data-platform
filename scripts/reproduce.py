"""Reproduce every measured number in the README from code (stdlib only; makes NO live calls).

    uv run --extra agent python scripts/reproduce.py            # run the checks, write metrics
    uv run --extra agent python scripts/reproduce.py --render   # fill README from metrics.json

The run executes, on this machine: ruff, the pytest suite (junit XML parsed into counts), a
synthetic ingest into a throwaway warehouse, the DQ-gate demo (`--inject-failures`: exit code 1 and
nothing written), and a real `rtdp serve` probed over HTTP (/livez, /health, /snapshots). It then
re-scores a recorded agent eval offline and merges a recorded OpenSky run, both read from files and
labelled as recorded. Nothing here calls a model or OpenSky.

Output: results/metrics.json, stamped with the commit it ran on. The file is generated at one
commit and committed in the next, so its `commit_sha` names the code it measured, not the commit
that holds it. `--render` replaces the text between ``<!-- results:start -->`` and
``<!-- results:end -->`` in README.md with a table built only from metrics.json.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results"
METRICS_PATH = RESULTS_DIR / "metrics.json"
EVAL_PATH = RESULTS_DIR / "agent_eval.json"
OPENSKY_PATH = RESULTS_DIR / "opensky_live_run.json"
README_PATH = REPO_ROOT / "README.md"
MARK_START, MARK_END = "<!-- results:start -->", "<!-- results:end -->"
SYNTHETIC_ROWS = 50

_DQ_LINE = re.compile(
    r"Data quality: (?P<status>PASS|FAIL) \((?P<rows>\d+) rows, (?P<failures>\d+) failures, "
    r"(?P<warnings>\d+) warnings\)"
)
_WROTE = re.compile(r"^Wrote (?P<rows>\d+) rows to ", re.MULTILINE)
_SNAPSHOT_COUNT = re.compile(r"snapshot_count\s*:\s*(?P<n>\d+)")


# --------------------------------------------------------------------------- parsing


def parse_junit(xml_text: str) -> dict:
    """Test counts from a pytest junit XML document."""
    root = ET.fromstring(xml_text)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0))
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return {**totals, "passed": passed}


def parse_ingest(stdout: str) -> dict:
    dq = _DQ_LINE.search(stdout)
    wrote = _WROTE.search(stdout)
    snaps = _SNAPSHOT_COUNT.search(stdout)
    return {
        "rows_in_batch": int(dq["rows"]) if dq else None,
        "dq_status": dq["status"] if dq else None,
        "dq_failure_cases": int(dq["failures"]) if dq else None,
        "dq_warning_cases": int(dq["warnings"]) if dq else None,
        "rows_written": int(wrote["rows"]) if wrote else 0,
        "snapshot_count": int(snaps["n"]) if snaps else None,
    }


# --------------------------------------------------------------------------- running


def _run(cmd: list[str], *, env: dict | None = None, timeout: float = 900.0):
    start = time.perf_counter()
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    return proc, round(time.perf_counter() - start, 3)


def _uv() -> str:
    found = shutil.which("uv")
    if not found:
        raise SystemExit("uv is required: https://docs.astral.sh/uv/")
    return found


def step_ruff() -> dict:
    proc, seconds = _run([_uv(), "run", "--frozen", "ruff", "check", "."])
    return {"exit_code": proc.returncode, "passed": proc.returncode == 0, "seconds": seconds}


def step_pytest(extra: str = "agent") -> dict:
    with tempfile.TemporaryDirectory(prefix="rtdp-junit-") as tmp:
        junit = Path(tmp) / "junit.xml"
        cmd = [_uv(), "run", "--frozen", "--extra", extra, "--", "pytest", "-m", "not localstack",
               f"--junitxml={junit}", "-q", "-p", "no:cacheprovider"]
        proc, seconds = _run(cmd)
        counts = parse_junit(junit.read_text(encoding="utf-8")) if junit.exists() else {}
    return {"command": f"pytest -m 'not localstack' (uv --extra {extra})",
            "exit_code": proc.returncode, "seconds": seconds, **counts}


def ingest_env(workdir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RTDP_")}
    env.update(
        RTDP_STORAGE_BACKEND="file",
        RTDP_LOCAL_WAREHOUSE_DIR=str(workdir / "warehouse"),
        RTDP_CATALOG_DB_PATH=str(workdir / "warehouse" / "catalog.db"),
        RTDP_SOURCE="synthetic",
    )
    return env


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "rtdp.cli", *args]


def step_synthetic_ingest(env: dict) -> dict:
    proc, seconds = _run(_cli("ingest", "--source", "synthetic", "--rows", str(SYNTHETIC_ROWS)),
                         env=env)
    return {"command": f"rtdp ingest --source synthetic --rows {SYNTHETIC_ROWS}",
            "exit_code": proc.returncode, "seconds": seconds, **parse_ingest(proc.stdout)}


def step_dq_gate(env: dict) -> dict:
    proc, seconds = _run(
        _cli("ingest", "--source", "synthetic", "--rows", str(SYNTHETIC_ROWS), "--inject-failures"),
        env=env,
    )
    parsed = parse_ingest(proc.stdout)
    return {"command": "rtdp ingest --source synthetic --inject-failures",
            "exit_code": proc.returncode, "seconds": seconds,
            "dq_status": parsed["dq_status"], "dq_failure_cases": parsed["dq_failure_cases"],
            "rows_written": parsed["rows_written"],
            "aborted": "INGEST ABORTED" in (proc.stderr or "")}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, timeout: float = 5.0) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (loopback only)
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None


def step_serve_probe(env: dict, *, startup_timeout: float = 60.0) -> dict:
    """Start the real read API on a free loopback port and probe it over HTTP."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        _cli("serve"), cwd=REPO_ROOT, env={**env, "RTDP_API_PORT": str(port)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    result: dict = {"command": "rtdp serve (probed over HTTP on a loopback port)"}
    try:
        start = time.perf_counter()
        livez = None
        while time.perf_counter() - start < startup_timeout:
            try:
                livez = _get(f"{base}/livez", timeout=1.0)
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        result["seconds_to_livez"] = round(time.perf_counter() - start, 3)
        result["livez_status"] = livez[0] if livez else None
        if livez:
            health_status, health = _get(f"{base}/health")
            _, snapshots = _get(f"{base}/snapshots")
            result["health_status"] = health_status
            result["health_ok"] = bool(health) and health.get("status") == "ok"
            result["snapshots_listed"] = len(snapshots) if isinstance(snapshots, list) else None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return result


def _load_scoring():
    path = REPO_ROOT / "scripts" / "agent_eval_scoring.py"
    spec = importlib.util.spec_from_file_location("agent_eval_scoring", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def step_rescore_eval(path: Path = EVAL_PATH, *, scoring=None) -> dict:
    """Re-score a recorded eval offline; verify the stored scores match. No model call."""
    if not path.exists():
        return {"status": "pending", "note": "no recorded run yet (results/agent_eval.json)"}
    scoring = scoring or _load_scoring()
    report = json.loads(path.read_text(encoding="utf-8"))
    rescored = json.loads(json.dumps(scoring.score_report(report)))
    summary = rescored["summary"]
    run = report.get("run", {})
    return {
        "status": run.get("status"),
        "recorded_utc": run.get("date_utc"),
        "model": run.get("model"),
        "commit_sha": run.get("commit_sha"),
        "k": run.get("k"),
        "rescore_matches_stored": rescored == report.get("scores"),
        "endpoint_errors": endpoint_errors(report),
        "addenda": run.get("preregistration_addenda", []),
        "tasks": summary["tasks"],
        "decoys": summary["decoys"],
        "tool_correctness": summary["tool_correctness"],
        "citation_present": summary["citation_present"],
        "latency_s": summary["latency_s"],
        "tokens": summary["tokens"],
        "bars": summary["bars"],
        "note": "recorded run, re-scored offline from its stored transcripts; not re-run",
    }


_STATUS = re.compile(r"Error code: (\d{3})")


def endpoint_errors(report: dict) -> dict:
    """Attempts that ended in a model-endpoint error, counted from the stored transcripts."""
    attempts = [a for case in report.get("cases", []) for a in case.get("attempts", [])]
    failed = [a for a in attempts if a.get("exception")]
    statuses: dict[str, int] = {}
    for attempt in failed:
        status = attempt.get("exception_status")
        if status is None and (match := _STATUS.search(attempt["exception"])):
            status = int(match[1])
        key = str(status) if status is not None else "other"
        statuses[key] = statuses.get(key, 0) + 1
    return {"attempts": len(failed), "of": len(attempts), "http_statuses": statuses}


def collect_evals(results_dir: Path = RESULTS_DIR, *, scoring=None) -> list[dict]:
    """Every recorded eval run (results/agent_eval*.json, oldest name first), re-scored offline."""
    runs = []
    for index, path in enumerate(sorted(results_dir.glob("agent_eval*.json")), start=1):
        summary = step_rescore_eval(path, scoring=scoring)
        runs.append({"label": f"run {index}", "file": path.name, **summary})
    return runs


def read_opensky(path: Path = OPENSKY_PATH) -> dict:
    if not path.exists():
        return {"status": "not run"}
    data = json.loads(path.read_text(encoding="utf-8"))
    keep = ("recorded_utc", "exit_code", "wall_seconds", "rows_in_batch", "rows_written",
            "dq_status", "dq_failure_cases", "dq_warning_cases")
    return {"status": "recorded", **{k: data.get(k) for k in keep},
            "note": f"recorded run {str(data.get('recorded_utc', ''))[:10]}, not re-run"}


# --------------------------------------------------------------------------- metrics


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True,
                             text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def collect() -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="rtdp-reproduce-"))
    try:
        env = ingest_env(workdir)
        steps = {
            "ruff": step_ruff(),
            "pytest": step_pytest(),
            "synthetic_ingest": step_synthetic_ingest(env),
            "dq_gate_demo": step_dq_gate(env),
            "serve_probe": step_serve_probe(env),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    dirty = _git("status", "--porcelain", "--", ".", ":!results")
    return {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit_sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(dirty),
        "commit_note": ("generated at commit_sha and committed afterwards; the sha names the code "
                        "that was measured, not the commit that holds this file"),
        "machine": {"python": platform.python_version(), "os_family": platform.system(),
                    "cpu_count": os.cpu_count()},
        "data": "synthetic only",
        "steps": steps,
        "agent_evals": collect_evals(),
        "opensky_live": read_opensky(),
    }


def problems(metrics: dict) -> list[str]:
    """Reasons this run should not be trusted or committed; empty when it checks out."""
    steps = metrics["steps"]
    found = []
    if not steps["ruff"]["passed"]:
        found.append("ruff failed")
    py = steps["pytest"]
    if py.get("exit_code") != 0 or py.get("failures") or py.get("errors"):
        found.append("pytest failed")
    ingest = steps["synthetic_ingest"]
    if ingest["exit_code"] != 0 or ingest["rows_written"] != ingest["rows_in_batch"]:
        found.append("synthetic ingest did not write its batch")
    gate = steps["dq_gate_demo"]
    if gate["exit_code"] != 1 or not gate["aborted"] or gate["rows_written"] != 0:
        found.append("DQ gate did not abort the failing batch")
    serve = steps["serve_probe"]
    if serve.get("livez_status") != 200 or serve.get("health_status") != 200:
        found.append("read API probe failed")
    if serve.get("snapshots_listed") != ingest["snapshot_count"]:
        found.append("the aborted batch changed the snapshot count")
    if metrics["working_tree_dirty"]:
        found.append("working tree has uncommitted changes outside results/")
    for evaluation in metrics["agent_evals"]:
        if not evaluation.get("rescore_matches_stored"):
            found.append(f"agent eval {evaluation['label']} re-score does not match the stored "
                         "scores")
    return found


# --------------------------------------------------------------------------- render


def _pct(rate: dict) -> str:
    if not rate or rate.get("n") in (None, 0):
        return "n/a"
    lo, hi = rate["wilson_95"]
    return f"{rate['passed']}/{rate['n']} (Wilson 95% CI {lo:.2f} to {hi:.2f})"


def render_table(metrics: dict) -> str:
    steps = metrics["steps"]
    py, ingest, gate, serve = (steps[k] for k in
                               ("pytest", "synthetic_ingest", "dq_gate_demo", "serve_probe"))
    rows = [
        ("Unit tests", f"{py['passed']} passed, {py['skipped']} skipped, "
                       f"{py['failures'] + py['errors']} failed ({py['seconds']} s)"),
        ("Lint", "ruff clean" if steps["ruff"]["passed"] else "ruff FAILED"),
        ("Synthetic ingest", f"{ingest['rows_written']} rows written in {ingest['seconds']} s; "
                             f"DQ {ingest['dq_status']}, {ingest['dq_failure_cases']} failure and "
                             f"{ingest['dq_warning_cases']} warning cases"),
        ("DQ gate demo", f"batch with injected failures: exit code {gate['exit_code']}, "
                         f"{gate['rows_written']} rows written, snapshot count unchanged"),
        ("Read API", f"/livez {serve['livez_status']} after {serve['seconds_to_livez']} s, "
                     f"/health {serve['health_status']}, {serve['snapshots_listed']} snapshot(s)"),
    ]
    evals = metrics["agent_evals"]
    if not evals:
        rows.append(("Agent eval", "pending live run; the harness and preregistration are "
                                   "committed"))
    for evaluation in evals:
        when = str(evaluation["recorded_utc"])[:10]
        text = (f"{evaluation['model']}, {when}, k={evaluation['k']}, status "
                f"{evaluation['status']}: tasks {_pct(evaluation['tasks'])}, decoys "
                f"{_pct(evaluation['decoys'])}")
        errors = evaluation["endpoint_errors"]
        if errors["attempts"]:
            codes = ", ".join(f"HTTP {k} x{v}" for k, v in sorted(errors["http_statuses"].items()))
            text += (f"; {errors['attempts']} of {errors['of']} attempts ended in a model-endpoint "
                     f"error ({codes}) and count as failures")
        rows.append((f"Agent eval, {evaluation['label']}", text + " (recorded, re-scored offline)"))
    sky = metrics["opensky_live"]
    if sky.get("status") == "recorded":
        rows.append((
            "OpenSky live ingest",
            f"{sky['rows_written']} rows written in {sky['wall_seconds']} s; DQ "
            f"{sky['dq_status']}, {sky['dq_failure_cases']} failure and "
            f"{sky['dq_warning_cases']} warning cases ({sky['note']}; opt-in, network-gated, "
            "never run in CI, never committed)",
        ))
    machine = metrics["machine"]
    lines = ["| Check | Result |", "| --- | --- |", *(f"| {a} | {b} |" for a, b in rows), "",
             f"Generated by `scripts/reproduce.py` at commit `{str(metrics['commit_sha'])[:7]}` on "
             f"{machine['os_family']}, Python {machine['python']}, {machine['cpu_count']} CPUs "
             f"({metrics['generated_utc'][:10]}). Synthetic data only; timings come from one "
             "machine and vary."]
    return "\n".join(lines)


def render_readme(readme: str, metrics: dict) -> str:
    start, end = readme.find(MARK_START), readme.find(MARK_END)
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"README needs {MARK_START} and {MARK_END} markers, in that order")
    head = readme[: start + len(MARK_START)]
    return f"{head}\n{render_table(metrics)}\n{readme[end:]}"


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduce the README's measured numbers")
    parser.add_argument("--render", action="store_true",
                        help="fill README.md from results/metrics.json (runs nothing)")
    args = parser.parse_args(argv)

    if args.render:
        if not METRICS_PATH.exists():
            print(f"{METRICS_PATH} not found; run without --render first", file=sys.stderr)
            return 2
        metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        try:
            updated = render_readme(README_PATH.read_text(encoding="utf-8"), metrics)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        README_PATH.write_text(updated, encoding="utf-8")
        print(f"Updated {README_PATH.name} from {METRICS_PATH.name}")
        return 0

    metrics = collect()
    issues = problems(metrics)
    metrics["problems"] = issues
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(render_table(metrics))
    print(f"\nWrote {METRICS_PATH}")
    for issue in issues:
        print(f"PROBLEM: {issue}", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
