from __future__ import annotations

import os

import pytest

from rtdp.config import Settings


@pytest.fixture(autouse=True)
def _clear_rtdp_env(monkeypatch):
    """Hermetic tests: ignore any RTDP_* env vars a developer may have set."""
    for key in list(os.environ):
        if key.startswith("RTDP_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def file_settings(tmp_path):
    """A file:// backed Settings on a temp dir — no Docker/LocalStack needed."""
    return Settings(
        _env_file=None,
        storage_backend="file",
        local_warehouse_dir=tmp_path / "warehouse",
        catalog_db_path=tmp_path / "warehouse" / "catalog.db",
        namespace="bronze",
        table_name="opensky_state_vectors",
    )
