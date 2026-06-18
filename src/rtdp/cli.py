"""Command-line entry point.

``rtdp info``    prints the resolved configuration.
``rtdp ingest``  runs one ingestion batch into the bronze Iceberg table.
``rtdp demo``    Iceberg capability demos (Phase 3).
``rtdp serve``   starts the read-only FastAPI serving layer (Stage 2A).
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

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
