"""Job management routes — create, list, get, delete, update status."""

import json
import logging
import os
import uuid
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query

from pointcloud_detect.api.access import owned_job, require_own_upload
from pointcloud_detect.api.deps import get_s3_client, get_settings, get_sqs_client
from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.models import (
    JobCreateRequest,
    JobListResponse,
    JobResponse,
    JobStatus,
    validate_input_key,
)
from pointcloud_detect.api.ownership import ANY_OWNER, OwnerFilter, Principal
from pointcloud_detect.api.routes.annotations import forget_annotations
from pointcloud_detect.api.store import job_store

logger = logging.getLogger(__name__)

router = APIRouter()

# A job in these states may still be read or written by a worker.
IN_FLIGHT = frozenset({JobStatus.queued, JobStatus.processing})


def _needs_pretrained_weights(config, owner: OwnerFilter = ANY_OWNER) -> bool:
    """Whether the worker would load a pretrained OpenMMLab checkpoint for this job.

    Mirrors worker/processor._build_pipeline_config: without a registered model with
    its own checkpoint, detection runs the architecture's pretrained weights. An id
    the registry does not know is left for the worker to reject.
    """
    from pointcloud_detect.api.model_registry import model_registry

    if config.job_type != "detection":
        return False
    custom = config.custom_weights_version
    if not custom or custom == "pretrained":
        return True
    model = model_registry.get(custom, owner=owner)
    return model is not None and model["checkpoint_path"] == "pretrained"


def _require_usable_model(config, principal: Principal) -> None:
    """In per-user mode, 422 unless the selected model is one the caller may use.

    A pretrained model, or one of the caller's own fine-tuned models (T31). An id no
    one registered and another user's model get the same answer. Shared mode leaves
    an unknown id to the worker, which refuses it (T18).
    """
    from pointcloud_detect.api.model_registry import model_registry

    custom = config.custom_weights_version
    if principal.owner is None or not custom or custom == "pretrained":
        return
    if model_registry.get(custom, owner=principal.owner_filter) is None:
        raise HTTPException(422, "custom_weights_version is not a model you can use")


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    request: JobCreateRequest, principal: Principal = Depends(current_principal)
):
    """Create a detection job and queue it for processing.

    In per-user mode the input must be the caller's own upload (T31): a job's tiles
    rebuild its whole input scan, so a job on someone else's key would read it.
    """
    require_own_upload(request.s3_key, principal)
    _require_usable_model(request.config, principal)
    from pointcloud_detect.backends.checkpoints import (
        DISABLED_MESSAGE,
        pretrained_selection_allowed,
    )

    # Refused here rather than left to the worker, which would retry it on every
    # SQS delivery before reporting it failed.
    if (
        _needs_pretrained_weights(request.config, principal.owner_filter)
        and not pretrained_selection_allowed()
    ):
        raise HTTPException(400, DISABLED_MESSAGE)

    settings = get_settings()
    sqs = get_sqs_client()

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    job = JobResponse(
        job_id=job_id,
        filename=request.filename,
        s3_input_key=request.s3_key,
        config=request.config,
        status=JobStatus.queued,
        created_at=now,
    )

    # Store job
    job_store.create(job, owner=principal.owner)

    # Queue SQS message
    message_body = json.dumps(
        {
            "job_id": job_id,
            "s3_input_key": request.s3_key,
            "config": request.config.model_dump(),
            "created_at": now.isoformat(),
        }
    )

    try:
        sqs.send_message(QueueUrl=settings.sqs_queue_url, MessageBody=message_body)
    except (BotoCoreError, ClientError) as e:
        # The record is already written. Left as it was, it would sit in "queued"
        # forever with nothing to process it (T66), so it is marked failed, which
        # the dashboard offers to retry.
        logger.exception("Could not queue job %s", job_id)
        job_store.update_status(
            job_id,
            status=JobStatus.failed,
            error_message="The job could not be queued. Submit it again.",
        )
        raise HTTPException(
            503, "The job could not be queued; it is marked failed. Submit it again."
        ) from e

    return job


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: JobStatus | None = Query(None),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(current_principal),
):
    """List the caller's jobs (every job in shared mode), newest first."""
    jobs = job_store.list(status=status, owner=principal.owner_filter)
    total = len(jobs)
    page = jobs[offset : offset + limit]
    return JobListResponse(jobs=page, total=total)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(job_id: uuid.UUID, principal: Principal = Depends(current_principal)):
    """Get a single job by ID."""
    return owned_job(str(job_id), principal)


class _DeleteFailed(Exception):
    """S3 reported per-key failures for a batch delete."""


def _delete_prefix(s3, bucket: str, prefix: str) -> int:
    """Delete every object under ``prefix``; return how many were deleted.

    ListObjectsV2 pages hold at most 1,000 keys, which is also DeleteObjects' limit
    per request. DeleteObjects answers 200 even when individual keys fail, so its
    ``Errors`` list is checked rather than trusted to raise.
    """
    deleted = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix
    ):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not objects:
            continue
        resp = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": objects, "Quiet": True}
        )
        errors = resp.get("Errors", [])
        if errors:
            raise _DeleteFailed(
                f"{len(errors)} object(s) under {prefix} could not be deleted "
                f"(first: {errors[0].get('Code', 'unknown error')})"
            )
        deleted += len(objects)
    return deleted


def _jobs_sharing_input(job: JobResponse) -> list[JobResponse]:
    """Every other job record whose input is this job's, whoever owns it.

    Across owners on purpose: this guards an input another job still reads, and a
    per-user key cannot be another user's input anyway.
    """
    return [
        other
        for other in job_store.list()
        if other.job_id != job.job_id and other.s3_input_key == job.s3_input_key
    ]


@router.delete("/{job_id}")
def delete_job(job_id: uuid.UUID, principal: Principal = Depends(current_principal)):
    """Delete a job: its input scan, results, tiles, annotations and record (T60).

    A plain ``def``, so FastAPI runs it in its threadpool: the S3 calls block, and a
    large job's tiles take several round trips.

    Refused with 409 while the job is queued or processing, and while another job
    that reuses the same input (the dashboard's Retry does) is. A worker still
    holding the job would otherwise write results and tiles back after they were
    deleted, leaving data no record points to. A job that never leaves
    "processing", because its worker was killed, stays undeletable here until it is
    marked failed or its record expires (JOBS_TTL_DAYS).

    Order matters. The stored data goes first and the record last, so a failure
    part-way leaves the record, and the same DELETE can be retried; each step is
    idempotent. What is deleted:

    - the input object at ``s3_input_key``, only if it is a valid ``uploads/`` key
      and no other job record still uses it (the last one to go deletes it);
    - everything under ``results/<id>/`` in the output bucket and ``tiles/<id>/`` in
      the tiles bucket;
    - saved annotations, and the job record.

    The buckets are versioned, so S3 keeps the deleted objects as noncurrent
    versions until the buckets' lifecycle rules expire them, and DynamoDB's
    point-in-time recovery keeps the record for its window. Neither is readable
    through the API.
    """
    jid = str(job_id)
    job = owned_job(jid, principal)
    if job.status in IN_FLIGHT:
        raise HTTPException(
            409,
            f"Job is {job.status}. Delete it once it has completed or failed.",
        )
    sharing = _jobs_sharing_input(job)
    if any(other.status in IN_FLIGHT for other in sharing):
        raise HTTPException(
            409,
            "Another job using the same input is still queued or processing. "
            "Delete this one once that job has completed or failed.",
        )

    settings = get_settings()
    s3 = get_s3_client()
    deleted = 0
    try:
        try:
            input_key = validate_input_key(job.s3_input_key)
        except ValueError:
            # A record written before keys were validated. Its key may name any
            # object, so nothing is deleted on its say-so.
            logger.warning("Job %s has an invalid input key; input not deleted", jid)
            input_key = None
        # Asked again, just before the delete: a Retry created since the check above
        # would otherwise lose its input. create_job writes its record before it
        # queues anything, so a job that is not listed here was created after this
        # point, and is no different from one created after the delete.
        if input_key is not None and (sharing or _jobs_sharing_input(job)):
            # Another job (a Retry) still reads this upload; it goes with the last.
            logger.info("Job %s shares its input with another job; input kept", jid)
            input_key = None
        if input_key is not None:
            s3.delete_object(Bucket=settings.s3_input_bucket, Key=input_key)
            deleted += 1
        deleted += _delete_prefix(s3, settings.s3_output_bucket, f"results/{jid}/")
        deleted += _delete_prefix(s3, settings.s3_tiles_bucket, f"tiles/{jid}/")
    except (BotoCoreError, ClientError, _DeleteFailed) as e:
        logger.exception("Deleting the stored data of job %s failed", jid)
        raise HTTPException(
            502,
            "Some of the job's stored data could not be deleted. The job was kept; "
            "try again.",
        ) from e

    forget_annotations(jid)
    job_store.delete(jid)
    logger.info("Deleted job %s and %d stored object(s)", jid, deleted)
    return {"status": "deleted", "job_id": jid, "deleted_objects": deleted}


@router.put("/{job_id}/status")
async def update_job_status(
    job_id: uuid.UUID,
    status: JobStatus,
    detection_count: int | None = None,
    tile_count: int | None = None,
    processing_time_s: float | None = None,
    error_message: str | None = None,
    principal: Principal = Depends(current_principal),
):
    """Update job status (called by worker on completion/failure).

    Only served with the in-memory store, where the worker shares no state with the
    API and this callback is its only way to report. With DynamoDB the worker writes
    the table directly (worker/processor.py _update_status), so this route has no
    legitimate caller, and leaving it open let any API client mark any job complete
    or failed with a message of its choosing.
    """
    if os.environ.get("JOB_STORE_BACKEND", "memory").strip().lower() != "memory":
        raise HTTPException(
            403, "Status updates are written by the worker directly to the job store."
        )
    jid = str(job_id)
    owned_job(jid, principal)

    job_store.update_status(
        jid,
        status=status,
        detection_count=detection_count,
        tile_count=tile_count,
        processing_time_s=processing_time_s,
        error_message=error_message,
    )
    return {"status": "updated"}
