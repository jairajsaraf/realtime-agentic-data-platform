"""Command-line entry point.

``rtdp info``     prints the resolved configuration.
``rtdp ingest``   runs one ingestion batch into the bronze Iceberg table.
``rtdp demo``     Iceberg capability demos (Phase 3).
``rtdp serve``    starts the read-only FastAPI serving layer (Stage 2A).
``rtdp stream``   scheduled micro-batch ingestion (Stage 2B; near-real-time, not true streaming).
``rtdp maintain`` table maintenance, e.g. snapshot expiration (Stage 2B; metadata-only).
``rtdp agent``    natural-language agent over the read API (Stage D; read-only, HITL).
"""

from __future__ import annotations

import argparse
import sys

from .config import Settings


def _info(settings: Settings) -> int:
    print("Resolved RTDP configuration")
    print(f"  storage_backend    : {settings.storage_backend.value}")
    print(f"  warehouse_location : {settings.warehouse_location}")
    print(f"  catalog_name       : {settings.catalog_name}")
    print(f"  catalog_uri        : {settings.catalog_uri}")
    print(f"  table_identifier   : {settings.table_identifier}")
    print(f"  source             : {settings.source.value}")
    if settings.storage_backend.value != "file":
        endpoint = (
            settings.s3_endpoint_url
            if settings.storage_backend.value == "localstack"
            else "(real AWS)"
        )
        print(f"  s3_endpoint        : {endpoint}")
        print(f"  s3_bucket          : {settings.s3_bucket}")
        print(f"  aws_region         : {settings.aws_region}")
    return 0


def _ingest(settings: Settings, args: argparse.Namespace) -> int:
    from .dq import format_report
    from .ingest import run_ingest
    from .sources.synthetic import SyntheticSource

    source_kind = args.source or settings.source.value
    if source_kind == "opensky-live":
        from .sources.opensky import OpenSkyLiveSource

        source = OpenSkyLiveSource(settings)
    else:
        source = SyntheticSource(
            n_rows=args.rows, seed=args.seed, inject_failures=args.inject_failures
        )

    result = run_ingest(settings, source)
    print(format_report(result.dq))
    print()
    if not result.dq.ok:
        print(
            f"INGEST ABORTED: {result.dq.n_failures} FAIL-level violation(s); "
            f"nothing written to {result.table_identifier}.",
            file=sys.stderr,
        )
        return 1
    print(f"Wrote {result.rows_written} rows to {result.table_identifier}")
    print(f"  snapshot_id    : {result.snapshot_id}")
    print(f"  snapshot_count : {result.snapshot_count}")
    return 0


def _demo(settings: Settings, args: argparse.Namespace) -> int:
    from . import demos

    runners = {
        "catalog": (demos.demo_catalog, demos.format_catalog),
        "schema-evolution": (demos.demo_schema_evolution, demos.format_schema_evolution),
        "partition-evolution": (demos.demo_partition_evolution, demos.format_partition_evolution),
        "time-travel": (demos.demo_time_travel, demos.format_time_travel),
    }
    if args.name == "reset":
        print(f"Demo table reset (purged): {demos.reset_demo(settings)}")
        return 0
    if args.name == "all":
        for run, fmt in runners.values():
            print(fmt(run(settings)))
            print()
        return 0
    run, fmt = runners[args.name]
    print(fmt(run(settings)))
    return 0


def _serve(settings: Settings) -> int:
    import uvicorn

    from .api import create_app

    app = create_app(settings)
    print(
        f"Serving rtdp read-only API on http://{settings.api_host}:{settings.api_port}  "
        f"(OpenAPI docs at /docs). Read path only — no ingestion."
    )
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
    return 0


def _stream(settings: Settings, args: argparse.Namespace) -> int:
    from .sources.synthetic import ContinuousSyntheticSource
    from .stream import run_stream

    source_kind = args.source or settings.source.value
    if source_kind == "opensky-live":
        from .sources.opensky import OpenSkyLiveSource

        source = OpenSkyLiveSource(settings)
    else:
        source = ContinuousSyntheticSource(fleet_size=args.rows, seed=args.seed)

    interval = args.interval if args.interval is not None else settings.stream_interval_seconds
    max_batches = (
        args.max_batches if args.max_batches is not None else settings.stream_max_batches
    )

    print(
        f"Streaming micro-batches from {source.name} every {interval}s "
        f"(max_batches={max_batches or 'unbounded'}). Near-real-time micro-batch ingestion — "
        f"NOT true streaming. Ctrl+C to stop."
    )

    def _on_batch(i: int, result) -> None:
        dq = "PASS" if result.dq.ok else "FAIL"
        print(
            f"  batch {i}: wrote {result.rows_written} rows -> snapshot {result.snapshot_id} "
            f"(snapshot_count={result.snapshot_count}); DQ {dq}"
        )

    def _on_skip(i: int) -> None:
        print(f"  batch {i}: empty batch — skipped (no snapshot)")

    def _on_error(i: int, exc: Exception) -> None:
        print(f"  batch {i}: error: {exc} — backing off, will retry", file=sys.stderr)

    results = run_stream(
        settings,
        source,
        interval_seconds=interval,
        max_batches=max_batches,
        on_batch=_on_batch,
        on_skip=_on_skip,
        on_error=_on_error,
    )
    print(f"Stream stopped after {len(results)} appended micro-batch(es).")
    return 0


def _maintain(settings: Settings, args: argparse.Namespace) -> int:
    from . import query
    from .maintenance import expire_snapshots

    if args.action == "expire-snapshots":
        retain = args.retain if args.retain is not None else settings.expire_retain_last
        table = query.load_bronze_table(settings)
        expired = expire_snapshots(table, retain_last=retain)
        print(
            f"Expired {len(expired)} snapshot(s); retained the newest {retain}. "
            f"Metadata-only maintenance — data files are NOT deleted (not compaction)."
        )
        return 0

    print(f"Unknown maintenance action: {args.action}", file=sys.stderr)
    return 1


def _agent(settings: Settings, args: argparse.Namespace) -> int:
    from .agent.runtime import answer_question, build_http_client, build_llm_client

    overrides: dict = {}
    if args.api_url is not None:
        overrides["agent_api_url"] = args.api_url
    if args.max_turns is not None:
        overrides["agent_max_turns"] = args.max_turns
    if overrides:
        settings = settings.model_copy(update=overrides)

    base_url = settings.agent_api_base_url
    client = build_http_client(settings)
    try:
        try:
            client.get(f"{base_url}/health")
        except Exception as exc:  # noqa: BLE001 — any transport failure means the API is down
            print(
                f"Read API not reachable at {base_url}: {exc}\n"
                "Start it first with `rtdp serve`.",
                file=sys.stderr,
            )
            return 2
        try:
            llm = build_llm_client(settings)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        def _ask(question: str) -> None:
            result = answer_question(settings, question, llm=llm, client=client)
            if args.json:
                import json

                print(
                    json.dumps(
                        {
                            "answer": result.answer,
                            "provenance": [vars(p) for p in result.provenance],
                            "complete": result.complete,
                            "tokens": result.tokens,
                        },
                        default=str,
                    )
                )
            else:
                print(result.answer)
                print(result.citation_line())

        if args.interactive or not args.question:
            print("rtdp agent — interactive mode. Ask a question, or type 'quit' to exit.")
            while True:
                try:
                    question = input("agent> ").strip()
                except EOFError:
                    break
                if question.lower() in ("quit", "exit"):
                    break
                if question:
                    _ask(question)
        else:
            _ask(args.question)
        return 0
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rtdp", description="Stage 1 Iceberg lakehouse CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Print the resolved configuration")

    ingest = sub.add_parser("ingest", help="Ingest a batch into the bronze table")
    ingest.add_argument("--source", choices=["synthetic", "opensky-live"], default=None)
    ingest.add_argument("--rows", type=int, default=50, help="synthetic rows (default 50)")
    ingest.add_argument("--seed", type=int, default=42, help="synthetic seed (default 42)")
    ingest.add_argument(
        "--inject-failures",
        action="store_true",
        help="add FAIL-triggering rows (synthetic) to demo DQ abort",
    )

    demo = sub.add_parser("demo", help="Run an Iceberg capability demo (uses the demo.* table)")
    demo.add_argument(
        "name",
        choices=[
            "catalog",
            "schema-evolution",
            "partition-evolution",
            "time-travel",
            "all",
            "reset",
        ],
    )

    sub.add_parser("serve", help="Start the read-only FastAPI serving layer (Stage 2A)")

    stream = sub.add_parser(
        "stream",
        help="Scheduled micro-batch ingestion (Stage 2B; near-real-time, not true streaming)",
    )
    stream.add_argument("--source", choices=["synthetic", "opensky-live"], default=None)
    stream.add_argument(
        "--interval", type=int, default=None, help="seconds between polls (default from settings)"
    )
    stream.add_argument(
        "--max-batches", type=int, default=None, help="number of batches; 0 = until interrupted"
    )
    stream.add_argument(
        "--rows", type=int, default=8, help="synthetic fleet size per batch (default 8)"
    )
    stream.add_argument("--seed", type=int, default=42, help="synthetic seed (default 42)")

    maintain = sub.add_parser(
        "maintain", help="Table maintenance (Stage 2B; snapshot expiration is metadata-only)"
    )
    maintain.add_argument("action", choices=["expire-snapshots"])
    maintain.add_argument(
        "--retain", type=int, default=None, help="snapshots to keep (default from settings)"
    )

    agent = sub.add_parser(
        "agent",
        help="Natural-language agent over the read API (Stage D; read-only, HITL)",
    )
    agent.add_argument(
        "question", nargs="?", default=None, help="question to ask (omit for interactive mode)"
    )
    agent.add_argument("--interactive", "-i", action="store_true", help="interactive REPL mode")
    agent.add_argument(
        "--api-url", default=None, help="read API base URL (default: settings / local serve)"
    )
    agent.add_argument(
        "--max-turns", type=int, default=None, help="tool-call budget per question"
    )
    agent.add_argument(
        "--json", action="store_true", help="emit JSON (answer + provenance) instead of text"
    )

    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "info":
        return _info(settings)
    if args.command == "ingest":
        return _ingest(settings, args)
    if args.command == "demo":
        return _demo(settings, args)
    if args.command == "serve":
        return _serve(settings)
    if args.command == "stream":
        return _stream(settings, args)
    if args.command == "maintain":
        return _maintain(settings, args)
    if args.command == "agent":
        return _agent(settings, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
