"""Preregistered live evaluation of the Stage D agent (opt-in; network-gated; never run in CI).

The case list, pass criteria, bars, k, and stop rules are frozen in scripts/agent_eval_scoring.py
(PREREGISTRATION) before any live call, and copied verbatim into the report. Data is synthetic
only: two seeded ingests into a throwaway file:// warehouse, served in-process through
fastapi.testclient.TestClient (the same read API routes over HTTP semantics; no server, no
network). The model endpoint is the only live dependency.

    # 1) no model calls: show the preregistration and the ground truth it will be scored against
    uv run --extra agent python scripts/eval_agent.py --dry-run

    # 2) live run (needs the [agent] extra and an OpenAI-compatible endpoint; never commit a key)
    #    RTDP_AGENT_BASE_URL, RTDP_AGENT_MODEL, RTDP_AGENT_API_KEY set in the environment
    uv run --extra agent python scripts/eval_agent.py --pause-seconds 10

Writes results/agent_eval.json: preregistration, run metadata (model id as listed by
GET {base}/models, date, package versions, commit), ground truth, per-case transcripts, and
scores. No keys or raw request payloads are written. Re-score offline with
``agent_eval_scoring.score_report``.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_eval_scoring as scoring  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = REPO_ROOT / "results" / "agent_eval.json"
API_BASE = "http://testserver"  # TestClient's base URL: in-process, no network

# Deterministic values from the seeded synthetic data (see scoring.DATASET). Frozen with the
# preregistration; the run aborts before any model call if the data does not reproduce them.
FROZEN_EXPECTED: dict = scoring.EXPECTED_VALUES


# --------------------------------------------------------------------------- dataset + truth


def build_dataset(workdir: Path):
    """Two seeded synthetic ingests into a throwaway file:// warehouse; returns Settings."""
    from rtdp.config import Settings
    from rtdp.ingest import run_ingest
    from rtdp.sources.synthetic import SyntheticSource

    settings = Settings(
        storage_backend="file",
        local_warehouse_dir=workdir / "warehouse",
        catalog_db_path=workdir / "warehouse" / "catalog.db",
        agent_api_url=API_BASE,
    )
    for batch in scoring.DATASET["ingests"]:
        result = run_ingest(
            settings, SyntheticSource(base_date=scoring.DATASET["base_date"], **batch)
        )
        if not result.dq.ok:
            raise RuntimeError("synthetic ingest unexpectedly failed its DQ gate")
    return settings


def _get(client, path: str, **params):
    resp = client.get(f"{API_BASE}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def compute_ground_truth(client) -> dict:
    """Expected answers, read straight from the read API (not through the agent)."""
    snapshots = _get(client, "/snapshots")
    first_id, latest_id = snapshots[0]["snapshot_id"], snapshots[-1]["snapshot_id"]

    def total(**params) -> int:
        stats = _get(client, "/stats/flights-per-interval", interval="day", **params)
        return sum(bucket["count"] for bucket in stats["buckets"])

    by_country: dict[str, int] = {}
    stats = _get(client, "/stats/flights-per-interval", interval="day", group_by="origin_country")
    for bucket in stats["buckets"]:
        by_country[bucket["group"]] = by_country.get(bucket["group"], 0) + bucket["count"]
    best = max(by_country.values())
    top = sorted(country for country, count in by_country.items() if count == best)

    day = _get(client, "/stats/flights-per-interval", interval="day")["buckets"]
    day_rows = sum(b["count"] for b in day if b["bucket_start"].startswith(scoring.DAY_UNDER_TEST))
    callsign_rows = _get(client, "/flights", callsign=scoring.WARN_CALLSIGN, limit=1000)["items"]
    return {
        "total_rows": total(),
        "top_country": top[0] if len(top) == 1 else top,
        "callsign_icao24s": sorted({row["icao24"] for row in callsign_rows}),
        "bbox_rows": _get(client, "/flights/bbox", limit=1000, **scoring.BBOX)["count"],
        "day_rows": day_rows,
        "duplicate_icao24_rows": _get(
            client, "/flights", icao24=scoring.DUPLICATE_ICAO24, limit=1000
        )["count"],
        "snapshot_count": len(snapshots),
        "first_snapshot_id": first_id,
        "latest_snapshot_id": latest_id,
        "first_snapshot_rows": total(as_of_snapshot_id=first_id),
    }


def check_frozen(truth: dict) -> list[str]:
    """Mismatches between the run's ground truth and the frozen expected values."""
    return [
        f"{key}: expected {want!r}, got {truth.get(key)!r}"
        for key, want in FROZEN_EXPECTED.items()
        if truth.get(key) != want
    ]


# --------------------------------------------------------------------------- run


class AbortRun(Exception):
    """A preregistered stop rule fired (quota/rate limit or auth)."""


def is_abort_error(exc: BaseException) -> bool:
    """Stop rule: quota/rate-limit (429, RESOURCE_EXHAUSTED) or auth (401/403) aborts the run."""
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 429):
        return True
    return "RESOURCE_EXHAUSTED" in str(exc) or type(exc).__name__ in (
        "RateLimitError",
        "AuthenticationError",
        "PermissionDeniedError",
    )


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def run_case(question: str, *, settings, llm, executor, tools) -> dict:
    """One attempt; returns the stored transcript (never raises except AbortRun)."""
    from rtdp.agent.loop import run_agent

    tool_calls: list[dict] = []

    def on_event(kind: str, payload) -> None:
        if kind == "tool_call":
            tool_calls.append({"name": payload.get("name"), "args": payload.get("args")})

    start = time.perf_counter()
    try:
        result = run_agent(
            question, llm=llm, executor=executor, tools=tools,
            max_turns=settings.agent_max_turns, max_tool_calls=settings.agent_max_tool_calls,
            on_event=on_event,
        )
    except Exception as exc:  # noqa: BLE001 — every model-side failure is recorded
        if is_abort_error(exc):
            raise AbortRun(_short(exc)) from exc
        return {
            "answer": "", "provenance": [], "tool_calls": tool_calls, "turns": None,
            "complete": False, "error": None, "tokens": None,
            "latency_s": round(time.perf_counter() - start, 3), "exception": _short(exc),
        }
    return {
        "answer": result.answer,
        "provenance": [vars(p) for p in result.provenance],
        "citation_line": result.citation_line(),
        "tool_calls": tool_calls,
        "turns": result.turns,
        "complete": result.complete,
        "error": result.error,
        "tokens": result.tokens,
        "latency_s": round(time.perf_counter() - start, 3),
        "exception": None,
    }


def run_eval(settings, client, llm, *, truth: dict, k: int, pause_s: float = 0.0) -> dict:
    """Run every case k times in order; stops early only on a preregistered abort."""
    from rtdp.agent.runtime import build_registry
    from rtdp.agent.tools import ApiToolExecutor

    registry = build_registry(settings, client)
    executor = ApiToolExecutor(API_BASE, client, registry, max_rows=settings.agent_max_rows)
    tools = registry.definitions()
    cases: list[dict] = []
    status, abort_reason = "completed", None
    try:
        for index, case in enumerate(scoring.CASES):
            question = case["question"].format(**truth)
            entry = {"name": case["name"], "kind": case["kind"], "question": question,
                     "attempts": []}
            cases.append(entry)
            for _ in range(k):
                entry["attempts"].append(
                    run_case(question, settings=settings, llm=llm, executor=executor, tools=tools)
                )
            if pause_s and index < len(scoring.CASES) - 1:
                time.sleep(pause_s)
    except AbortRun as exc:
        status, abort_reason = "aborted", str(exc)
        if cases and not cases[-1]["attempts"]:
            cases.pop()  # the aborted case has no completed attempt to score
    report = {"ground_truth": truth, "cases": cases}
    return {
        "status": status,
        "abort_reason": abort_reason,
        "cases": cases,
        "scores": scoring.score_report(report),
    }


# --------------------------------------------------------------------------- metadata


def _versions() -> dict:
    names = ("langchain", "langchain-core", "langchain-openai", "langgraph", "openai")
    found = {}
    for name in names:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = None
    return found


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def model_listing(settings) -> dict:
    """Whether the configured model appears in GET {base}/models (one live, read-only call)."""
    import httpx

    key = settings.agent_api_key
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = httpx.get(
        f"{settings.agent_base_url.rstrip('/')}/models", headers=headers,
        timeout=settings.agent_timeout_seconds,
    )
    resp.raise_for_status()
    ids = [m.get("id", "") for m in resp.json().get("data", [])]
    wanted = settings.agent_model
    match = next((i for i in ids if i in (wanted, f"models/{wanted}")), None)
    return {"listed": match is not None, "listed_id": match, "models_returned": len(ids)}


def run_metadata(settings, *, k: int, pause_s: float) -> dict:
    return {
        "date_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit_sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "model": settings.agent_model,
        "endpoint_host": urlparse(settings.agent_base_url or "").hostname,
        "temperature": settings.agent_temperature,
        "max_turns": settings.agent_max_turns,
        "max_tool_calls": settings.agent_max_tool_calls,
        "k": k,
        "pause_seconds_between_cases": pause_s,
        "python": platform.python_version(),
        "packages": _versions(),
        "data": "synthetic only",
    }


# --------------------------------------------------------------------------- main


def _remove_workdir(path: Path) -> None:
    """Delete the throwaway warehouse without ever failing the run.

    On Windows SQLite keeps catalog.db open until the catalog's engine is garbage-collected, and
    an open file cannot be deleted; collect first, then remove what can be removed.
    """
    gc.collect()
    shutil.rmtree(path, ignore_errors=True)


def _evaluate(workdir: Path, args, llm, k: int):
    """Build the dataset, check it, and run the cases; returns an exit code or the results."""
    from fastapi.testclient import TestClient

    from rtdp.api.app import create_app

    settings = build_dataset(workdir)
    with TestClient(create_app(settings), base_url=API_BASE) as client:
        truth = compute_ground_truth(client)
        mismatches = check_frozen(truth)
        if args.dry_run:
            print(json.dumps({"preregistration": scoring.PREREGISTRATION,
                              "ground_truth": truth, "frozen_mismatches": mismatches},
                             indent=2, default=str))
            return 0 if not mismatches else 2
        if mismatches:
            print("Synthetic data does not reproduce the frozen values; aborting before any "
                  "model call:\n  " + "\n  ".join(mismatches), file=sys.stderr)
            return 2
        if llm is None:
            if not settings.agent_base_url or not settings.agent_model:
                print("Live eval requires RTDP_AGENT_BASE_URL and RTDP_AGENT_MODEL (and "
                      "RTDP_AGENT_API_KEY if the endpoint needs one). No calls made.",
                      file=sys.stderr)
                return 2
            from rtdp.agent.runtime import build_llm_client

            listing = model_listing(settings)
            llm = build_llm_client(settings)
        else:
            listing = {"listed": None, "listed_id": None, "models_returned": None}
        meta = run_metadata(settings, k=k, pause_s=args.pause_seconds)
        meta["model_listing"] = listing
        outcome = run_eval(settings, client, llm, truth=truth, k=k, pause_s=args.pause_seconds)
    return meta, truth, outcome


def main(argv: list[str] | None = None, *, llm=None) -> int:
    parser = argparse.ArgumentParser(description="Preregistered live eval for the Stage D agent")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the preregistration + ground truth; make no model calls")
    parser.add_argument("--pause-seconds", type=float, default=0.0,
                        help="pause between cases (rate-limit pacing; not counted in latency)")
    args = parser.parse_args(argv)
    k = scoring.PREREGISTRATION["k"]

    workdir = Path(tempfile.mkdtemp(prefix="rtdp-eval-"))
    try:
        result = _evaluate(workdir, args, llm, k)
    finally:
        _remove_workdir(workdir)
    if isinstance(result, int):
        return result
    meta, truth, outcome = result

    report = {
        "preregistration": scoring.PREREGISTRATION,
        "run": meta | {"status": outcome["status"], "abort_reason": outcome["abort_reason"]},
        "ground_truth": truth,
        "cases": outcome["cases"],
        "scores": outcome["scores"],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    summary = outcome["scores"]["summary"]
    print(f"status={outcome['status']} tasks={summary['tasks']} decoys={summary['decoys']}")
    for name, scores in outcome["scores"]["per_case"].items():
        print(f"  {'PASS' if all(s['passed'] for s in scores) else 'FAIL'}  {name}")
    print(f"Wrote {args.report}")
    if outcome["status"] == "aborted":
        print(f"ABORTED (preregistered stop rule): {outcome['abort_reason']}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
