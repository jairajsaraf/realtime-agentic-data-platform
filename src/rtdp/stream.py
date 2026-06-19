"""Stage 2B scheduled micro-batch ingestion loop.

This is **near-real-time micro-batch ingestion, NOT true streaming**: a timer polls a source
and appends one micro-batch per interval, reusing the unchanged Stage 1 ``run_ingest`` writer.
Kafka/Flink-style streaming remains out of scope.

The loop is a plain, testable function (inject ``sleep`` to run it instantly in tests). Per tick
it fetches ONE batch, de-duplicates within the batch on ``(icao24, last_contact)``, skips empty
batches (so empty OpenSky polls never mint an empty snapshot), and otherwise appends via
``run_ingest`` using a :class:`PrefetchedSource` so the batch is not fetched twice (important for
live sources with rate limits). A batch that fails FAIL-level DQ is aborted by ``run_ingest``
(nothing is written); the loop routes it through the same per-batch error path so it is never
miscounted as a successful append. Transient per-batch errors are caught with exponential backoff
so the loop survives them; the loop stops cleanly on ``KeyboardInterrupt``.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .config import Settings
from .ingest import IngestResult, run_ingest
from .sources.base import PrefetchedSource, RawBatch, Source
from .transforms import dedupe_raw_records


class BatchDQAbort(Exception):
    """A micro-batch was rejected by FAIL-level data-quality checks.

    ``run_ingest`` does not raise on a FAIL-level DQ violation — it returns an
    :class:`~rtdp.ingest.IngestResult` with ``rows_written == 0`` and the table left
    untouched. The stream loop raises this so an aborted batch is routed through the normal
    per-batch error path (``on_error`` + backoff) rather than being counted as a successful
    append.
    """

    def __init__(self, result: IngestResult) -> None:
        self.result = result
        super().__init__(
            f"micro-batch aborted by FAIL-level data-quality checks "
            f"({result.dq.n_failures} violation(s), {result.rows_in} rows rejected)"
        )


def run_stream(
    settings: Settings,
    source: Source,
    *,
    interval_seconds: int,
    max_batches: int = 0,
    sleep: Callable[[float], None] = time.sleep,
    on_batch: Callable[[int, IngestResult], None] | None = None,
    on_skip: Callable[[int], None] | None = None,
    on_error: Callable[[int, Exception], None] | None = None,
    max_backoff_seconds: float = 300.0,
) -> list[IngestResult]:
    """Run the micro-batch loop and return the results of successful appends.

    Args:
        interval_seconds: seconds to wait between ticks (skipped after the final tick).
        max_batches: number of ticks to attempt; ``0`` runs until ``KeyboardInterrupt``.
        sleep: injected for tests (default :func:`time.sleep`).
        on_batch / on_skip / on_error: optional callbacks for a successful append, a skipped
            empty batch, and a caught per-batch error (including a FAIL-level DQ abort,
            reported as a :class:`BatchDQAbort`), respectively.
        max_backoff_seconds: cap for the exponential backoff applied after errors.
    """
    results: list[IngestResult] = []
    index = 0
    consecutive_failures = 0
    try:
        while max_batches == 0 or index < max_batches:
            try:
                batch = source.fetch()
                deduped = dedupe_raw_records(batch.records)
                if not deduped:
                    consecutive_failures = 0
                    if on_skip is not None:
                        on_skip(index)
                else:
                    prefetched = PrefetchedSource(
                        batch.source_name, RawBatch(deduped, batch.source_name)
                    )
                    result = run_ingest(settings, prefetched)
                    if not result.dq.ok:
                        # FAIL-level DQ aborts the write: run_ingest returns rows_written=0
                        # WITHOUT raising. Raise here so this tick goes through the per-batch
                        # error/backoff path below instead of being counted as an append.
                        raise BatchDQAbort(result)
                    results.append(result)
                    consecutive_failures = 0
                    if on_batch is not None:
                        on_batch(index, result)
            except Exception as exc:  # noqa: BLE001 — keep the loop alive across transient errors
                consecutive_failures += 1
                if on_error is not None:
                    on_error(index, exc)

            index += 1
            is_last = max_batches != 0 and index >= max_batches
            if not is_last:
                delay: float = interval_seconds
                if consecutive_failures:
                    delay = min(interval_seconds * 2**consecutive_failures, max_backoff_seconds)
                sleep(delay)
    except KeyboardInterrupt:
        pass  # graceful stop — return whatever was appended so far
    return results
