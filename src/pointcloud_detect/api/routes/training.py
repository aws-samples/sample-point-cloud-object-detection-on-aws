"""Training routes — stub fine-tuning job management.

Accepts training submissions, simulates progress (queued → processing → complete)
via a background thread with configurable delay. Enough to prove out the full
annotation → train → model artifact flow without GPU nodes.
"""

import threading
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pointcloud_detect.api.access import require_job_access
from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.model_registry import (
    ModelRegistrationError,
    checkpoint_uri,
    model_registry,
)
from pointcloud_detect.api.models import ClassList
from pointcloud_detect.api.ownership import OwnerFilter, Principal, owner_matches
from pointcloud_detect.api.training_session_store import session_store

router = APIRouter()

# In-memory training job store, and each job's owner in per-user mode (T31), kept
# apart so that it is never returned to a client. Both are guarded by _lock.
_training_jobs: dict[str, dict] = {}
_training_owners: dict[str, str | None] = {}
_lock = threading.Lock()


def _owned(training_job_id: str, owner: OwnerFilter) -> dict | None:
    """The training job, if ``owner`` may reach it. Call with _lock held."""
    job = _training_jobs.get(training_job_id)
    if job is None or not owner_matches(_training_owners.get(training_job_id), owner):
        return None
    return job


# Simulated training duration in seconds (short for demo)
STUB_TRAINING_SECONDS = 15


class TrainingRequest(BaseModel):
    """Request to submit a fine-tuning job."""

    # A UUID, as every job id is. It was any string, stored and returned as the
    # source job, and the training page navigated to it: a crafted value moved a
    # second user's tab to another origin (T56).
    job_id: uuid.UUID
    base_model: str = Field(default="votenet_scannet", max_length=128)
    epochs: int = Field(default=30, ge=1, le=1000)
    custom_classes: ClassList = []


@router.post("", status_code=201)
async def submit_training(
    req: TrainingRequest, principal: Principal = Depends(current_principal)
):
    """Submit a fine-tuning job. Returns immediately; training runs in background.

    In per-user mode the source job must be the caller's, and so must the base model
    unless it is a pretrained one.
    """
    require_job_access(str(req.job_id), principal)
    # Only a registered model can be fine-tuned. The checkpoint name is derived from
    # it, so an unknown base would name an arbitrary key that registration then
    # refuses, after the whole run.
    if model_registry.get(req.base_model, owner=principal.owner_filter) is None:
        raise HTTPException(422, "base_model is not a registered model")

    training_job_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    job = {
        "training_job_id": training_job_id,
        "status": "queued",
        # Stored as the canonical string, which is what sessions hold in job_ids.
        "job_id": str(req.job_id),
        "base_model": req.base_model,
        "epochs": req.epochs,
        "custom_classes": req.custom_classes,
        "model_artifact": None,
        "error_message": None,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
    }

    with _lock:
        _training_jobs[training_job_id] = job
        _training_owners[training_job_id] = principal.owner

    # Kick off simulated training in background
    thread = threading.Thread(
        target=_simulate_training,
        args=(training_job_id, req.base_model, req.epochs),
        daemon=True,
    )
    thread.start()

    return job


@router.get("")
async def list_training_jobs(principal: Principal = Depends(current_principal)):
    """List the caller's training jobs (all in shared mode), newest first."""
    with _lock:
        jobs = sorted(
            (
                job
                for tj_id in _training_jobs
                if (job := _owned(tj_id, principal.owner_filter)) is not None
            ),
            key=lambda j: j["created_at"],
            reverse=True,
        )
    return {"training_jobs": jobs, "total": len(jobs)}


@router.get("/{training_job_id}")
async def get_training_job(
    training_job_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Get a single training job by ID."""
    with _lock:
        job = _owned(str(training_job_id), principal.owner_filter)
    if job is None:
        raise HTTPException(404, f"Training job not found: {training_job_id}")
    return job


@router.delete("/{training_job_id}")
async def delete_training_job(
    training_job_id: uuid.UUID, principal: Principal = Depends(current_principal)
):
    """Delete a training job and unregister any model it produced.

    Removing the model keeps the detection model selector consistent: a job that
    no longer exists must not leave a selectable model behind.

    A job still in flight is popped from the store here; its background thread
    re-reads the store under the same lock at each write point and returns early
    once the entry is gone, including immediately before registering its model.
    That last check is what prevents an in-flight completion from registering a
    model this call has already decided to remove.

    NOTE: the checkpoint artifact itself is not deleted from S3. The job record and
    the registry entry go, so the model stops being selectable, but the weights
    remain in the bucket.
    """
    tj_id = str(training_job_id)
    with _lock:
        job = None
        if _owned(tj_id, principal.owner_filter) is not None:
            job = _training_jobs.pop(tj_id, None)
            _training_owners.pop(tj_id, None)

    if job is None:
        raise HTTPException(404, f"Training job not found: {tj_id}")

    removed_models = model_registry.unregister_by_training_job(tj_id)

    return {
        "status": "deleted",
        "training_job_id": tj_id,
        "unregistered_models": removed_models,
    }


def _pretrained_root(base_model: str) -> str:
    """Resolve a base model id back to the pretrained model it descends from.

    Fine-tuned ids are derived from their base, so naively concatenating produces
    ``votenet_scannet_ft_a1b2c3d4_ft_e5f6a7b8`` by generation 3 and keeps growing.
    Resolving to the root keeps generated ids a fixed shape, and lets the UI hide
    the superseded pretrained baseline at any generation rather than only the first.
    """
    seen: set[str] = set()
    current = base_model
    # Bounded: a malformed chain must not loop.
    while current and current not in seen:
        seen.add(current)
        model = model_registry.get(current)
        if model is None:
            return current
        if model.get("type") == "pretrained":
            return current
        parent = model.get("base_model")
        if not parent or parent == current:
            return current
        current = parent
    return current


def _simulate_training(training_job_id: str, base_model: str, epochs: int) -> None:
    """Background thread that simulates training progress."""
    # Brief queued period.
    time.sleep(2)  # nosemgrep: arbitrary-sleep

    with _lock:
        if training_job_id not in _training_jobs:
            return
        _training_jobs[training_job_id]["status"] = "processing"
        _training_jobs[training_job_id]["started_at"] = datetime.now(UTC).isoformat()

    # Simulate training duration
    time.sleep(STUB_TRAINING_SECONDS)  # nosemgrep: arbitrary-sleep

    now = datetime.now(UTC).isoformat()
    root = _pretrained_root(base_model)
    # The key under the models bucket. The registry accepts only checkpoints there
    # (api/model_registry.py), so the model is registered with its s3:// URI.
    artifact = f"models/{root}_finetuned_{training_job_id[:8]}.pth"

    with _lock:
        if training_job_id not in _training_jobs:
            return
        _training_jobs[training_job_id]["status"] = "complete"
        _training_jobs[training_job_id]["completed_at"] = now
        _training_jobs[training_job_id]["model_artifact"] = artifact

    # Also update the linked training session (if any) to 'complete'
    _update_linked_session(training_job_id, artifact)

    # Resolve the display name outside the lock: it only affects presentation, and
    # _find_session_name walks session_store, which has its own locking.
    with _lock:
        job_snapshot = _training_jobs.get(training_job_id)
    session_name = _find_session_name(training_job_id, job_snapshot)

    model_id = f"{root}_ft_{training_job_id[:8]}"
    model_name = session_name or f"{base_model} (fine-tuned {training_job_id[:8]})"

    # Registration must be atomic with respect to DELETE, which pops the job under
    # this same lock before calling unregister_by_training_job().
    #
    # Registering unconditionally loses that race in one direction: a DELETE landing
    # between the "complete" write above and this point pops the job, finds nothing
    # to unregister, and then this thread registers a model whose training_job_id no
    # longer resolves. Because DELETE 404s on a missing job, no API call could ever
    # remove it again, and selectableModels() keeps offering the newest fine-tuned
    # model, so the orphan stays selectable, pointing at weights the user believes
    # they deleted.
    #
    # Checking and registering under the lock makes both interleavings correct:
    # if we win, DELETE's unregister call finds the model; if DELETE wins, we skip.
    with _lock:
        job = _training_jobs.get(training_job_id)
        if job is None:
            print(
                f"[TRAINING STUB] Job {training_job_id[:8]} was deleted before its "
                "model could be registered; skipping registration"
            )
            return
        registration_error: ModelRegistrationError | None = None
        try:
            model_registry.register(
                owner=_training_owners.get(training_job_id),
                model_id=model_id,
                name=model_name,
                base_model=base_model,
                checkpoint_path=checkpoint_uri(artifact),
                classes=job.get("custom_classes", []),
                training_job_id=training_job_id,
            )
        except ModelRegistrationError as e:
            # A taken id (two training jobs sharing an 8-character prefix) or no
            # models bucket configured. The run produced nothing selectable, so it is
            # reported failed rather than complete.
            job["status"] = "failed"
            job["model_artifact"] = None
            job["error_message"] = f"Model registration failed: {e}"
            registration_error = e

    if registration_error is not None:
        # Outside _lock: _update_linked_session takes it. No artifact marks the
        # linked session failed rather than complete.
        _update_linked_session(training_job_id, None)
        print(
            f"[TRAINING STUB] Job {training_job_id[:8]} registration failed: "
            f"{registration_error}"
        )
        return

    print(
        f"[TRAINING STUB] Job {training_job_id[:8]} complete → {artifact} "
        f"(model: {model_name})"
    )


def _find_session_name(training_job_id: str, job: dict | None) -> str | None:
    """Look up the training session name for a nicer model display name."""
    try:
        source_job_id = job.get("job_id") if job else None
        sessions = session_store.list_all()
        for session in sessions:
            linked = session.get("training_job_id") == training_job_id or (
                source_job_id and source_job_id in session.get("job_ids", [])
            )
            if linked:
                return session.get("name")
    except Exception:
        pass
    return None


def _update_linked_session(training_job_id: str, artifact: str | None) -> None:
    """Find and update any training session that triggered this job."""
    try:
        with _lock:
            job = _training_jobs.get(training_job_id)
        if not job:
            return

        source_job_id = job.get("job_id")
        sessions = session_store.list_all()

        for session in sessions:
            # Match by training_job_id (set when session's train endpoint is used)
            # OR by job_id membership (when training submitted from annotation viewer)
            linked = session.get("training_job_id") == training_job_id or (
                source_job_id and source_job_id in session.get("job_ids", [])
            )
            # Linkage alone, deliberately. This used to also require
            # status == "training", which only the since-removed
            # POST /training-sessions/{id}/train ever set — so once training moved
            # to the annotation viewer the condition could never be true and no
            # session ever recorded the model it produced. Sessions are created
            # "collecting" and stay there.
            if linked:
                session_store.set_training_result(
                    session["session_id"], training_job_id, artifact
                )
                print(f"[TRAINING STUB] Session {session['session_id'][:8]} → complete")
                break
    except Exception as e:
        print(f"[TRAINING STUB] Failed to update session: {e}")
