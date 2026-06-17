"""Build and bootstrap the pyiceberg SqlCatalog from :class:`Settings`.

The catalog is a real Iceberg catalog (atomic compare-and-swap on a metadata
pointer stored in SQLite); table data + metadata files land in the configured
warehouse (LocalStack/AWS S3 or a local ``file://`` directory).
"""

from __future__ import annotations

from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog

from .config import Settings, StorageBackend


def build_catalog(settings: Settings) -> Catalog:
    """Create the catalog, ensuring local state and (for LocalStack) the bucket exist."""
    _ensure_local_state(settings)
    if settings.storage_backend is StorageBackend.LOCALSTACK:
        ensure_bucket(settings)
    return SqlCatalog(settings.catalog_name, **settings.catalog_properties())


def ensure_namespace(catalog: Catalog, namespace: str) -> None:
    catalog.create_namespace_if_not_exists(namespace)


def _ensure_local_state(settings: Settings) -> None:
    settings.catalog_db_path.resolve().parent.mkdir(parents=True, exist_ok=True)
    if settings.storage_backend is StorageBackend.FILE:
        settings.local_warehouse_dir.resolve().mkdir(parents=True, exist_ok=True)


def ensure_bucket(settings: Settings) -> None:
    """Create the S3 bucket if it does not already exist (idempotent)."""
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=settings.s3_bucket)
