"""Table maintenance for the incremental (Stage 2B) micro-batch lakehouse.

Micro-batch ingestion appends one snapshot per interval, so snapshot metadata grows
without bound. ``expire_snapshots`` bounds that growth by dropping old snapshots from the
table metadata, keeping the newest ``retain_last``.

IMPORTANT — this is **metadata maintenance, not data-file compaction**. pyiceberg's
``expire_snapshots`` only removes snapshot entries from the table metadata; it does NOT
delete the underlying data/manifest files and does NOT rewrite or merge small files.
Compaction is a true-engine (e.g. Spark) concern and remains out of scope for this
pyiceberg-only build — we do not fake it. Expiring snapshots reduces metadata growth and
the available time-travel history, not on-disk storage.
"""

from __future__ import annotations

from pyiceberg.table import Table


def _snapshot_sort_key(snapshot) -> tuple[int, int]:
    """Newest-last ordering key: (timestamp_ms, sequence_number).

    ``sequence_number`` disambiguates snapshots committed in the same millisecond
    (matching the tie-break used in ``rtdp.query.resolve_snapshot_id``)."""
    seq = getattr(snapshot, "sequence_number", None)
    return (snapshot.timestamp_ms, seq if seq is not None else -1)


def expire_snapshots(table: Table, *, retain_last: int) -> list[int]:
    """Expire all but the newest ``retain_last`` snapshots (metadata-only).

    Keeps the ``retain_last`` most-recent snapshots and expires the rest, except snapshots
    that are protected — the current snapshot and any branch/tag head — which pyiceberg
    refuses to expire (``by_id`` raises on a protected id), so they are excluded up front.

    Returns the list of expired snapshot ids (``[]`` if nothing was expired, e.g. when the
    table has ``<= retain_last`` snapshots). Does NOT delete data files (see module docstring).
    """
    if retain_last < 1:
        raise ValueError("retain_last must be >= 1")

    snapshots = list(table.metadata.snapshots)
    if len(snapshots) <= retain_last:
        return []

    # Snapshots pyiceberg will not expire: the current snapshot + every ref (branch/tag) head.
    protected: set[int] = {ref.snapshot_id for ref in table.metadata.refs.values()}
    current = table.current_snapshot()
    if current is not None:
        protected.add(current.snapshot_id)

    newest_first = sorted(snapshots, key=_snapshot_sort_key, reverse=True)
    keep = {s.snapshot_id for s in newest_first[:retain_last]}
    to_expire = [
        s.snapshot_id
        for s in newest_first[retain_last:]
        if s.snapshot_id not in keep and s.snapshot_id not in protected
    ]
    if not to_expire:
        return []

    table.maintenance.expire_snapshots().by_ids(to_expire).commit()
    return to_expire
