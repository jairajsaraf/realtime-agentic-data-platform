# 1. pyiceberg, DuckDB, and a SQLite catalog, with no JVM

Status: Accepted

## Context

The platform has to run from a fresh clone on Windows and Linux, and its CI has to stay light. It
also has to be a real Apache Iceberg lakehouse, not a CSV-to-Parquet job: a catalog-backed table
with schema evolution, partition evolution, snapshot time travel, and ingestion-time data-quality
checks.

The main alternative was PySpark with Iceberg. It is more feature-complete, but it needs a JVM and
winutils on Windows and a heavy CI setup, which conflicts with fresh-clone reproducibility and light
CI. The one pyiceberg gap that matters here is data-file compaction, which was out of scope for the
first stage.

## Decision

- Write with pyiceberg and read with DuckDB. Both are Python packages, so no JVM is required.
- Use a pyiceberg `SqlCatalog` on SQLite. It is a real catalog (an atomic compare-and-swap on a
  metadata pointer) with no extra runtime, and it leaves a configuration path to Postgres or a
  REST catalog.
- Use LocalStack S3 as the primary storage backend. Real AWS S3 is a configuration-only swap. A
  `file://` backend exists as a dev/CI convenience only — not the target architecture.
- Validate with Pandera. A FAIL-level result aborts the write and a WARN-level result is reported.
- Push `icao24`, `callsign`, and time-window filters into the pyiceberg scan so partition pruning
  fires at the table-format level, and keep DuckDB for the SQL-shaped work (bounding boxes,
  `GROUP BY`, ordered paging).

## Consequences

- A fresh clone needs only Python 3.12 and uv. CI stays light.
- Snapshot maintenance is metadata-only — NOT compaction. pyiceberg can expire snapshots but cannot
  compact or rewrite data files in this build, so micro-batch appends accumulate small files.
- The SQLite catalog lives on local disk, so the topology is single-writer.
- The writer depends on pyiceberg's partition transforms working in a pure-Python install. An
  unlocked install that resolves pyiceberg 0.12 breaks the first partitioned write, because the
  `pyarrow` extra no longer pulls in `pyiceberg-core`. The lockfile pins a working version, so the
  supported install is `uv sync --frozen`. Fixing the declared version range is tracked separately.
