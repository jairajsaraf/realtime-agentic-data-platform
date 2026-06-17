from __future__ import annotations

from rtdp import demos


def test_demo_catalog(file_settings):
    r = demos.demo_catalog(file_settings)
    assert demos.DEMO_NAMESPACE in r.namespaces
    assert demos.DEMO_IDENTIFIER in r.demo_tables
    assert r.identifier == demos.DEMO_IDENTIFIER
    assert "icao24" in r.schema_columns
    assert r.current_snapshot_id is not None
    assert r.snapshot_count >= 1


def test_demo_schema_evolution(file_settings):
    r = demos.demo_schema_evolution(file_settings)
    assert demos.EVOLVED_COLUMN not in r.before_columns
    assert demos.EVOLVED_COLUMN in r.after_columns
    assert len(r.after_columns) == len(r.before_columns) + 1
    assert r.s1_row_count == 20
    # Old snapshot has no populated new-column values; current has exactly batch2 rows.
    assert r.s1_value_count == 0
    assert r.current_value_count == r.n_batch2


def test_demo_partition_evolution(file_settings):
    r = demos.demo_partition_evolution(file_settings)
    # Spec evolved 0 -> 1, both specs retained.
    assert r.spec_ids == [0, 1]
    assert "day" in r.before_spec
    assert "hour" in r.after_spec
    # No rewrite: files identical immediately after the spec update, and the
    # pre-existing files keep spec_id 0 through the final state.
    assert all(spec == 0 for _, spec in r.files_before)
    assert r.files_after_evolution == r.files_before
    assert r.pre_files_unchanged is True
    # New appended files use the new spec id.
    assert r.new_file_spec_ids == [1]
    assert r.total_rows == r.rows_a + r.rows_b


def test_demo_time_travel(file_settings):
    r = demos.demo_time_travel(file_settings)
    # Primary proof: explicit snapshot-id reads.
    assert r.rows_at_s1 == r.n1
    assert r.rows_current == r.n_total
    assert r.s1_snapshot_id != r.s2_snapshot_id
    # Secondary: timestamp lookup resolves to a real snapshot (not asserted exact).
    assert r.as_of_snapshot_id is not None


def test_reset_only_touches_demo_table(file_settings):
    # reset must target the dedicated demo table, never bronze.
    assert demos.reset_demo(file_settings) == demos.DEMO_IDENTIFIER
    assert demos.DEMO_IDENTIFIER.startswith("demo.")
    assert demos.DEMO_IDENTIFIER != file_settings.table_identifier
