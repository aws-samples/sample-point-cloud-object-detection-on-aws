"""Job store: shared job state for the API and worker.

Two backends implement the same interface:

- ``InMemoryJobStore`` — a process-local dict. Fine for local dev and tests, but
  incorrect for a multi-replica deployment: each replica has its own dict, so the
  dashboard's job list flickers (a poll may hit a replica that never saw the job)
  and status updates can land on a replica that does not hold the job, leaving jobs
  stuck in "processing".
- ``DynamoJobStore`` — a DynamoDB-backed store. Job state is shared across every
  replica and survives restarts, which is what the deployment needs.

The backend is chosen by ``JOB_STORE_BACKEND`` (``memory`` default, ``dynamodb`` in
the cluster). When ``dynamodb`` is selected, ``JOBS_TABLE_NAME`` must be set.
"""

import json
import logging
import os
import time
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol

from pointcloud_detect.api.models import JobResponse, JobStatus
from pointcloud_detect.api.ownership import ANY_OWNER, OwnerFilter, owner_matches

logger = logging.getLogger(__name__)

# Bound on optimistic-concurrency retries in DynamoJobStore.update_status. Contention
# is between at most a couple of writers (worker status vs API detection_count), so a
# handful of attempts is plenty; the bound exists so a pathological hot-spot cannot
# spin forever holding a request open.
_UPDATE_MAX_ATTEMPTS = 5

# Default retention for job records. The job list is served by a full table Scan, so
# without expiry the table — and every list call — grows without bound. 0 disables TTL.
_DEFAULT_JOBS_TTL_DAYS = 90


def _jobs_ttl_days() -> int:
    """Retention in days for job records, from ``JOBS_TTL_DAYS``.

    Must match the TTL attribute configured on the table (``expires_at``). A
    non-numeric or negative value falls back to the default rather than disabling
    expiry silently, since unbounded growth is the failure mode being avoided.
    """
    raw = os.environ.get("JOBS_TTL_DAYS")
    if raw is None or raw.strip() == "":
        return _DEFAULT_JOBS_TTL_DAYS
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "JOBS_TTL_DAYS=%r is not an integer; using default %d",
            raw,
            _DEFAULT_JOBS_TTL_DAYS,
        )
        return _DEFAULT_JOBS_TTL_DAYS
    if days < 0:
        logger.warning(
            "JOBS_TTL_DAYS=%d is negative; using default %d",
            days,
            _DEFAULT_JOBS_TTL_DAYS,
        )
        return _DEFAULT_JOBS_TTL_DAYS
    return days


def _status_value(status: JobStatus | str) -> str:
    # JobStatus is a StrEnum, so str() yields its value ("complete") for the enum
    # and passes a plain string through unchanged.
    return str(status)


def _apply_updates(
    data: dict,
    status: JobStatus | str | None,
    detection_count: int | None,
    tile_count: int | None,
    processing_time_s: float | None,
    error_message: str | None,
) -> dict:
    """Apply a partial status update to a job record dict (in place) and return it."""
    if status is not None:
        data["status"] = _status_value(status)
        now = datetime.now(UTC).isoformat()
        if status == JobStatus.processing:
            data["started_at"] = now
        elif status in (JobStatus.complete, JobStatus.failed):
            data["completed_at"] = now
    if detection_count is not None:
        data["detection_count"] = detection_count
    if tile_count is not None:
        data["tile_count"] = tile_count
    if processing_time_s is not None:
        data["processing_time_s"] = processing_time_s
    if error_message is not None:
        data["error_message"] = error_message
    return data


class JobStore(Protocol):
    """Interface shared by the in-memory and DynamoDB stores.

    ``owner`` is the Cognito user id of the user who created a job, in per-user mode
    (threat model T31); None in shared mode. Reads and lists take an owner filter: a
    user id selects that user's jobs only, and ANY_OWNER, the default, every job,
    which is what shared mode and the worker use. The owner is not part of the
    JobResponse, so it is never returned to a client.
    """

    def create(self, job: JobResponse, owner: str | None = None) -> None: ...

    def get(
        self, job_id: str, owner: OwnerFilter = ANY_OWNER
    ) -> JobResponse | None: ...

    def list(
        self, status: str | None = None, owner: OwnerFilter = ANY_OWNER
    ) -> list[JobResponse]: ...

    def update_status(
        self,
        job_id: str,
        status: JobStatus | None = None,
        detection_count: int | None = None,
        tile_count: int | None = None,
        processing_time_s: float | None = None,
        error_message: str | None = None,
    ) -> None: ...

    def delete(self, job_id: str) -> None: ...

    def clear(self) -> None: ...


class InMemoryJobStore:
    """Thread-safe process-local job store (local dev / tests only)."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._owners: dict[str, str | None] = {}
        self._lock = Lock()

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._owners.clear()

    def create(self, job: JobResponse, owner: str | None = None) -> None:
        with self._lock:
            self._jobs[job.job_id] = job.model_dump()
            self._owners[job.job_id] = owner

    def get(self, job_id: str, owner: OwnerFilter = ANY_OWNER) -> JobResponse | None:
        with self._lock:
            data = self._jobs.get(job_id)
            record_owner = self._owners.get(job_id)
        if data is None or not owner_matches(record_owner, owner):
            return None
        return JobResponse(**data)

    def list(
        self, status: str | None = None, owner: OwnerFilter = ANY_OWNER
    ) -> list[JobResponse]:
        with self._lock:
            jobs = [
                data
                for job_id, data in self._jobs.items()
                if owner_matches(self._owners.get(job_id), owner)
            ]
        result = [JobResponse(**j) for j in jobs]
        if status:
            result = [j for j in result if j.status == status]
        result.sort(key=lambda j: j.created_at, reverse=True)
        return result

    def delete(self, job_id: str) -> None:
        """Remove a job record. Deleting an unknown id is a no-op."""
        with self._lock:
            self._jobs.pop(job_id, None)
            self._owners.pop(job_id, None)

    def update_status(
        self,
        job_id: str,
        status: JobStatus | None = None,
        detection_count: int | None = None,
        tile_count: int | None = None,
        processing_time_s: float | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            if job_id not in self._jobs:
                logger.warning(
                    "update_status for unknown job_id %s (known: %d)",
                    job_id,
                    len(self._jobs),
                )
                return
            _apply_updates(
                self._jobs[job_id],
                status,
                detection_count,
                tile_count,
                processing_time_s,
                error_message,
            )


class DynamoJobStore:
    """DynamoDB-backed job store shared across API replicas and the worker.

    Each job is one item keyed by ``job_id``. The full ``JobResponse`` is stored as a
    JSON string in ``record`` so we do not have to map nested config / datetimes /
    floats to DynamoDB attribute types (and back). ``status`` and ``created_at`` are
    duplicated as top-level attributes so listing can filter and sort without
    deserializing every item's JSON.
    """

    def __init__(self, table_name: str, region: str | None = None) -> None:
        import boto3  # imported lazily so local dev / tests need no AWS deps

        # boto3's resource API is dynamically generated and unstubbed, so pyright
        # cannot see .Table on the resource. The call is correct at runtime.
        region_name = region or os.environ.get("AWS_REGION", "us-east-1")
        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._table = dynamodb.Table(table_name)  # pyright: ignore[reportAttributeAccessIssue]
        self._ttl_days = _jobs_ttl_days()

    def _item(
        self,
        job_id: str,
        data: dict,
        version: int,
        expires_at: int | None = None,
        owner_id: str | None = None,
    ) -> dict:
        """Build the stored item. ``status``/``created_at`` are duplicated top-level
        for listing; ``version`` guards concurrent updates; ``expires_at`` is the
        table's TTL attribute so job records do not accumulate forever; ``owner_id``
        is the user who created the job, in per-user mode.

        ``owner_id`` is carried over by update_status like ``expires_at``: the worker
        rewrites the item without knowing the owner, and a job that lost it would
        belong to no one.

        ``expires_at`` must be carried over by callers that rewrite an existing item.
        Updates replace the whole item, so recomputing the expiry here would restart
        the retention clock on every status change: a job whose annotations are edited
        periodically would never expire, and neither would a permanently failing job,
        because nothing marks jobs failed in the cluster and each SQS redelivery
        rewrites "processing". Those are the rows most worth collecting, so retention
        runs from creation. Passing ``None`` for an item that has no expiry yet starts
        the clock now, which is what lets records written before this attribute
        existed expire once they are next touched.
        """
        item = {
            "job_id": job_id,
            "status": data.get("status", ""),
            "created_at": data.get("created_at", ""),
            "record": json.dumps(data),
            "version": version,
        }
        if expires_at is not None:
            item["expires_at"] = expires_at
        elif self._ttl_days > 0:
            item["expires_at"] = int(time.time()) + self._ttl_days * 86400
        if owner_id is not None:
            item["owner_id"] = owner_id
        return item

    def create(self, job: JobResponse, owner: str | None = None) -> None:
        data = job.model_dump(mode="json")
        self._table.put_item(Item=self._item(job.job_id, data, 0, owner_id=owner))

    @staticmethod
    def _expired(item: dict, now: float) -> bool:
        """True once an item is past its ``expires_at``.

        DynamoDB's TTL deletes expired items lazily, typically within a few days, and
        until then reads still return them. Retention is what the setting says only if
        reads treat an expired record as gone.
        """
        expires_at = item.get("expires_at")
        return expires_at is not None and int(expires_at) <= now

    def get(self, job_id: str, owner: OwnerFilter = ANY_OWNER) -> JobResponse | None:
        resp = self._table.get_item(Key={"job_id": job_id})
        item = resp.get("Item")
        if (
            item is None
            or self._expired(item, time.time())
            or not owner_matches(item.get("owner_id"), owner)
        ):
            return None
        return JobResponse(**json.loads(item["record"]))

    def list(
        self, status: str | None = None, owner: OwnerFilter = ANY_OWNER
    ) -> list[JobResponse]:
        # The UI lists a bounded number of jobs; a scan is acceptable here. If the
        # table grows large this should move to a GSI on (status, created_at).
        items: list[dict] = []
        kwargs: dict = {}
        while True:
            resp = self._table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            key = resp.get("LastEvaluatedKey")
            if not key:
                break
            kwargs["ExclusiveStartKey"] = key

        now = time.time()
        # Filtered here, as status is, rather than in the Scan: the Scan reads the
        # whole table either way (T43).
        result = [
            JobResponse(**json.loads(i["record"]))
            for i in items
            if not self._expired(i, now) and owner_matches(i.get("owner_id"), owner)
        ]
        if status:
            result = [j for j in result if j.status == status]
        result.sort(key=lambda j: j.created_at, reverse=True)
        return result

    def update_status(
        self,
        job_id: str,
        status: JobStatus | None = None,
        detection_count: int | None = None,
        tile_count: int | None = None,
        processing_time_s: float | None = None,
        error_message: str | None = None,
    ) -> None:
        """Apply a partial update under optimistic concurrency control.

        The whole ``JobResponse`` lives in one JSON attribute, so any update is a
        read-modify-write and two concurrent writers can lose one of the two edits.
        This is not hypothetical: the worker writes status transitions
        (``worker/processor.py``) while the API writes ``detection_count`` when
        annotations are saved (``api/routes/annotations.py``) and on the job route.
        A blind ``put_item`` lets the slower writer overwrite the faster one's field.

        So each item carries a ``version``, and the write is conditional on the
        version still being the one that was read. If another writer got there
        first the condition fails, and we re-read and re-apply rather than
        clobbering. ``attribute_not_exists(version)`` is accepted too, so records
        written before this attribute existed still update.
        """
        conditional_failed = (
            self._table.meta.client.exceptions.ConditionalCheckFailedException
        )
        for attempt in range(_UPDATE_MAX_ATTEMPTS):
            resp = self._table.get_item(Key={"job_id": job_id})
            item = resp.get("Item")
            if item is None:
                logger.warning("update_status for unknown job_id %s", job_id)
                return
            version = int(item.get("version", 0))
            data = json.loads(item["record"])
            _apply_updates(
                data,
                status,
                detection_count,
                tile_count,
                processing_time_s,
                error_message,
            )
            existing_expiry = item.get("expires_at")
            try:
                self._table.put_item(
                    Item=self._item(
                        job_id,
                        data,
                        version + 1,
                        expires_at=int(existing_expiry)
                        if existing_expiry is not None
                        else None,
                        owner_id=item.get("owner_id"),
                    ),
                    ConditionExpression=(
                        "attribute_not_exists(version) OR version = :v"
                    ),
                    ExpressionAttributeValues={":v": version},
                )
                return
            except conditional_failed:
                # Someone else updated this job between our read and write. Re-read
                # and re-apply our partial update on top of their result.
                logger.debug(
                    "update_status contention on job %s (attempt %d), retrying",
                    job_id,
                    attempt + 1,
                )
                continue
        logger.warning(
            "update_status for job %s gave up after %d contended attempts; "
            "this update was not applied",
            job_id,
            _UPDATE_MAX_ATTEMPTS,
        )

    def delete(self, job_id: str) -> None:
        """Remove a job record (``dynamodb:DeleteItem``). An unknown id is a no-op.

        The table's point-in-time recovery still holds the record for its recovery
        window; that is backup retention, not a copy the API can serve.
        """
        self._table.delete_item(Key={"job_id": job_id})

    def clear(self) -> None:
        # Delete every item. Intended for tests; production never calls this.
        # Paginated for the same reason list() is: a single scan() returns at most
        # 1 MB, so without following LastEvaluatedKey this would silently leave
        # items behind and a test asserting an empty store would fail confusingly.
        kwargs: dict = {"ProjectionExpression": "job_id"}
        with self._table.batch_writer() as batch:
            while True:
                resp = self._table.scan(**kwargs)
                for item in resp.get("Items", []):
                    batch.delete_item(Key={"job_id": item["job_id"]})
                key = resp.get("LastEvaluatedKey")
                if not key:
                    break
                kwargs["ExclusiveStartKey"] = key


JOB_STORE_BACKENDS = ("memory", "dynamodb")


def configured_backend() -> str:
    """JOB_STORE_BACKEND, normalised; ``memory`` when unset."""
    return os.environ.get("JOB_STORE_BACKEND", "memory").strip().lower()


def _build_store() -> JobStore:
    backend = configured_backend()
    if backend == "dynamodb":
        table = os.environ.get("JOBS_TABLE_NAME")
        if not table:
            raise RuntimeError(
                "JOB_STORE_BACKEND=dynamodb requires JOBS_TABLE_NAME to be set"
            )
        logger.info("Using DynamoDB job store (table=%s)", table)
        return DynamoJobStore(table)
    if backend == "memory":
        logger.info("Using in-memory job store (local dev / single replica only)")
        return InMemoryJobStore()
    # Anything else is rejected rather than silently treated as "memory". Falling
    # back would hand a multi-replica deployment the process-local dict this store
    # exists to replace — the dashboard would flicker and jobs would stick in
    # "processing", which is exactly the failure a typo like JOB_STORE_BACKEND=dynamo
    # should not be able to reintroduce silently.
    raise RuntimeError(
        f"JOB_STORE_BACKEND={backend!r} is not a recognised job store backend. "
        f"Valid values: {', '.join(JOB_STORE_BACKENDS)}. Refusing to fall back to the "
        "in-memory store, which is incorrect for more than one API replica."
    )


# Singleton instance selected from the environment.
job_store: JobStore = _build_store()
