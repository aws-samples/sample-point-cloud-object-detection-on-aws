"""Object checks the routes share (threat model T31).

In per-user mode an object the caller does not own is answered exactly as a missing
one is, so ids cannot be probed for whether someone else's object exists.
"""

from fastapi import HTTPException

from pointcloud_detect.api.models import JobResponse
from pointcloud_detect.api.ownership import Principal, input_key_owner
from pointcloud_detect.api.store import job_store


def owned_job(job_id: str, principal: Principal) -> JobResponse:
    """The job, if it exists and the caller may reach it; otherwise 404."""
    job = job_store.get(job_id, owner=principal.owner_filter)
    if job is None:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job


def require_job_access(job_id: str, principal: Principal) -> None:
    """In per-user mode, 404 unless ``job_id`` is one of the caller's jobs.

    For routes that, in shared mode, never needed the job record: tiles are read
    straight from S3, and annotations can be saved for any id. Shared mode keeps that.
    """
    if principal.owner is not None:
        owned_job(job_id, principal)


def require_own_upload(s3_key: str, principal: Principal) -> None:
    """404 unless the caller may use the upload at ``s3_key``.

    In per-user mode only keys under the caller's own ``uploads/<user id>/`` qualify,
    so no user can start a job on, complete or abort another user's upload. A key
    the API did not issue in per-user mode, such as a file staged by hand under
    ``uploads/<id>/``, belongs to no user.
    """
    if not principal.can_access(input_key_owner(s3_key)):
        raise HTTPException(404, "Upload not found")
