"""Annotations routes — store and retrieve bounding box annotations per job."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pointcloud_detect.api.access import require_job_access
from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.models import ClassName
from pointcloud_detect.api.ownership import Principal
from pointcloud_detect.api.store import job_store

router = APIRouter()

# In-memory annotation store (job_id -> annotation set)
_annotations: dict[str, dict] = {}

# Boxes per saved set. Annotations are drawn by hand, so this is far above any real
# scene, and a full set still fits the request-body cap (api/deps.py), which
# tests/test_api_hardening.py checks.
MAX_ANNOTATIONS = 2000


class AnnotationBox(BaseModel):
    center_xyz: list[float] = Field(min_length=3, max_length=3)
    size_wlh: list[float] = Field(min_length=3, max_length=3)
    heading: float
    class_name: ClassName


class AnnotationSet(BaseModel):
    scene_id: str = Field(default="", max_length=128)
    point_cloud_path: str = Field(default="", max_length=1024)
    num_annotations: int = 0
    annotations: list[AnnotationBox] = Field(default=[], max_length=MAX_ANNOTATIONS)


def forget_annotations(job_id: str) -> None:
    """Drop a job's saved annotations, as part of deleting the job."""
    _annotations.pop(job_id, None)


@router.get("/{job_id}")
async def get_annotations(
    job_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Get saved annotations for a job; in per-user mode, the caller's job only."""
    jid = str(job_id)
    require_job_access(jid, principal)
    if jid not in _annotations:
        raise HTTPException(404, "No annotations for this job")
    return _annotations[jid]


@router.post("/{job_id}", status_code=201)
async def save_annotations(
    job_id: uuid.UUID,
    body: AnnotationSet,
    principal: Principal = Depends(current_principal),
):
    """Save annotations for a job (overwrites previous); per-user: the caller's job."""
    jid = str(job_id)
    require_job_access(jid, principal)
    stored = {
        "scene_id": body.scene_id or jid,
        "point_cloud_path": body.point_cloud_path,
        "num_annotations": len(body.annotations),
        "annotations": [a.model_dump() for a in body.annotations],
    }
    _annotations[jid] = stored

    # Update the job's detection_count so the training sessions table reflects it.
    try:
        job_store.update_status(jid, detection_count=len(body.annotations))
    except Exception:
        pass  # Job may not exist yet or store may reject; non-critical

    return {"status": "saved", "num_annotations": len(body.annotations)}
