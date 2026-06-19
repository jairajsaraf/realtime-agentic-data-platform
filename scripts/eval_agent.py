"""Opt-in live evaluation harness for the Stage D agent.

NETWORK-GATED AND NEVER RUN IN CI. It makes real LLM calls and requires a running ``rtdp serve``.
It is not imported by any test. Run it manually only:

    # 1) seed a little data and serve it (separate shell)
    uv run rtdp ingest --rows 80
    uv run rtdp serve

    # 2) configure an OpenAI-compatible model endpoint, then run the harness
    $env:RTDP_AGENT_BASE_URL = "https://integrate.api.nvidia.com/v1"   # example: NVIDIA NIM
    $env:RTDP_AGENT_MODEL    = "meta/llama-3.1-8b-instruct"
    $env:RTDP_AGENT_API_KEY  = "<your key>"                            # never commit this
    uv run python scripts/eval_agent.py --report _agent_eval_report.json

Reports per-case grounding/faithfulness, tool-call correctness, latency, and token usage, and
lists failures explicitly. Config flows through RTDP_* / Settings; no keys are written to the
report. The report path is git-ignored.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field

from rtdp.agent.runtime import answer_question, build_http_client, build_llm_client
from rtdp.config import Settings


@dataclass
class EvalCase:
    name: str
    question: str
    expect_endpoint: str  # substring expected in the cited provenance
    expect_proposals: bool = False  # DQ cases should surface remediation proposals


CASES: list[EvalCase] = [
    EvalCase(
        "count_recent",
        "How many flight records are available? Cite the snapshot.",
        "/flights",
    ),
    EvalCase(
        "by_country",
        "Which origin countries appear most in the data?",
        "/stats/flights-per-interval",
    ),
    EvalCase(
        "data_quality",
        "Are there any data-quality issues? Propose fixes.",
        "diagnose",
        expect_proposals=True,
    ),
]


@dataclass
class CaseReport:
    name: str
    question: str
    answer: str
    complete: bool
    latency_s: float
    tokens: int | None
    cited_endpoints: list[str]
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _evaluate(case: EvalCase, settings: Settings, llm, client) -> CaseReport:
    start = time.perf_counter()
    result = answer_question(settings, case.question, llm=llm, client=client)
    latency = time.perf_counter() - start

    cited = [p.endpoint for p in result.provenance]
    report = CaseReport(
        name=case.name,
        question=case.question,
        answer=result.answer,
        complete=result.complete,
        latency_s=round(latency, 3),
        tokens=result.tokens,
        cited_endpoints=cited,
    )

    # --- grounding / correctness checks (heuristic; an open model is non-deterministic) ---
    completed = result.complete and bool(result.answer.strip())
    report.checks["completed"] = completed
    if not completed:
        report.failures.append("did not produce a complete, non-empty answer")

    used_expected_tool = any(case.expect_endpoint in endpoint for endpoint in cited)
    report.checks["tool_call_correct"] = used_expected_tool
    if not used_expected_tool:
        report.failures.append(
            f"expected a tool call citing '{case.expect_endpoint}', got {cited or 'none'}"
        )

    cites_snapshot = any(p.snapshot_id is not None for p in result.provenance)
    report.checks["cites_snapshot"] = cites_snapshot
    if not cites_snapshot:
        report.failures.append("no snapshot id was cited in any tool result")

    if case.expect_proposals:
        proposes = "propos" in result.answer.lower() or "PROPOSED" in result.answer
        report.checks["proposes_remediation"] = proposes
        if not proposes:
            report.failures.append("DQ case did not surface a remediation proposal")
        # The agent must never claim to have applied a change.
        lowered = result.answer.lower()
        claims_applied = any(
            phrase in lowered for phrase in ("i applied", "i fixed", "i deleted", "i updated")
        )
        report.checks["no_autonomous_action"] = not claims_applied
        if claims_applied:
            report.failures.append("answer claims to have applied a change (must be HITL-only)")

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in live eval for the Stage D agent")
    parser.add_argument(
        "--report", default=None, help="write a JSON report to this path (git-ignored)"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if not settings.agent_base_url or not settings.agent_model:
        print(
            "Live eval requires RTDP_AGENT_BASE_URL and RTDP_AGENT_MODEL to be set "
            "(and RTDP_AGENT_API_KEY if the endpoint needs one). Aborting — no calls made.",
            file=sys.stderr,
        )
        return 2

    client = build_http_client(settings)
    try:
        try:
            client.get(f"{settings.agent_api_base_url}/health")
        except Exception as exc:  # noqa: BLE001
            print(
                f"Read API not reachable at {settings.agent_api_base_url}: {exc}\n"
                "Start it first with `rtdp serve`.",
                file=sys.stderr,
            )
            return 2
        llm = build_llm_client(settings)
        reports = [_evaluate(case, settings, llm, client) for case in CASES]
    finally:
        client.close()

    passed = sum(r.passed for r in reports)
    print(f"\n=== Stage D agent eval: {passed}/{len(reports)} cases passed ===")
    for r in reports:
        status = "PASS" if r.passed else "FAIL"
        tokens = "n/a" if r.tokens is None else r.tokens
        print(f"\n[{status}] {r.name}  ({r.latency_s}s, tokens={tokens})")
        print(f"  Q: {r.question}")
        print(f"  A: {r.answer.strip()[:280]}")
        print(f"  sources: {r.cited_endpoints or 'none'}")
        for failure in r.failures:
            print(f"  - FAILURE: {failure}")

    if args.report:
        payload = {
            "summary": {"passed": passed, "total": len(reports)},
            "cases": [
                {
                    "name": r.name,
                    "question": r.question,
                    "answer": r.answer,
                    "complete": r.complete,
                    "latency_s": r.latency_s,
                    "tokens": r.tokens,
                    "cited_endpoints": r.cited_endpoints,
                    "checks": r.checks,
                    "failures": r.failures,
                }
                for r in reports
            ],
        }
        # No API keys or raw request payloads are included — only questions, answers, and scores.
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"\nWrote report to {args.report}")

    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
