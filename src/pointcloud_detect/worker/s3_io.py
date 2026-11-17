"""S3 I/O helpers for the worker."""

import logging
from pathlib import Path

import orjson

logger = logging.getLogger(__name__)


def download_from_s3(s3_client, bucket: str, key: str, local_path: Path) -> None:
    """Download a file from S3 to local disk."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(bucket, key, str(local_path))
    logger.info(
        "Downloaded s3://%s/%s (%d bytes)", bucket, key, local_path.stat().st_size
    )


def upload_json_to_s3(s3_client, bucket: str, key: str, data: dict) -> None:
    """Upload a JSON object to S3."""
    body = orjson.dumps(data, option=orjson.OPT_INDENT_2)
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/json"
    )
    logger.info("Uploaded s3://%s/%s (%d bytes)", bucket, key, len(body))


def upload_binary_to_s3(s3_client, bucket: str, key: str, data: bytes) -> None:
    """Upload binary data to S3."""
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=data, ContentType="application/octet-stream"
    )
    logger.info("Uploaded s3://%s/%s (%d bytes)", bucket, key, len(data))
