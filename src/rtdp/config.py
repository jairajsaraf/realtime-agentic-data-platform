"""Environment-driven configuration.

A single :class:`Settings` object resolves the object-storage backend, the S3 /
LocalStack endpoint, the SQLite catalog URI, the Iceberg warehouse location, and
the data source. Real AWS is a config-only swap: set ``RTDP_STORAGE_BACKEND=aws``
and provide real credentials/region — no code changes.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageBackend(StrEnum):
    LOCALSTACK = "localstack"
    FILE = "file"
    AWS = "aws"


class SourceKind(StrEnum):
    SYNTHETIC = "synthetic"
    OPENSKY_LIVE = "opensky-live"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTDP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage selection ---
    storage_backend: StorageBackend = StorageBackend.LOCALSTACK

    # --- S3 / object storage (localstack + aws backends) ---
    s3_endpoint_url: str = "http://localhost:4566"
    s3_bucket: str = "lakehouse"
    s3_warehouse_prefix: str = "warehouse"
    aws_region: str = "us-east-1"
    aws_access_key_id: str = "test"
    aws_secret_access_key: str = "test"

    # --- local file warehouse (file backend / no-Docker fallback) ---
    local_warehouse_dir: Path = Path("./_warehouse")

    # --- catalog (pyiceberg SqlCatalog on SQLite) ---
    catalog_name: str = "rtdp"
    catalog_db_path: Path = Path("./_warehouse/catalog.db")
    namespace: str = "bronze"
    table_name: str = "opensky_state_vectors"

    # --- data source ---
    source: SourceKind = SourceKind.SYNTHETIC
    opensky_client_id: str | None = None
    opensky_client_secret: str | None = None
    opensky_token_url: str = (
        "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    )
    opensky_states_url: str = "https://opensky-network.org/api/states/all"

    # --- read/serving API (Stage 2A) ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_default_limit: int = 100
    api_max_limit: int = 1000

    # --- incremental micro-batch ingestion (Stage 2B) ---
    stream_interval_seconds: int = 60  # seconds between micro-batch polls in `rtdp stream`
    stream_max_batches: int = 0  # 0 = run until interrupted
    expire_retain_last: int = 10  # default snapshots to keep for `rtdp maintain expire-snapshots`

    # --- agentic layer (Stage D) ---
    # The agent is an HTTP client of the Stage 2A read API; it never touches the catalog/query
    # layer directly. `agent_api_url` is the base URL it calls (defaults to the local serve URL).
    agent_api_url: str | None = None
    # OpenAI-compatible LLM endpoint. Provider-agnostic and swappable by config; when unset, the
    # live client is disabled (deterministic fake-LLM tests need none of these).
    agent_base_url: str | None = None  # e.g. an NVIDIA NIM / OpenAI-compatible base URL
    agent_api_key: str | None = None  # bearer token for the LLM endpoint — secret, never committed
    agent_model: str | None = None  # model name, e.g. "meta/llama-3.1-8b-instruct"
    agent_timeout_seconds: float = 60.0  # per LLM HTTP call
    agent_max_turns: int = 6  # model-turn budget per question (LLM round-trips)
    agent_max_tool_calls: int = 12  # total tool executions per question (caps multi-call fan-out)
    agent_temperature: float = 0.0  # near-deterministic generation
    agent_max_rows: int = 1000  # row cap the agent requests for DQ sampling (<= api_max_limit)

    # --- observability / telemetry (Stage E) ---
    # No-op by default: tracing export is OFF unless `otel_enabled` is true AND the optional
    # `[otel]` extra is installed (see rtdp.telemetry). Provider-agnostic — point the OTLP
    # endpoint at a collector / the Datadog Agent. No keys/secrets are read here.
    otel_enabled: bool = False
    otel_service_name: str = "rtdp"
    otel_exporter_otlp_endpoint: str | None = None  # e.g. http://localhost:4317; None = SDK default
    log_format: LogFormat = LogFormat.TEXT  # structured JSON logs in deploy via `json`
    log_level: str = "INFO"

    # --- derived values ---
    @property
    def warehouse_location(self) -> str:
        """Iceberg warehouse root: ``s3://...`` for S3 backends, ``file://...`` for local."""
        if self.storage_backend is StorageBackend.FILE:
            return self.local_warehouse_dir.resolve().as_uri()
        return f"s3://{self.s3_bucket}/{self.s3_warehouse_prefix}"

    @property
    def catalog_uri(self) -> str:
        """SQLAlchemy SQLite URI (absolute path) for the SqlCatalog metadata store."""
        return f"sqlite:///{self.catalog_db_path.resolve().as_posix()}"

    @property
    def table_identifier(self) -> str:
        return f"{self.namespace}.{self.table_name}"

    @property
    def agent_api_base_url(self) -> str:
        """Base URL of the Stage 2A read API the agent calls as its tools.

        Defaults to the local ``rtdp serve`` address when ``agent_api_url`` is unset, with no
        trailing slash so endpoint paths can be appended directly.
        """
        url = self.agent_api_url or f"http://{self.api_host}:{self.api_port}"
        return url.rstrip("/")

    def catalog_properties(self) -> dict[str, str]:
        """Properties passed to :class:`pyiceberg.catalog.sql.SqlCatalog`."""
        props: dict[str, str] = {
            "type": "sql",
            "uri": self.catalog_uri,
            "warehouse": self.warehouse_location,
            "init_catalog_tables": "true",
            # Cross-platform FileIO (fixes Windows file:// drive-letter paths; no-op
            # on POSIX and for S3). See rtdp._io.
            "py-io-impl": "rtdp._io.CrossPlatformPyArrowFileIO",
        }
        if self.storage_backend in (StorageBackend.LOCALSTACK, StorageBackend.AWS):
            props["s3.access-key-id"] = self.aws_access_key_id
            props["s3.secret-access-key"] = self.aws_secret_access_key
            props["s3.region"] = self.aws_region
            if self.storage_backend is StorageBackend.LOCALSTACK:
                # LocalStack custom endpoint. Leave path-style at pyarrow's default
                # (force-virtual-addressing=False) — LocalStack requires path-style.
                props["s3.endpoint"] = self.s3_endpoint_url
        return props
