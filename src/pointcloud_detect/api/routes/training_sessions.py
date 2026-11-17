"""Training session routes — group annotation jobs for fine-tuning."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pointcloud_detect.api.access import require_job_access
from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.models import MAX_NAME_LENGTH, ClassList
from pointcloud_detect.api.ownership import Principal
from pointcloud_detect.api.training_session_store import session_store

router = APIRouter()


class CreateSessionRequest(BaseModel):
    """Request to create a training session."""

    name: str = Field(max_length=MAX_NAME_LENGTH)
    base_model: str = Field(default="votenet_scannet", max_length=128)
    coord_frame: str = Field(default="z-up", max_length=32)
    use_color: bool = True
    use_intensity: bool = False
    classes: ClassList = []


class AddJobRequest(BaseModel):
    """Add an annotation job to a session."""

    job_id: uuid.UUID


@router.post("", status_code=201)
async def create_session(
    req: CreateSessionRequest, principal: Principal = Depends(current_principal)
):
    """Create a new training session, owned by the caller in per-user mode."""
    session = session_store.create(
        name=req.name,
        base_model=req.base_model,
        coord_frame=req.coord_frame,
        use_color=req.use_color,
        use_intensity=req.use_intensity,
        classes=req.classes,
        owner=principal.owner,
    )
    return session


@router.get("")
async def list_sessions(principal: Principal = Depends(current_principal)):
    """List the caller's training sessions (every session in shared mode)."""
    return {"sessions": session_store.list_all(owner=principal.owner_filter)}


@router.get("/{session_id}")
async def get_session(
    session_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Get a training session with its annotation jobs."""
    session = session_store.get(str(session_id), owner=principal.owner_filter)
    if session is None:
        raise HTTPException(404, f"Session not found: {session_id}")
    return session


@router.post("/{session_id}/jobs")
async def add_job_to_session(
    session_id: uuid.UUID,
    req: AddJobRequest,
    principal: Principal = Depends(current_principal),
):
    """Add an annotation job to a training session; per-user: the caller's own."""
    sid = str(session_id)
    session = session_store.get(sid, owner=principal.owner_filter)
    if session is None:
        raise HTTPException(404, f"Session not found: {sid}")
    # The canonical string: job_ids are compared with training jobs' job_id.
    job_id = str(req.job_id)
    require_job_access(job_id, principal)
    session_store.add_job(sid, job_id)
    return {"status": "added", "job_id": job_id}


# POST /{session_id}/train was removed. Fine-tuning is submitted from the
# annotation viewer via POST /api/training, which is the only path the UI takes.
#
# It was not merely unused. Its docstring promised to train on "all annotated data
# in this session" while passing only job_ids[0], it took `epochs` as an
# unvalidated query parameter, and it reached into session_store._lock and
# ._sessions directly. Its one remaining job — recording the training_job_id on
# the session — is handled by _update_linked_session() in routes/training.py,
# which matches a session by job-id membership.
