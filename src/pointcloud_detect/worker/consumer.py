"""SQS message consumer — polls for detection jobs and processes them."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid

import boto3

from pointcloud_detect.worker.json_logging import configure_json_logging
from pointcloud_detect.worker.parser_ipc import PARSER_MODE_ENV, parser_mode
from pointcloud_detect.worker.processor import (
    ACTIVE_JOB_STATUSES,
    _update_status,
    fetch_job_record,
    process_job,
)

logger = logging.getLogger(__name__)

QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", None)
POLL_WAIT_SECONDS = 20

# How long a message stays invisible while a worker holds it, and how often that is
# renewed while the job is still running. The renewal is what makes a job longer than
# the timeout safe: without it the message becomes visible again mid-run, a second
# worker starts the same job on its own GPU, and the delivery budget is consumed by a
# job that is progressing perfectly well. A 50 GB scene takes hours, so that is the
# normal case for large inputs rather than an edge case.
#
# SQS caps total invisibility at 12 hours from the first receive, so renewal cannot
# protect a job that runs longer than that; the extender logs and stops when the API
# refuses.
VISIBILITY_TIMEOUT_SECONDS = 900  # 15 minutes
VISIBILITY_EXTENSION_INTERVAL = 300  # 5 minutes, comfortably inside the timeout

# Must not exceed the queue's redrive maxReceiveCount (Terraform
# var.max_receive_count, default 3). On the delivery where the receive count
# reaches this, the job is marked failed, because SQS will move the message to the
# dead-letter queue instead of redelivering it and nothing will touch the job again.
#
# Erring low is safe and erring high is not. Lower than the queue's value marks a
# job failed while SQS still has retries left, and the job then stays failed: a
# redelivery for a failed job is refused as a replay (see _message_problem), so the
# cost is the retries SQS had left. Higher than the queue's value means the message
# reaches the DLQ before this threshold is ever hit and the job is never marked
# failed at all, which is the bug this exists to fix.
MAX_RECEIVE_COUNT = int(os.environ.get("SQS_MAX_RECEIVE_COUNT", "3"))

# DynamoDB items cap at 400 KB and the whole job record shares one item, so an
# unbounded traceback in error_message could fail the write that reports the failure.
MAX_ERROR_MESSAGE_CHARS = 1024

# Worker capability: 'auto' accepts all jobs, 'cpu' only accepts cpu/auto jobs,
# 'gpu' only accepts gpu/auto jobs. Set via WORKER_DEVICE env var.
#
# 'auto' is the default and the only value the chart sets unless worker.device is
# overridden, so a single homogeneous pool never declines a message and none of the
# deferral logic below runs. Anything else opts into that path, which spends delivery
# attempts on routing — see the comment in _handle_message.
WORKER_DEVICE = os.environ.get("WORKER_DEVICE", "auto")

# How long a declined message stays invisible before another worker can take it. Short
# on purpose: workers long-poll for 20s, so a capable one sees it on its current or
# next poll. The binding constraint is the delivery budget, not this interval — raising
# it only delays pickup without buying more attempts.
DEFERRAL_VISIBILITY_SECONDS = 5


class VisibilityExtender:
    """Keeps a message invisible for as long as its job is still running.

    Used as a context manager around processing. A daemon thread renews the message's
    visibility every ``interval`` seconds until the block exits.

    Renewal stops on the first failure rather than retrying. The usual reason for the
    API to refuse is that the message is no longer ours to extend — SQS's 12 hour cap
    on total invisibility has been reached, or the message has already been moved to
    the dead-letter queue — and in neither case does trying again help. Processing is
    left to finish; the job may simply be picked up again by another worker.

    Failures never propagate. A job that is running fine should not be killed because
    its bookkeeping call failed.
    """

    def __init__(
        self,
        sqs,
        queue_url: str,
        receipt_handle: str,
        job_id: str,
        # interval is a float because it is a wait duration; timeout is an int because
        # SQS requires VisibilityTimeout in whole seconds.
        interval: float = VISIBILITY_EXTENSION_INTERVAL,
        timeout: int = VISIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        self._sqs = sqs
        self._queue_url = queue_url
        self._receipt_handle = receipt_handle
        self._job_id = job_id
        self._interval = interval
        self._timeout = timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.extensions = 0

    def __enter__(self) -> VisibilityExtender:
        self._thread = threading.Thread(
            target=self._run, name=f"visibility-{self._job_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            # Bounded so a wedged renewal call cannot hold up the poll loop. The thread
            # is a daemon, so a straggler cannot keep the process alive either.
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Event.wait returns True once stopped and False on timeout, so this renews on
        # each interval and exits promptly when the block does, instead of sleeping out
        # the remaining interval.
        while not self._stop.wait(self._interval):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt_handle,
                    VisibilityTimeout=self._timeout,
                )
            except Exception as e:
                logger.warning(
                    "Stopped extending visibility for job %s after %d extension(s): "
                    "%s. The job keeps running, but its message may be redelivered.",
                    self._job_id,
                    self.extensions,
                    e,
                )
                return
            self.extensions += 1
            logger.debug(
                "Extended visibility for job %s by %ds (extension %d)",
                self._job_id,
                self._timeout,
                self.extensions,
            )


def _message_problem(job_id: object, s3_input_key: object) -> str | None:
    """Why a message must not be processed, or None if it matches its job.

    The body is untrusted: anything that can send to the queue can name any job,
    and the worker would then overwrite that job's results and mark it complete
    (T19). The API creates every job with a random UUID and a record before its
    message, so a message whose id is not a canonical UUID, whose job has no record,
    whose input key differs from the record's, or whose job is no longer queued or
    processing is a forgery or a replay. The record lookup can raise; the caller
    treats that as transient.
    """
    if not isinstance(job_id, str) or not isinstance(s3_input_key, str):
        return "job_id and s3_input_key must be strings"
    try:
        canonical = str(uuid.UUID(job_id))
    except ValueError:
        canonical = None
    if canonical != job_id:
        return "job_id is not a UUID"
    record = fetch_job_record(job_id)
    if record is None:
        return "there is no job record with this id"
    if record.get("s3_input_key") != s3_input_key:
        return "s3_input_key does not match the job record"
    status = record.get("status")
    if status not in ACTIVE_JOB_STATUSES:
        return f"the job is {status!r}, not queued or processing"
    return None


def _truncate_error(exc: Exception) -> str:
    """Render an exception for storage, bounded in length.

    The type name is kept because the message alone is often empty or ambiguous — a
    bare KeyError renders as just the missing key, and an OSError as just a number.
    """
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if len(text) <= MAX_ERROR_MESSAGE_CHARS:
        return text
    return text[: MAX_ERROR_MESSAGE_CHARS - 3] + "..."


class Worker:
    """SQS consumer with graceful shutdown support."""

    def __init__(self) -> None:
        self._running = True
        boto_kwargs: dict = {"region_name": AWS_REGION}
        if AWS_ENDPOINT_URL:
            boto_kwargs["endpoint_url"] = AWS_ENDPOINT_URL
        self._sqs = boto3.client("sqs", **boto_kwargs)
        self._s3 = boto3.client("s3", **boto_kwargs)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, signum, frame) -> None:
        logger.info("Shutdown signal received, finishing current job...")
        self._running = False

    def run(self) -> None:
        """Main loop: poll SQS and process messages."""
        logger.info("Worker started. Polling queue: %s", QUEUE_URL)

        while self._running:
            try:
                response = self._sqs.receive_message(
                    QueueUrl=QUEUE_URL,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=POLL_WAIT_SECONDS,
                    VisibilityTimeout=VISIBILITY_TIMEOUT_SECONDS,
                    # SQS omits system attributes unless they are asked for, so
                    # without this the handler cannot tell a first delivery from a
                    # last one and cannot know when to give up on a job.
                    MessageSystemAttributeNames=["ApproximateReceiveCount"],
                )
                messages = response.get("Messages", [])

                for message in messages:
                    if not self._running:
                        break
                    self._handle_message(message)

            except Exception:
                logger.exception("Error polling SQS")
                time.sleep(5)  # nosemgrep: arbitrary-sleep

    @staticmethod
    def _receive_count(message: dict) -> int:
        """How many times this message has been delivered, 1 on first receipt.

        Defaults to 1 rather than 0 if the attribute is missing, so a message that
        somehow arrives without it retries rather than being written off immediately.
        """
        try:
            return int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))
        except (TypeError, ValueError):
            return 1

    def _handle_message(self, message: dict) -> None:
        """Process a single SQS message."""
        receipt_handle = message["ReceiptHandle"]

        # Parsing sits in its own try because it used to sit outside one entirely: a
        # malformed body escaped this method and surfaced as "Error polling SQS", with
        # no job_id in scope to attribute it to. It is not deleted, so the redrive
        # policy moves it to the DLQ for inspection rather than it being lost.
        try:
            body = json.loads(message["Body"])
            job_id = body["job_id"]
            s3_input_key = body["s3_input_key"]
            config = body["config"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(
                "Unprocessable message body, leaving for the DLQ (receive %d/%d): %s",
                self._receive_count(message),
                MAX_RECEIVE_COUNT,
                e,
            )
            return

        # Checked before anything else, including the routing below, because every
        # later path writes to the named job's record or outputs. A rejected message
        # is not deleted, so it reaches the DLQ for inspection, and no job record is
        # touched on its behalf: the job it names may be someone else's.
        try:
            problem = _message_problem(job_id, s3_input_key)
        except Exception as e:
            logger.warning(
                "Could not check job %r against its record (receive %d/%d), leaving "
                "it for redelivery: %s",
                job_id,
                self._receive_count(message),
                MAX_RECEIVE_COUNT,
                e,
            )
            return
        if problem is not None:
            logger.error(
                "Rejecting message for job %r: %s. Leaving it for the DLQ "
                "(receive %d/%d).",
                job_id,
                problem,
                self._receive_count(message),
                MAX_RECEIVE_COUNT,
            )
            return
        if not isinstance(config, dict):
            logger.error("Message for job %s has a non-object config", job_id)
            return

        # Route based on device preference vs worker capability.
        #
        # Declining a message costs a delivery: SQS has no way to look at a message
        # without receiving it, and every receive counts against the redrive policy's
        # maxReceiveCount. So bouncing a job between mismatched workers spends the same
        # budget that exists to catch poison messages, and an unbounded bounce loop
        # would push a perfectly healthy job into the DLQ having never been attempted.
        # It would land there still marked "processing", because the failure handler
        # below is never reached on this path.
        #
        # Bouncing is therefore bounded: on the final delivery this worker stops
        # deferring and reports the job failed, naming the reason. That is a real
        # failure — no worker matching the request took it — and it is strictly better
        # than the job disappearing silently. The proper fix for heterogeneous pools is
        # one queue per pool, so routing never consumes a job's retry budget; see the
        # WORKER_DEVICE note in README.md.
        device_pref = config.get("device_preference", "auto")
        if not self._should_process(device_pref):
            receive_count = self._receive_count(message)
            if receive_count >= MAX_RECEIVE_COUNT:
                logger.error(
                    "Job %s wants device_preference=%s and this worker is %s. Final "
                    "delivery (%d/%d), so no matching worker took it; marking failed "
                    "rather than letting it reach the DLQ as 'processing'.",
                    job_id,
                    device_pref,
                    WORKER_DEVICE,
                    receive_count,
                    MAX_RECEIVE_COUNT,
                )
                _update_status(
                    job_id,
                    "failed",
                    error_message=(
                        f"No worker available for device_preference={device_pref!r} "
                        f"after {receive_count} delivery attempts."
                    ),
                )
                return

            logger.debug(
                "Deferring job %s (device_preference=%s, worker=%s, delivery %d/%d)",
                job_id,
                device_pref,
                WORKER_DEVICE,
                receive_count,
                MAX_RECEIVE_COUNT,
            )
            # Not deleted, so a capable worker can take it. Visibility is shortened so
            # it returns promptly rather than waiting out the full timeout.
            self._sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=DEFERRAL_VISIBILITY_SECONDS,
            )
            return

        logger.info("Processing job %s: %s", job_id, s3_input_key)

        try:
            # Renew visibility while the job runs. A job that outlasts the timeout was
            # otherwise redelivered mid-run: a second worker started the same job on a
            # second GPU, and the redelivery spent a retry attempt on a job that was
            # progressing fine. Hours-long runs on large scenes made that routine.
            # Interval and timeout are read here rather than relied on as parameter
            # defaults, which would bind them at class-definition time and make the
            # module constants impossible to override.
            with VisibilityExtender(
                self._sqs,
                QUEUE_URL,
                receipt_handle,
                job_id,
                interval=VISIBILITY_EXTENSION_INTERVAL,
                timeout=VISIBILITY_TIMEOUT_SECONDS,
            ) as extender:
                process_job(
                    job_id=job_id,
                    s3_input_key=s3_input_key,
                    config=config,
                    s3_client=self._s3,
                )

            # Success — delete message
            self._sqs.delete_message(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=receipt_handle,
            )
            logger.info(
                "Job %s completed successfully (%d visibility extension(s))",
                job_id,
                extender.extensions,
            )

        except Exception as e:
            logger.exception("Job %s failed: %s", job_id, e)
            # The message is never deleted here, so SQS redelivers it until the redrive
            # policy moves it to the DLQ. What matters is the job record: on the final
            # delivery nothing will touch this job again, so if it is not marked failed
            # now it stays "processing" until its TTL expires. The dashboard already
            # renders failed jobs with the error and a retry button; it just never had
            # anything to render.
            receive_count = self._receive_count(message)
            if receive_count >= MAX_RECEIVE_COUNT:
                logger.error(
                    "Job %s failed on final delivery (%d/%d), marking failed",
                    job_id,
                    receive_count,
                    MAX_RECEIVE_COUNT,
                )
                _update_status(
                    job_id,
                    "failed",
                    error_message=_truncate_error(e),
                )
            else:
                logger.warning(
                    "Job %s failed on delivery %d/%d, leaving it for redelivery",
                    job_id,
                    receive_count,
                    MAX_RECEIVE_COUNT,
                )

    def _should_process(self, device_pref: str) -> bool:
        """Check if this worker should handle a job based on device preference.

        Routing rules:
        - Worker 'auto': accepts everything
        - Worker 'cpu': accepts 'cpu' and 'auto' jobs (not 'gpu')
        - Worker 'gpu': accepts 'gpu' and 'auto' jobs (not 'cpu')
        - Job 'auto': any worker can pick it up
        - Job 'cpu': only cpu or auto workers
        - Job 'gpu': only gpu or auto workers
        """
        if WORKER_DEVICE == "auto":
            return True
        if device_pref == "auto":
            return True
        return device_pref == WORKER_DEVICE


def main() -> None:
    """Entry point for the worker process."""
    configure_json_logging()

    if not QUEUE_URL:
        logger.error("SQS_QUEUE_URL environment variable not set")
        sys.exit(1)

    try:
        mode = parser_mode()
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)
    if mode == "sidecar":
        # Files the worker leaves in the spool (the download, the request) are
        # for the parser container, which shares this process's group only.
        os.umask(0o007)
    logger.info("Parsing input files %s (%s)", mode, PARSER_MODE_ENV)

    worker = Worker()
    worker.run()


if __name__ == "__main__":
    main()
