"""Results routes — serve detection JSON, segmentation, and LOD tiles.

Job ids are UUIDs and tile ids match TILE_ID_PATTERN, so a path parameter is always a
single plain key segment (M56). In per-user mode every route first checks the job is
the caller's (T31): the tiles rebuild the whole input scan.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import Response

from pointcloud_detect.api.access import owned_job, require_job_access
from pointcloud_detect.api.deps import get_s3_client, get_settings
from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.models import TILE_ID_PATTERN
from pointcloud_detect.api.ownership import Principal

router = APIRouter()

TileId = Annotated[str, PathParam(pattern=TILE_ID_PATTERN)]


@router.get("/{job_id}/detections")
async def get_detections(
    job_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Serve detection results JSON for a completed job."""
    job = owned_job(str(job_id), principal)
    if job.status != "complete":
        raise HTTPException(400, f"Job not complete (status: {job.status})")

    settings = get_settings()
    s3 = get_s3_client()

    key = f"results/{job_id}/detections.json"
    try:
        obj = s3.get_object(Bucket=settings.s3_output_bucket, Key=key)
        data = obj["Body"].read()
        return Response(content=data, media_type="application/json")
    except s3.exceptions.NoSuchKey:
        raise HTTPException(404, "Detection results not found")


@router.get("/{job_id}/segmentation")
async def get_segmentation(
    job_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Serve segmentation results JSON for a completed job."""
    owned_job(str(job_id), principal)

    settings = get_settings()
    s3 = get_s3_client()

    key = f"results/{job_id}/segmentation.json"
    try:
        obj = s3.get_object(Bucket=settings.s3_output_bucket, Key=key)
        data = obj["Body"].read()
        return Response(content=data, media_type="application/json")
    except Exception:
        raise HTTPException(404, "Segmentation results not found")


@router.get("/{job_id}/tiles/index")
async def get_tile_index(
    job_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Serve the LOD tile index for the 3D viewer."""
    require_job_access(str(job_id), principal)
    settings = get_settings()
    s3 = get_s3_client()

    key = f"tiles/{job_id}/index.json"
    try:
        obj = s3.get_object(Bucket=settings.s3_tiles_bucket, Key=key)
        data = obj["Body"].read()
        return Response(content=data, media_type="application/json")
    except Exception:
        raise HTTPException(404, "Tile index not found")


@router.get("/{job_id}/tiles/{tile_id}")
async def get_tile(
    job_id: uuid.UUID,
    tile_id: TileId,
    principal: Principal = Depends(current_principal),
):
    """Serve a single LOD tile (binary point data)."""
    require_job_access(str(job_id), principal)
    settings = get_settings()
    s3 = get_s3_client()

    key = f"tiles/{job_id}/{tile_id}.bin"
    try:
        obj = s3.get_object(Bucket=settings.s3_tiles_bucket, Key=key)
        data = obj["Body"].read()
        return Response(content=data, media_type="application/octet-stream")
    except Exception:
        raise HTTPException(404, f"Tile not found: {tile_id}")


@router.get("/{job_id}/tiles/{tile_id}/segmentation")
async def get_tile_segmentation(
    job_id: uuid.UUID,
    tile_id: TileId,
    principal: Principal = Depends(current_principal),
):
    """Serve per-tile segmentation labels (Uint16 array, one label per point).

    Aligned index-for-index with the tile's point data. Returns 404 if
    segmentation was not run for this job.
    """
    require_job_access(str(job_id), principal)
    settings = get_settings()
    s3 = get_s3_client()

    key = f"tiles/{job_id}/{tile_id}_seg.bin"
    try:
        obj = s3.get_object(Bucket=settings.s3_tiles_bucket, Key=key)
        data = obj["Body"].read()
        return Response(content=data, media_type="application/octet-stream")
    except Exception:
        raise HTTPException(404, f"Segmentation not available for tile: {tile_id}")
