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


def test_stream_defaults_and_env_override(monkeypatch):
    s = _settings()
    assert s.stream_interval_seconds == 60
    assert s.stream_max_batches == 0
    assert s.expire_retain_last == 10

    monkeypatch.setenv("RTDP_STREAM_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("RTDP_EXPIRE_RETAIN_LAST", "3")
    s2 = _settings()
    assert s2.stream_interval_seconds == 5
    assert s2.expire_retain_last == 3


def test_agent_defaults_unset_so_fake_tests_need_no_config():
    s = _settings()
    # Live-model config is absent by default; deterministic fake-LLM tests require none of it.
    assert s.agent_base_url is None
    assert s.agent_model is None
    assert s.agent_api_key is None
    assert s.agent_max_turns == 6
    assert s.agent_max_tool_calls == 12
    assert s.agent_max_rows == 1000


def test_agent_settings_from_env(monkeypatch):
    monkeypatch.setenv("RTDP_AGENT_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("RTDP_AGENT_MODEL", "m1")
    monkeypatch.setenv("RTDP_AGENT_API_KEY", "secret")
    monkeypatch.setenv("RTDP_AGENT_MAX_TURNS", "9")
    monkeypatch.setenv("RTDP_AGENT_MAX_TOOL_CALLS", "4")
    s = _settings()
    assert s.agent_base_url == "http://llm.test/v1"
    assert s.agent_model == "m1"
    assert s.agent_api_key == "secret"
    assert s.agent_max_turns == 9
    assert s.agent_max_tool_calls == 4


def test_agent_api_base_url_default_and_override():
    # Defaults to the local serve URL...
    s = _settings(api_host="0.0.0.0", api_port=9000)
    assert s.agent_api_base_url == "http://0.0.0.0:9000"
    # ...and an explicit override wins, with any trailing slash stripped.
    s2 = _settings(agent_api_url="http://api.test:8080/")
    assert s2.agent_api_base_url == "http://api.test:8080"
