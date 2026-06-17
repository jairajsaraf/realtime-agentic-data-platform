"""Diagnostic: print bronze table state (snapshots, partition spec, sample rows).

Usage:
    uv run python scripts/inspect_table.py

Honors the same RTDP_* configuration as the CLI (backend, warehouse, catalog).
Read-only — useful for eyeballing ingested data and snapshot metadata.
"""

from __future__ import annotations

import duckdb

from rtdp.catalog import build_catalog
from rtdp.config import Settings


def main() -> None:
    settings = Settings()
    catalog = build_catalog(settings)
    table = catalog.load_table(settings.table_identifier)

    print(f"table_identifier : {settings.table_identifier}")
    print(f"warehouse        : {settings.warehouse_location}")
    print(f"snapshot_count   : {len(table.metadata.snapshots)}")
    for snap in table.metadata.snapshots:
        print(f"  - id={snap.snapshot_id} {snap.summary}")
    print(f"current_snapshot : {table.current_snapshot().snapshot_id}")
    print(f"partition_spec   : {table.spec()}")

    arr = table.scan().to_arrow()
    print(f"total_rows       : {arr.num_rows}")

    cols = [
        "icao24",
        "callsign",
        "event_time",
        "latitude",
        "longitude",
        "velocity",
        "position_source",
        "source_name",
    ]
    print("\nsample rows:")
    print(arr.select(cols).slice(0, 5).to_pandas().to_string(index=False))

    con = duckdb.connect()
    con.register("sv", arr)
    print("\nby source (DuckDB):")
    query = (
        "select source_name, count(*) as n, min(event_time) as min_event, "
        "max(event_time) as max_event from sv group by 1"
    )
    print(con.execute(query).fetchdf().to_string(index=False))


if __name__ == "__main__":
    main()
