from __future__ import annotations

from rtdp.config import Settings, SourceKind, StorageBackend


def _settings(**kw):
    # _env_file=None so a developer's local .env never leaks into tests.
    return Settings(_env_file=None, **kw)


def test_defaults_localstack():
    s = _settings()
    assert s.storage_backend is StorageBackend.LOCALSTACK
    assert s.warehouse_location == "s3://lakehouse/warehouse"
    assert s.catalog_uri.startswith("sqlite:///")
    assert s.table_identifier == "bronze.opensky_state_vectors"

    props = s.catalog_properties()
    assert props["type"] == "sql"
    assert props["warehouse"] == "s3://lakehouse/warehouse"
    assert props["s3.endpoint"] == "http://localhost:4566"
    assert props["s3.access-key-id"] == "test"


def test_file_backend(tmp_path):
    s = _settings(storage_backend="file", local_warehouse_dir=tmp_path / "wh")
    assert s.warehouse_location.startswith("file://")
    props = s.catalog_properties()
    assert "s3.endpoint" not in props
    assert "s3.access-key-id" not in props


def test_aws_backend_has_no_endpoint():
    s = _settings(storage_backend="aws", s3_bucket="prod-lake")
    assert s.warehouse_location == "s3://prod-lake/warehouse"
    props = s.catalog_properties()
    assert "s3.endpoint" not in props
    assert props["s3.access-key-id"] == "test"


def test_env_override(monkeypatch):
    monkeypatch.setenv("RTDP_STORAGE_BACKEND", "aws")
    monkeypatch.setenv("RTDP_S3_BUCKET", "envbucket")
    s = _settings()
    assert s.storage_backend is StorageBackend.AWS
    assert s.s3_bucket == "envbucket"
    assert s.warehouse_location == "s3://envbucket/warehouse"


def test_source_enum():
    s = _settings(source="opensky-live")
    assert s.source is SourceKind.OPENSKY_LIVE
