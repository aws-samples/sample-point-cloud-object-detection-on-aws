"""Local S3 and SQS for docker-compose.yml: moto, with the stack's buckets and queues.

State is in memory and is discarded when the container stops, as LocalStack's was.
"""

import json
import signal
import threading

import boto3

# moto is in this tool's own environment (pyproject.toml here), not the project's,
# which is the one pyright analyses.
from moto.server import ThreadedMotoServer  # pyright: ignore[reportMissingImports]

PORT = 4566
REGION = "us-east-1"
BUCKETS = [
    "pointcloud-detection-dev-input",
    "pointcloud-detection-dev-output",
    "pointcloud-detection-dev-tiles",
    "pointcloud-detection-dev-models",
]
QUEUE = "pointcloud-detection-dev-jobs"
DLQ = f"{QUEUE}-dlq"


def create_resources(endpoint: str) -> None:
    kwargs = {
        "endpoint_url": endpoint,
        "region_name": REGION,
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }
    s3 = boto3.client("s3", **kwargs)
    for bucket in BUCKETS:
        s3.create_bucket(Bucket=bucket)
    # The browser uploads to the input bucket with presigned URLs.
    s3.put_bucket_cors(
        Bucket=BUCKETS[0],
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["PUT", "POST", "GET"],
                    "AllowedOrigins": ["*"],
                    # As the deployed bucket does: multipart uploads read each part's
                    # ETag from a cross-origin response.
                    "ExposeHeaders": ["ETag"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )

    sqs = boto3.client("sqs", **kwargs)
    dlq_url = sqs.create_queue(QueueName=DLQ)["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    sqs.create_queue(
        QueueName=QUEUE,
        Attributes={
            "VisibilityTimeout": "900",
            "MessageRetentionPeriod": "604800",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}
            ),
        },
    )


def main() -> None:
    # All interfaces inside the container, so the other services can reach it;
    # docker-compose.yml publishes the port on the host's loopback only.
    server = ThreadedMotoServer(ip_address="0.0.0.0", port=PORT, verbose=False)  # nosec B104
    server.start()
    create_resources(f"http://127.0.0.1:{PORT}")
    print(
        f"ready on :{PORT}: buckets {', '.join(BUCKETS)}; queues {QUEUE}, {DLQ}",
        flush=True,
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
