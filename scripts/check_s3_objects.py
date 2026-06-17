"""Diagnostic: list the warehouse objects in S3 (LocalStack or real AWS).

Usage:
    uv run python scripts/check_s3_objects.py

Proves that Iceberg metadata + data files physically exist in object storage.
Honors the same RTDP_* configuration as the CLI. Read-only.
"""

from __future__ import annotations

import boto3

from rtdp.config import Settings, StorageBackend


def main() -> None:
    s = Settings()
    if s.storage_backend not in (StorageBackend.LOCALSTACK, StorageBackend.AWS):
        raise SystemExit(f"S3 backend required; current backend is {s.storage_backend.value}")

    client = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url if s.storage_backend is StorageBackend.LOCALSTACK else None,
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        region_name=s.aws_region,
    )

    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s.s3_bucket):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))

    meta = [k for k in keys if k.endswith(".metadata.json")]
    manifests = [k for k in keys if k.endswith(".avro")]
    data = [k for k in keys if k.endswith(".parquet")]

    print(f"bucket           : {s.s3_bucket}")
    print(f"endpoint         : {s.s3_endpoint_url}")
    print(f"total objects    : {len(keys)}")
    print(f"metadata.json    : {len(meta)}")
    print(f"manifest .avro   : {len(manifests)}")
    print(f"parquet data     : {len(data)}")
    print("sample keys:")
    for key in sorted(keys)[:8]:
        print(f"  {key}")


if __name__ == "__main__":
    main()
