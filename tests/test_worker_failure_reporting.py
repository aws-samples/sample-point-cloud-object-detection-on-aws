"""Tests for how the SQS consumer reports job failures.

Before this, a job whose processing raised was logged and left for redelivery, and
nothing ever marked it failed. SQS bounds the retries (redrive maxReceiveCount, 3 by
default) and then moves the message to the DLQ, at which point nothing touches the job
again — so the record sat at "processing" until its TTL expired. The dashboard already
renders a red badge, the error text and a retry button for failed jobs; it simply never
had anything to render.

The consumer had no test coverage at all, so these build the scaffolding: a fake SQS
client recording calls, and a Worker built via __new__ to skip boto3 construction (the
pattern the existing shutdown test already uses).
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

import pytest

from pointcloud_detect.worker import consumer as consumer_mod
from pointcloud_detect.worker.consumer import (
    MAX_ERROR_MESSAGE_CHARS,
    Worker,
    _truncate_error,
)


class FakeSQS:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.visibility_calls: list[dict[str, Any]] = []

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> None:  # noqa: N803
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(
        self,
        QueueUrl: str,  # noqa: N803
        ReceiptHandle: str,  # noqa: N803
        VisibilityTimeout: int,  # noqa: N803
    ) -> None:
        self.visibility_calls.append(
            {"receipt": ReceiptHandle, "timeout": VisibilityTimeout}
        )


# The consumer only processes a message whose job_id is a UUID with a matching,
# active job record (T19), so every message here names this job and the record
# lookup is faked to agree with it.
JOB_ID = "6f1c1c9e-2d8b-4a57-9d1e-0c2b7f3a9e41"
INPUT_KEY = f"input/{JOB_ID}/scene.las"


@pytest.fixture(autouse=True)
def job_record(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    record: dict[str, Any] = {
        "job_id": JOB_ID,
        "s3_input_key": INPUT_KEY,
        "status": "queued",
    }
    monkeypatch.setattr(
        consumer_mod,
        "fetch_job_record",
        lambda job_id: dict(record) if job_id == JOB_ID else None,
    )
    return record


def _message(receive_count: int = 1, body: dict | str | None = None) -> dict:
    if body is None:
        body = {
            "job_id": JOB_ID,
            "s3_input_key": INPUT_KEY,
            "config": {"device_preference": "auto"},
        }
    return {
        "ReceiptHandle": "rh-1",
        "Body": body if isinstance(body, str) else json.dumps(body),
        "Attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> Worker:
    w = Worker.__new__(Worker)
    w._running = True
    w._sqs = FakeSQS()  # pyright: ignore[reportAttributeAccessIssue]
    w._s3 = object()  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(consumer_mod, "WORKER_DEVICE", "auto")
    monkeypatch.setattr(consumer_mod, "MAX_RECEIVE_COUNT", 3)
    return w


@pytest.fixture
def status_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def record(job_id: str, status: str, **kwargs: Any) -> None:
        calls.append({"job_id": job_id, "status": status, **kwargs})

    monkeypatch.setattr(consumer_mod, "_update_status", record)
    return calls


def _fake_sqs(worker: Worker) -> FakeSQS:
    return cast(FakeSQS, worker._sqs)


def _raise(exc: Exception):
    def _inner(**kwargs: Any) -> None:
        raise exc

    return _inner


class TestSuccess:
    def test_successful_job_deletes_the_message(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(consumer_mod, "process_job", lambda **kw: None)
        worker._handle_message(_message())
        assert _fake_sqs(worker).deleted == ["rh-1"]
        assert status_calls == []


class TestFailureBeforeFinalDelivery:
    @pytest.mark.parametrize("receive_count", [1, 2])
    def test_job_is_left_for_redelivery_and_not_marked_failed(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        receive_count: int,
    ) -> None:
        # Marking failed here would flash a red badge and a retry button at the user
        # for what may be a transient S3 error that the next delivery resolves.
        monkeypatch.setattr(
            consumer_mod, "process_job", _raise(RuntimeError("s3 throttled"))
        )
        worker._handle_message(_message(receive_count=receive_count))
        assert status_calls == []
        assert _fake_sqs(worker).deleted == []


class TestFailureOnFinalDelivery:
    def test_job_is_marked_failed_with_the_error(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            consumer_mod, "process_job", _raise(ValueError("unsupported E57 variant"))
        )
        worker._handle_message(_message(receive_count=3))

        assert len(status_calls) == 1
        call = status_calls[0]
        assert call["job_id"] == JOB_ID
        assert call["status"] == "failed"
        assert call["error_message"] == "ValueError: unsupported E57 variant"

    def test_message_is_not_deleted_so_it_reaches_the_dlq(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Deleting after marking failed would lose the forensic copy of the message.
        monkeypatch.setattr(consumer_mod, "process_job", _raise(RuntimeError("boom")))
        worker._handle_message(_message(receive_count=3))
        assert _fake_sqs(worker).deleted == []

    def test_receive_count_past_the_threshold_still_marks_failed(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A redelivery mid-flight can push the count past the threshold; the
        # comparison must be >= rather than ==.
        monkeypatch.setattr(consumer_mod, "process_job", _raise(RuntimeError("boom")))
        worker._handle_message(_message(receive_count=7))
        assert [c["status"] for c in status_calls] == ["failed"]


class TestUnprocessableBody:
    @pytest.mark.parametrize(
        "body",
        [
            "not json at all",
            {"s3_input_key": INPUT_KEY, "config": {}},  # no job_id
            {"job_id": JOB_ID, "config": {}},  # no s3_input_key
            {"job_id": JOB_ID, "s3_input_key": INPUT_KEY},  # no config
        ],
    )
    def test_malformed_body_does_not_escape_the_handler(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        body: dict | str,
    ) -> None:
        # This used to raise out of _handle_message and surface as "Error polling SQS",
        # with no job_id in scope to attribute the failure to.
        worker._handle_message(_message(body=body))
        assert status_calls == []
        assert _fake_sqs(worker).deleted == []


class TestDeviceRouting:
    """Declining a message costs one of its SQS delivery attempts.

    SQS cannot show a message without delivering it, so a mismatched pool that defers a
    job has already spent budget the redrive policy reserves for poison messages.
    Unbounded bouncing therefore pushed healthy jobs into the DLQ still marked
    "processing" — the deferral path returns before the failure handler ever runs.
    """

    @staticmethod
    def _gpu_job(receive_count: int) -> dict:
        return _message(
            receive_count=receive_count,
            body={
                "job_id": JOB_ID,
                "s3_input_key": INPUT_KEY,
                "config": {"device_preference": "gpu"},
            },
        )

    @pytest.mark.parametrize("receive_count", [1, 2])
    def test_mismatch_defers_without_failing_the_job(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        receive_count: int,
    ) -> None:
        # A capable pool may still take it, so nothing is decided yet.
        monkeypatch.setattr(consumer_mod, "WORKER_DEVICE", "cpu")
        worker._handle_message(self._gpu_job(receive_count))

        assert status_calls == []
        assert _fake_sqs(worker).deleted == []
        assert _fake_sqs(worker).visibility_calls == [{"receipt": "rh-1", "timeout": 5}]

    def test_mismatch_on_final_delivery_fails_the_job_with_a_reason(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No pool matching the request took it, so this is a real failure. Deferring
        # again would send it to the DLQ still reading "processing".
        monkeypatch.setattr(consumer_mod, "WORKER_DEVICE", "cpu")
        worker._handle_message(self._gpu_job(3))

        assert len(status_calls) == 1
        assert status_calls[0]["status"] == "failed"
        assert status_calls[0]["job_id"] == JOB_ID
        assert "device_preference='gpu'" in status_calls[0]["error_message"]
        # No further deferral, and the message is kept for the DLQ.
        assert _fake_sqs(worker).visibility_calls == []
        assert _fake_sqs(worker).deleted == []

    def test_processing_is_not_attempted_on_a_mismatched_pool(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Failing loudly is the point; running a gpu job on a cpu pool anyway is not.
        monkeypatch.setattr(consumer_mod, "WORKER_DEVICE", "cpu")
        monkeypatch.setattr(
            consumer_mod, "process_job", _raise(AssertionError("must not run"))
        )
        worker._handle_message(self._gpu_job(3))
        assert status_calls[0]["status"] == "failed"

    @pytest.mark.parametrize(
        ("worker_device", "device_pref"),
        [
            ("auto", "gpu"),
            ("auto", "cpu"),
            ("auto", "auto"),
            ("cpu", "auto"),
            ("gpu", "auto"),
            ("cpu", "cpu"),
            ("gpu", "gpu"),
        ],
    )
    def test_accepted_combinations_process_normally(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
        worker_device: str,
        device_pref: str,
    ) -> None:
        # The shipped configuration is a single pool at "auto", so none of the deferral
        # logic runs for it at any receive count.
        monkeypatch.setattr(consumer_mod, "WORKER_DEVICE", worker_device)
        monkeypatch.setattr(consumer_mod, "process_job", lambda **kw: None)
        msg = _message(
            receive_count=3,
            body={
                "job_id": JOB_ID,
                "s3_input_key": INPUT_KEY,
                "config": {"device_preference": device_pref},
            },
        )
        worker._handle_message(msg)

        assert _fake_sqs(worker).deleted == ["rh-1"]
        assert _fake_sqs(worker).visibility_calls == []
        assert status_calls == []


class TestReceiveCountParsing:
    @pytest.mark.parametrize(
        "attributes",
        [None, {}, {"ApproximateReceiveCount": "not-a-number"}],
    )
    def test_missing_or_unparseable_count_defaults_to_first_delivery(
        self, worker: Worker, attributes: dict | None
    ) -> None:
        msg = _message()
        if attributes is None:
            del msg["Attributes"]
        else:
            msg["Attributes"] = attributes
        # Defaulting high would write off a job on its first delivery.
        assert Worker._receive_count(msg) == 1

    def test_count_is_read_from_the_attribute(self, worker: Worker) -> None:
        assert Worker._receive_count(_message(receive_count=4)) == 4


class TestErrorTruncation:
    def test_short_error_keeps_type_and_message(self) -> None:
        assert _truncate_error(ValueError("bad file")) == "ValueError: bad file"

    def test_empty_message_falls_back_to_the_type_name(self) -> None:
        assert _truncate_error(RuntimeError()) == "RuntimeError"

    def test_long_error_is_bounded(self) -> None:
        # An unbounded traceback in the record could fail the very write that reports
        # the failure, since the whole job record shares one 400 KB DynamoDB item.
        out = _truncate_error(ValueError("x" * 5000))
        assert len(out) == MAX_ERROR_MESSAGE_CHARS
        assert out.endswith("...")


class TestHttpFallbackEncoding:
    """The in-memory/local path reports status over HTTP with query parameters.

    The error message is exception text, so it always contains spaces and may contain
    & or =. Interpolating it into the query string raw produced an invalid URL and let
    a message inject extra parameters.
    """

    @staticmethod
    def _captured_url(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> str:
        import urllib.request

        from pointcloud_detect.worker import processor as processor_mod

        seen: list[str] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            seen.append(req.full_url)
            return FakeResponse()

        monkeypatch.delenv("JOB_STORE_BACKEND", raising=False)
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        processor_mod._update_status(JOB_ID, "failed", **kwargs)
        return seen[0]

    def test_spaces_are_encoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = self._captured_url(
            monkeypatch, error_message="ValueError: unsupported E57 variant"
        )
        assert " " not in url
        assert "status=failed" in url

    def test_ampersand_cannot_inject_a_parameter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = self._captured_url(
            monkeypatch, error_message="boom&status=complete&detection_count=999"
        )
        from urllib.parse import parse_qs, urlparse

        params = parse_qs(urlparse(url).query)
        # The injected keys must not appear as real parameters, and status must not
        # have been flipped to complete by the error text.
        assert params["status"] == ["failed"]
        assert "detection_count" not in params
        assert params["error_message"] == ["boom&status=complete&detection_count=999"]

    def test_round_trips_through_parse_qs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        msg = "OSError: [Errno 28] No space left on device: '/tmp/x y.las'"
        url = self._captured_url(monkeypatch, error_message=msg)
        from urllib.parse import parse_qs, urlparse

        assert parse_qs(urlparse(url).query)["error_message"] == [msg]


class TestVisibilityExtender:
    """A job outlasting the visibility timeout used to be processed twice.

    Visibility is 900s and a large scene takes hours, so the message became visible
    again mid-run: a second worker started the same job on a second GPU, and the
    redelivery spent a retry attempt on a job that was progressing fine. The extender
    existed for this but nothing ever called it.
    """

    def test_extends_repeatedly_while_the_block_runs(self) -> None:
        sqs = FakeSQS()
        with consumer_mod.VisibilityExtender(
            sqs, "q", "rh-1", JOB_ID, interval=0.01, timeout=900
        ) as ext:
            deadline = time.monotonic() + 2.0
            while ext.extensions < 3 and time.monotonic() < deadline:
                time.sleep(0.01)

        assert ext.extensions >= 3
        assert all(c["timeout"] == 900 for c in sqs.visibility_calls)
        assert all(c["receipt"] == "rh-1" for c in sqs.visibility_calls)

    def test_stops_when_the_block_exits(self) -> None:
        sqs = FakeSQS()
        with consumer_mod.VisibilityExtender(
            sqs, "q", "rh-1", JOB_ID, interval=0.01, timeout=900
        ) as ext:
            deadline = time.monotonic() + 2.0
            while ext.extensions < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

        after_exit = len(sqs.visibility_calls)
        time.sleep(0.1)  # several intervals' worth
        assert len(sqs.visibility_calls) == after_exit

    def test_no_extension_for_a_job_shorter_than_the_interval(self) -> None:
        # The common case. Visibility was just set by receive_message, so the first
        # renewal should wait out a full interval rather than firing immediately.
        sqs = FakeSQS()
        with consumer_mod.VisibilityExtender(
            sqs, "q", "rh-1", JOB_ID, interval=30, timeout=900
        ):
            pass
        assert sqs.visibility_calls == []

    def test_a_failed_extension_does_not_propagate(self) -> None:
        # Reached the 12h cap, or the message is already in the DLQ. Killing a job that
        # is running fine because its bookkeeping call failed would be worse.
        class Failing(FakeSQS):
            def change_message_visibility(
                self,
                QueueUrl: str,  # noqa: N803
                ReceiptHandle: str,  # noqa: N803
                VisibilityTimeout: int,  # noqa: N803
            ) -> None:
                raise RuntimeError("InvalidParameterValue: exceeds maximum")

        sqs = Failing()
        with consumer_mod.VisibilityExtender(
            sqs, "q", "rh-1", JOB_ID, interval=0.01, timeout=900
        ) as ext:
            time.sleep(0.1)
        assert ext.extensions == 0

    def test_stops_retrying_after_a_failure(self) -> None:
        calls: list[int] = []

        class FailingOnce(FakeSQS):
            def change_message_visibility(
                self,
                QueueUrl: str,  # noqa: N803
                ReceiptHandle: str,  # noqa: N803
                VisibilityTimeout: int,  # noqa: N803
            ) -> None:
                calls.append(1)
                raise RuntimeError("nope")

        sqs = FailingOnce()
        with consumer_mod.VisibilityExtender(
            sqs, "q", "rh-1", JOB_ID, interval=0.01, timeout=900
        ):
            time.sleep(0.15)  # many intervals
        # One attempt, then it gives up rather than spamming a call that cannot succeed.
        assert len(calls) == 1

    def test_handle_message_extends_during_processing(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(consumer_mod, "VISIBILITY_EXTENSION_INTERVAL", 0.01)

        def slow_job(**kwargs: Any) -> None:
            time.sleep(0.1)

        monkeypatch.setattr(consumer_mod, "process_job", slow_job)
        worker._handle_message(_message())

        sqs = _fake_sqs(worker)
        assert sqs.visibility_calls, "a long job must have its visibility renewed"
        assert all(c["timeout"] == 900 for c in sqs.visibility_calls)
        # Still deleted on success, and never marked failed.
        assert sqs.deleted == ["rh-1"]
        assert status_calls == []

    def test_extension_stops_before_the_message_is_deleted(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Renewing a receipt handle after its message is gone would log a spurious
        # warning on every successful long job.
        monkeypatch.setattr(consumer_mod, "VISIBILITY_EXTENSION_INTERVAL", 0.01)
        monkeypatch.setattr(consumer_mod, "process_job", lambda **kw: time.sleep(0.05))
        worker._handle_message(_message())

        sqs = _fake_sqs(worker)
        before = len(sqs.visibility_calls)
        time.sleep(0.1)
        assert len(sqs.visibility_calls) == before

    def test_a_failing_long_job_is_still_reported(
        self,
        worker: Worker,
        status_calls: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The extender wraps processing, so it must not swallow the exception.
        monkeypatch.setattr(consumer_mod, "VISIBILITY_EXTENSION_INTERVAL", 0.01)

        def slow_failure(**kwargs: Any) -> None:
            time.sleep(0.05)
            raise ValueError("corrupt tile")

        monkeypatch.setattr(consumer_mod, "process_job", slow_failure)
        worker._handle_message(_message(receive_count=3))

        assert len(status_calls) == 1
        assert status_calls[0]["status"] == "failed"
        assert status_calls[0]["error_message"] == "ValueError: corrupt tile"
