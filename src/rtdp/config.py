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
