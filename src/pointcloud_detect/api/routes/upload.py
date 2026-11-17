"""Upload routes — multipart upload via S3 pre-signed URLs."""

import math
import uuid

from fastapi import APIRouter, Depends, HTTPException

from pointcloud_detect.api.access import require_own_upload
from pointcloud_detect.api.deps import get_s3_client, get_settings
from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.models import (
    ALLOWED_EXTENSIONS,
    S3_MAX_MULTIPART_PARTS,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitiateRequest,
    UploadInitiateResponse,
    validate_input_key,
)
from pointcloud_detect.api.ownership import Principal, upload_key_prefix

router = APIRouter()


def plan_part_count(filename: str, file_size_bytes: int, part_size: int) -> int:
    """Return the number of parts for a file, rejecting plans S3 cannot accept.

    S3 caps a multipart upload at 10,000 parts. At the default 100 MB part size that
    ceiling is only reached around 1 TB, but upload_part_size_bytes is configurable:
    lower it and a large file silently plans an upload S3 will refuse. Without this
    check the refusal arrives from S3 only partway through, after the client has
    already transferred hundreds of parts.

    Split out from the handler so the boundary can be tested without an S3 client and
    without generating ten thousand pre-signed URLs to prove an off-by-one.
    """
    total_parts = math.ceil(file_size_bytes / part_size)
    if total_parts > S3_MAX_MULTIPART_PARTS:
        raise HTTPException(
            400,
            f"{filename} needs {total_parts} parts at the configured part size of "
            f"{part_size} bytes, which exceeds S3's limit of {S3_MAX_MULTIPART_PARTS}. "
            f"Raise upload_part_size_bytes to at least "
            f"{math.ceil(file_size_bytes / S3_MAX_MULTIPART_PARTS)} bytes, or stage "
            f"this file directly to the bucket instead of uploading it.",
        )
    return total_parts


@router.post("/initiate", response_model=UploadInitiateResponse)
async def initiate_upload(
    request: UploadInitiateRequest, principal: Principal = Depends(current_principal)
):
    """Start a multipart upload, returning pre-signed URLs for each part.

    In per-user mode the key is under the caller's own ``uploads/<user id>/`` (T31),
    which is what the job, complete and abort routes check.
    """
    # Validate extension
    ext = (
        "." + request.filename.rsplit(".", 1)[-1].lower()
        if "." in request.filename
        else ""
    )
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400, f"Unsupported format: {ext}. Allowed: {ALLOWED_EXTENSIONS}"
        )

    settings = get_settings()
    part_size = settings.upload_part_size_bytes

    # A quota, not S3's limit (T43): without it one upload could plan about 1 TB.
    if request.file_size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            413,
            f"{request.filename} is {request.file_size_bytes} bytes; uploads through "
            f"the API are limited to {settings.max_upload_bytes} bytes "
            "(MAX_UPLOAD_BYTES). Stage larger files directly to the input bucket.",
        )

    # Validated before create_multipart_upload, deliberately. Rejecting afterwards
    # would leave an initiated upload in the bucket that no client will ever complete
    # or abort, accruing storage for its parts until a lifecycle rule swept it.
    total_parts = plan_part_count(request.filename, request.file_size_bytes, part_size)

    s3 = get_s3_client()

    # Generate unique S3 key
    upload_dir = str(uuid.uuid4())
    s3_key = f"{upload_key_prefix(principal.owner)}{upload_dir}/{request.filename}"

    # Initiate multipart upload
    response = s3.create_multipart_upload(
        Bucket=settings.s3_input_bucket,
        Key=s3_key,
        ContentType=request.content_type,
    )
    upload_id = response["UploadId"]

    # Generate pre-signed URLs for each part
    part_urls = []
    for part_num in range(1, total_parts + 1):
        url = s3.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings.s3_input_bucket,
                "Key": s3_key,
                "UploadId": upload_id,
                "PartNumber": part_num,
            },
            ExpiresIn=settings.presigned_url_expiry,
        )
        part_urls.append({"part_number": part_num, "presigned_url": url})

    return UploadInitiateResponse(
        upload_id=upload_id,
        s3_key=s3_key,
        part_size_bytes=part_size,
        total_parts=total_parts,
        part_urls=part_urls,
    )


@router.post("/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    request: UploadCompleteRequest, principal: Principal = Depends(current_principal)
):
    """Finalize a multipart upload after all parts are uploaded."""
    require_own_upload(request.s3_key, principal)
    settings = get_settings()
    s3 = get_s3_client()

    parts = [
        {"PartNumber": p["part_number"], "ETag": p["etag"]}
        for p in sorted(request.parts, key=lambda x: x["part_number"])
    ]

    s3.complete_multipart_upload(
        Bucket=settings.s3_input_bucket,
        Key=request.s3_key,
        UploadId=request.upload_id,
        MultipartUpload={"Parts": parts},
    )

    filename = request.s3_key.split("/")[-1]
    return UploadCompleteResponse(s3_key=request.s3_key, filename=filename)


@router.delete("/{upload_id}")
async def abort_upload(
    upload_id: str, s3_key: str, principal: Principal = Depends(current_principal)
):
    """Abort an incomplete multipart upload."""
    try:
        validate_input_key(s3_key)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    require_own_upload(s3_key, principal)
    settings = get_settings()
    s3 = get_s3_client()

    s3.abort_multipart_upload(
        Bucket=settings.s3_input_bucket,
        Key=s3_key,
        UploadId=upload_id,
    )
    return {"status": "aborted"}
