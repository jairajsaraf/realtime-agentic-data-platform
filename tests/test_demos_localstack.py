from __future__ import annotations

import pytest

from rtdp import demos
from rtdp.config import Settings


@pytest.mark.localstack
def test_demo_partition_evolution_on_s3():
    """Run a capability demo end-to-end on the primary LocalStack S3 backend."""
    settings = Settings(_env_file=None)  # localstack defaults
    assert settings.storage_backend.value == "localstack"

    r = demos.demo_partition_evolution(settings)
    assert r.spec_ids == [0, 1]
    assert r.files_after_evolution == r.files_before
    assert r.pre_files_unchanged is True
    assert r.new_file_spec_ids == [1]
    assert r.total_rows == r.rows_a + r.rows_b
