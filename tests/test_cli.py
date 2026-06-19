"""CLI wiring tests for the Stage 2B commands (file:// backend, no Docker).

Exercises the ``rtdp stream`` and ``rtdp maintain expire-snapshots`` argument-parsing and
handler branches end-to-end against a temp file:// warehouse. Kept light: bounded batches,
synthetic source only, no live OpenSky, no LocalStack.
"""

from __future__ import annotations

import pytest

from rtdp.cli import main


@pytest.fixture
def cli_env(tmp_path, monkeypatch, _clear_rtdp_env):
    """Point the fresh ``Settings()`` built inside ``main`` at a temp file:// warehouse."""
    warehouse = tmp_path / "warehouse"
    monkeypatch.setenv("RTDP_STORAGE_BACKEND", "file")
    monkeypatch.setenv("RTDP_LOCAL_WAREHOUSE_DIR", str(warehouse))
    monkeypatch.setenv("RTDP_CATALOG_DB_PATH", str(warehouse / "catalog.db"))
    monkeypatch.setenv("RTDP_NAMESPACE", "bronze")
    monkeypatch.setenv("RTDP_TABLE_NAME", "opensky_state_vectors")
    return warehouse


def test_cli_stream_synthetic_bounded(cli_env, capsys):
    rc = main(
        [
            "stream",
            "--source",
            "synthetic",
            "--interval",
            "0",
            "--max-batches",
            "2",
            "--rows",
            "3",
            "--seed",
            "1",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stream stopped after 2 appended micro-batch(es)." in out


def test_cli_maintain_expire_snapshots(cli_env, capsys):
    # Create several snapshots via the stream command, then expire down to the newest 2.
    assert (
        main(["stream", "--interval", "0", "--max-batches", "5", "--rows", "2", "--seed", "1"])
        == 0
    )
    capsys.readouterr()  # discard stream output

    rc = main(["maintain", "expire-snapshots", "--retain", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Expired 3 snapshot(s); retained the newest 2." in out
