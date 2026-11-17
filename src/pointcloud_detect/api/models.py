"""Pydantic request/response models for the API."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

ALLOWED_EXTENSIONS = {".e57", ".las", ".laz", ".ply", ".bin"}

# Fixed by the S3 API, not a tunable of ours.
S3_MAX_MULTIPART_PARTS = 10_000

# Bounds on free text and lists (M56, M43). The SPA sets the same limits on its
# inputs (frontend/src/services/limits.ts); these are the ones that hold.
MAX_NAME_LENGTH = 128
MAX_FILENAME_LENGTH = 255
MAX_CLASS_NAME_LENGTH = 64
MAX_CLASSES = 100

ClassName = Annotated[str, StringConstraints(max_length=MAX_CLASS_NAME_LENGTH)]
ClassList = Annotated[list[ClassName], Field(max_length=MAX_CLASSES)]

# Tile ids are "overview" and "detail_NNNN" (tiles/generator.py). One path segment,
# never "." or "..", since the local backend joins it onto a directory.
TILE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

# Every input object the API creates or accepts lives under this prefix. The worker's
# IAM grant still covers the whole input bucket, so this is what stops a client
# naming some other object in it as a job's input.
INPUT_KEY_PREFIX = "uploads/"


def validate_input_key(key: str) -> str:
    """Reject input keys outside uploads/, with traversal segments, or of other types.

    S3 itself has no directories, so ``..`` is not a traversal there. It is on the
    worker, which used the key's last segment as a local filename, and a client
    choosing arbitrary keys could otherwise point a job at any object in the bucket.
    """
    segments = key.split("/")
    if (
        not key.startswith(INPUT_KEY_PREFIX)
        or "\\" in key
        or any(seg in ("", ".", "..") for seg in segments)
        or len(segments) < 3
    ):
        raise ValueError(
            f"s3_key must be of the form {INPUT_KEY_PREFIX}<id>/<filename> "
            "with no empty, '.' or '..' segments"
        )
    ext = "." + segments[-1].rsplit(".", 1)[-1].lower() if "." in segments[-1] else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"s3_key has unsupported format {ext!r}")
    return key


class JobStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class JobConfigRequest(BaseModel):
    """Detection job configuration from client."""

    model_mode: str = "indoor"
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    use_color: bool = True
    use_intensity: bool = False
    use_normals: bool = False
    coord_frame: str = "z-up"
    # Positive: the voxel grid divides by it. 10 m is far coarser than any scan needs.
    downsample_voxel_m: float = Field(default=0.02, gt=0.0, le=10.0)
    max_points: int | None = Field(default=None, ge=1, le=1_000_000_000)
    custom_weights_version: str | None = None
    detection_config: str | None = Field(
        default=None,
        description=(
            "Detection model architecture/config name (e.g. 'fcaf3d_scannet', "
            "'votenet_scannet'). Selects which pretrained model to run. When unset, "
            "the pipeline default for the chosen model_mode is used."
        ),
    )
    raw_mode: bool = False
    device_preference: str = Field(
        default="auto",
        description="Inference device: 'auto' (use GPU if available), 'cpu', or 'gpu'",
    )
    job_type: str = Field(
        default="detection",
        description=(
            "Job type: 'detection' (run inference) or "
            "'annotation' (view only, no detection)"
        ),
    )


class UploadInitiateRequest(BaseModel):
    """Request to start a multipart upload."""

    filename: str = Field(max_length=MAX_FILENAME_LENGTH)
    file_size_bytes: int = Field(gt=0)
    content_type: str = Field(default="application/octet-stream", max_length=255)

    @field_validator("filename")
    @classmethod
    def _plain_filename(cls, v: str) -> str:
        # Becomes the last segment of the object key, so it must be one segment.
        if not v or "/" in v or "\\" in v or v in (".", ".."):
            raise ValueError("filename must be a bare file name with no path")
        return v


class UploadInitiateResponse(BaseModel):
    """Response with upload session details."""

    upload_id: str
    s3_key: str
    part_size_bytes: int
    total_parts: int
    part_urls: list[dict]  # [{part_number, presigned_url}]


class UploadCompleteRequest(BaseModel):
    """Request to finalize a multipart upload."""

    upload_id: str = Field(max_length=1024)
    s3_key: str
    # [{part_number, etag}]; S3 accepts no more parts than this.
    parts: list[dict] = Field(max_length=S3_MAX_MULTIPART_PARTS)

    @field_validator("s3_key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        return validate_input_key(v)


class UploadCompleteResponse(BaseModel):
    """Response after upload completion."""

    s3_key: str
    filename: str


class JobCreateRequest(BaseModel):
    """Request to create a detection job."""

    s3_key: str
    filename: str = Field(max_length=MAX_FILENAME_LENGTH)
    file_size_bytes: int = 0
    config: JobConfigRequest = Field(default_factory=JobConfigRequest)

    @field_validator("s3_key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        return validate_input_key(v)


class JobResponse(BaseModel):
    """Job details response."""

    job_id: str
    filename: str
    s3_input_key: str
    config: JobConfigRequest
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    detection_count: int | None = None
    tile_count: int | None = None
    processing_time_s: float | None = None
    error_message: str | None = None


class JobListResponse(BaseModel):
    """Paginated job list."""

    jobs: list[JobResponse]
    total: int
