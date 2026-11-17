"""Shared dependencies for API routes (S3, SQS clients, settings)."""

from functools import lru_cache

import boto3
from botocore.config import Config as BotoConfig
from pydantic import Field
from pydantic_settings import BaseSettings

# The bucket settings the AWS-mode API (api/app.py) cannot run without, by the
# environment variable that sets each one. There are no defaults: a default name is a
# guess at someone else's bucket, and it hides a missing setting until the first
# request fails, or reaches a bucket of that name in another account.
REQUIRED_BUCKET_SETTINGS = {
    "s3_input_bucket": "S3_INPUT_BUCKET",
    "s3_output_bucket": "S3_OUTPUT_BUCKET",
    "s3_tiles_bucket": "S3_TILES_BUCKET",
    "s3_models_bucket": "S3_MODELS_BUCKET",
}

# 1 MiB. The largest legitimate bodies are measured in tests/test_api_hardening.py:
# a complete-upload part list at S3's 10,000-part ceiling and an annotation set at
# its 2,000-box ceiling both stay well under it. File data never passes through the
# API; the browser sends it straight to S3.
DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None  # compose: http://aws-local:4566
    # Required in AWS mode, with no defaults: see REQUIRED_BUCKET_SETTINGS.
    s3_input_bucket: str = ""
    s3_output_bucket: str = ""
    s3_tiles_bucket: str = ""
    s3_models_bucket: str = ""
    sqs_queue_url: str = ""
    log_level: str = "INFO"
    log_format: str = "json"
    upload_part_size_bytes: int = 100 * 1024 * 1024  # 100MB
    # Largest file an upload may declare (routes/upload.py). The SPA stops at 500 MB;
    # this leaves room for scripted clients. Larger scenes are staged directly to the
    # input bucket (docs/EKS_DEPLOY_RUNBOOK.md).
    max_upload_bytes: int = Field(default=5 * 1024 * 1024 * 1024, gt=0)
    presigned_url_expiry: int = 3600  # 1 hour
    # Largest request body the API accepts, in bytes (api/middleware.py). Larger
    # bodies get 413 before they are parsed.
    max_request_body_bytes: int = Field(default=DEFAULT_MAX_REQUEST_BODY_BYTES, gt=0)
    # Serve /docs, /redoc and /openapi.json. Off by default: they describe every
    # route to anything that can reach the pod, and nothing in the product uses them.
    api_docs_enabled: bool = False
    # Comma-separated browser origins allowed to call the API cross-origin. Empty (the
    # default) allows none, which is correct for the shipped deployment: the frontend
    # and /api are served from one origin, so the browser never makes a CORS request.
    cors_allowed_origins: str = ""
    # Comma-separated Host names the API serves (api/hosts.py). The backend chart sets
    # them; empty, as in the local modes, turns the check off.
    allowed_hosts: str = ""
    # Who reaches what (api/identity.py, threat model T31): "per-user", where each
    # signed-in user reaches only what they created, or "shared", where every caller
    # reaches everything. Required, with no default (identity.require_access_settings).
    access_scope: str = ""
    # per-user only: the ALB that signs users in, and the Cognito user pool and app
    # client it signs them in with. deploy.sh sets all three.
    alb_arn: str = ""
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def allowed_host_names(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    def missing_bucket_settings(self) -> list[str]:
        """Environment variables of the required bucket settings that are unset."""
        return [
            env
            for field, env in REQUIRED_BUCKET_SETTINGS.items()
            if not getattr(self, field).strip()
        ]

    def require_buckets(self) -> None:
        """Raise, naming every missing variable, unless all buckets are set.

        Called when the AWS-mode API starts (api/app.py), so a deployment without them
        fails at startup with this message rather than on its first request. The
        backend chart and docker-compose set all four; the local filesystem backend
        (api/local_backend.py) uses no buckets and does not call this.
        """
        missing = self.missing_bucket_settings()
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} must be set. The API has no default bucket "
                "names; set each to the bucket Terraform created (terraform output "
                "s3_input_bucket, s3_output_bucket, s3_tiles_bucket, "
                "s3_models_bucket)."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_s3_client():
    settings = get_settings()
    kwargs: dict = {"region_name": settings.aws_region}
    s3_options: dict = {}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    else:
        # Presign against the Region's own host. botocore's default is the global
        # <bucket>.s3.amazonaws.com, which S3 answers for a new bucket outside
        # us-east-1 with a 307 for up to a day, and a browser cannot follow it with a
        # presigned URL: the host is signed. (Bucket names here carry no dots.)
        s3_options["addressing_style"] = "virtual"
    kwargs["config"] = BotoConfig(signature_version="s3v4", s3=s3_options or None)
    return boto3.client("s3", **kwargs)


@lru_cache
def get_sqs_client():
    settings = get_settings()
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return boto3.client("sqs", **kwargs)
