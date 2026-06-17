from __future__ import annotations

import pytest

from rtdp.catalog import ensure_bucket
from rtdp.config import Settings


@pytest.mark.localstack
def test_localstack_bucket_roundtrip():
    """Validates docker-compose + S3 endpoint wiring. Run via the CI integration job."""
    boto3 = pytest.importorskip("boto3")
    s = Settings(_env_file=None)  # localstack defaults

    ensure_bucket(s)
    client = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        region_name=s.aws_region,
    )
    client.put_object(Bucket=s.s3_bucket, Key="smoke/hello.txt", Body=b"hello")
    body = client.get_object(Bucket=s.s3_bucket, Key="smoke/hello.txt")["Body"].read()
    assert body == b"hello"
