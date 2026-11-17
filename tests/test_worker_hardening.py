"""Tests for the worker's message checks (T19), log format (T59) and API URLs."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from typing import Any

import pytest

from pointcloud_detect.worker import consumer as consumer_mod
from pointcloud_detect.worker import processor as processor_mod
from pointcloud_detect.worker.json_logging import JsonFormatter

JOB_ID = "9d0c4f4e-1b2a-4c3d-8e5f-6a7b8c9d0e1f"
INPUT_KEY = f"uploads/{JOB_ID}/scene.las"


# ---------------------------------------------------------------------------
# T59: one JSON object per record, whatever the message holds
# ---------------------------------------------------------------------------


def _format(message: str, *args: Any, exc_info: Any = None) -> str:
    record = logging.LogRecord(
        "pointcloud_detect.worker.consumer",
        logging.INFO,
        __file__,
        1,
        message,
        args,
        exc_info,
    )
    return JsonFormatter().format(record)


class TestJsonLogging:
    @pytest.mark.parametrize(
        "key",
        [
            'uploads/x/a","level":"CRITICAL","message":"forged.las',
            'uploads/x/a.las\n{"level": "ERROR", "message": "forged"}',
            "uploads/x/a.las\r  \x00\x1b[31m",
            'uploads/x/\\"a.las',
            "uploads/x/a\u2028b\u2029c.las",
        ],
    )
    def test_quotes_and_newlines_stay_inside_one_record(self, key: str) -> None:
        line = _format("Processing job %s: %s", JOB_ID, key)
        # One physical line, one object, and the key exactly as it was.
        assert "\n" not in line and "\r" not in line
        # ensure_ascii escapes the Unicode line separators too.
        assert "\u2028" not in line and "\u2029" not in line
        entry = json.loads(line)
        assert entry["message"] == f"Processing job {JOB_ID}: {key}"
        assert entry["level"] == "INFO"
        assert set(entry) == {"time", "level", "module", "message"}

    def test_tracebacks_are_one_escaped_field(self) -> None:
        try:
            raise ValueError('bad "value"\nsecond line')
        except ValueError:
            import sys

            line = _format("Job failed", exc_info=sys.exc_info())
        assert "\n" not in line
        entry = json.loads(line)
        assert "Traceback" in entry["exc_info"]
        assert 'bad "value"\nsecond line' in entry["exc_info"]

    def test_configure_uses_the_json_formatter(self) -> None:
        from pointcloud_detect.worker.json_logging import configure_json_logging

        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            root.handlers = []
            configure_json_logging()
            assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            handler.setFormatter(JsonFormatter())
            logging.getLogger("t59").addHandler(handler)
            logging.getLogger("t59").warning('a "b"\nc')
            assert json.loads(stream.getvalue())["message"] == 'a "b"\nc'
        finally:
            root.handlers = saved


# ---------------------------------------------------------------------------
# T19: a message must name an active job and match its record
# ---------------------------------------------------------------------------


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    store: dict[str, dict[str, Any]] = {
        JOB_ID: {"job_id": JOB_ID, "s3_input_key": INPUT_KEY, "status": "queued"}
    }
    monkeypatch.setattr(consumer_mod, "fetch_job_record", store.get)
    monkeypatch.setattr(processor_mod, "fetch_job_record", store.get)
    return store


class TestMessageProblem:
    def test_matching_message_is_accepted(self, records: dict) -> None:
        assert consumer_mod._message_problem(JOB_ID, INPUT_KEY) is None

    @pytest.mark.parametrize("status", ["queued", "processing"])
    def test_active_statuses(self, records: dict, status: str) -> None:
        records[JOB_ID]["status"] = status
        assert consumer_mod._message_problem(JOB_ID, INPUT_KEY) is None

    @pytest.mark.parametrize(
        "job_id",
        [
            "job-1",
            "../" + JOB_ID,
            JOB_ID.upper(),  # a UUID, but not the canonical text the API writes
            "{" + JOB_ID + "}",
            JOB_ID.replace("-", ""),
            "",
        ],
    )
    def test_non_uuid_ids_are_rejected(self, records: dict, job_id: str) -> None:
        assert (
            consumer_mod._message_problem(job_id, INPUT_KEY) == "job_id is not a UUID"
        )

    def test_non_string_fields_are_rejected(self, records: dict) -> None:
        assert consumer_mod._message_problem(123, INPUT_KEY) is not None
        assert consumer_mod._message_problem(JOB_ID, ["k"]) is not None

    def test_unknown_job_is_rejected(self, records: dict) -> None:
        other = "11111111-2222-4333-8444-555555555555"
        assert "no job record" in (
            consumer_mod._message_problem(other, INPUT_KEY) or ""
        )

    def test_different_input_key_is_rejected(self, records: dict) -> None:
        problem = consumer_mod._message_problem(JOB_ID, f"uploads/{JOB_ID}/other.las")
        assert problem == "s3_input_key does not match the job record"

    @pytest.mark.parametrize("status", ["complete", "failed", None, "weird"])
    def test_finished_job_is_rejected(self, records: dict, status: Any) -> None:
        records[JOB_ID]["status"] = status
        assert "not queued or processing" in (
            consumer_mod._message_problem(JOB_ID, INPUT_KEY) or ""
        )


class FakeSQS:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> None:  # noqa: N803
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, **kwargs: Any) -> None:
        pass


def _worker() -> consumer_mod.Worker:
    w = consumer_mod.Worker.__new__(consumer_mod.Worker)
    w._running = True
    w._sqs = FakeSQS()  # pyright: ignore[reportAttributeAccessIssue]
    w._s3 = object()  # pyright: ignore[reportAttributeAccessIssue]
    return w


def _message(job_id: str, key: str, receive_count: int = 3) -> dict:
    return {
        "ReceiptHandle": "rh",
        "Body": json.dumps(
            {"job_id": job_id, "s3_input_key": key, "config": {"job_type": "x"}}
        ),
        "Attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


class TestConsumerRejects:
    @pytest.mark.parametrize(
        ("job_id", "key", "status"),
        [
            ("job-1", INPUT_KEY, "queued"),
            (JOB_ID, f"uploads/{JOB_ID}/other.las", "queued"),
            (JOB_ID, INPUT_KEY, "complete"),
            ("11111111-2222-4333-8444-555555555555", INPUT_KEY, "queued"),
        ],
    )
    def test_rejected_message_touches_nothing(
        self,
        records: dict,
        monkeypatch: pytest.MonkeyPatch,
        job_id: str,
        key: str,
        status: str,
    ) -> None:
        # Even on the final delivery: the job it names may be someone else's, so
        # it is neither processed nor marked failed, and it is left for the DLQ.
        records[JOB_ID]["status"] = status
        calls: list[Any] = []
        monkeypatch.setattr(consumer_mod, "process_job", lambda **kw: calls.append(kw))
        monkeypatch.setattr(
            consumer_mod, "_update_status", lambda *a, **kw: calls.append(a)
        )
        worker = _worker()
        worker._handle_message(_message(job_id, key))
        assert calls == []
        assert worker._sqs.deleted == []  # pyright: ignore[reportAttributeAccessIssue]

    def test_lookup_failure_leaves_the_message_for_redelivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(job_id: str) -> None:
            raise OSError("store unavailable")

        calls: list[Any] = []
        monkeypatch.setattr(consumer_mod, "fetch_job_record", boom)
        monkeypatch.setattr(consumer_mod, "process_job", lambda **kw: calls.append(kw))
        monkeypatch.setattr(
            consumer_mod, "_update_status", lambda *a, **kw: calls.append(a)
        )
        worker = _worker()
        worker._handle_message(_message(JOB_ID, INPUT_KEY))
        assert calls == []
        assert worker._sqs.deleted == []  # pyright: ignore[reportAttributeAccessIssue]


class TestResultWritesAreGuarded:
    def test_assert_job_active_raises_for_a_finished_job(self, records: dict) -> None:
        processor_mod._assert_job_active(JOB_ID)
        records[JOB_ID]["status"] = "complete"
        with pytest.raises(processor_mod.JobNoLongerActiveError):
            processor_mod._assert_job_active(JOB_ID)
        del records[JOB_ID]
        with pytest.raises(processor_mod.JobNoLongerActiveError):
            processor_mod._assert_job_active(JOB_ID)

    def test_job_finished_during_inference_is_not_overwritten(
        self, records: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from pointcloud_detect.models import PipelineResult

        statuses: list[str] = []
        uploads: list[str] = []
        monkeypatch.delenv("PCD_PARSER_MODE", raising=False)
        monkeypatch.setattr(processor_mod, "download_from_s3", lambda *a, **k: None)
        monkeypatch.setattr(
            processor_mod,
            "upload_json_to_s3",
            lambda client, bucket, key, data: uploads.append(key),
        )
        monkeypatch.setattr(
            processor_mod,
            "_update_status",
            lambda job_id, status, **kw: statuses.append(status),
        )

        def pipeline_that_sees_the_job_finish(path: Any, config: Any) -> Any:
            # Another worker, or an operator, completes the job meanwhile.
            records[JOB_ID]["status"] = "complete"
            return PipelineResult(source_file=str(path), detections=[])

        import pointcloud_detect.pipeline as pipeline_mod

        monkeypatch.setattr(
            pipeline_mod, "run_pipeline", pipeline_that_sees_the_job_finish
        )
        with pytest.raises(processor_mod.JobNoLongerActiveError):
            processor_mod.process_job(JOB_ID, INPUT_KEY, {}, object())
        assert uploads == []
        assert statuses == ["processing"]

    def test_bad_input_key_is_refused_before_the_job_is_marked(
        self, records: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        statuses: list[str] = []
        monkeypatch.setattr(
            processor_mod,
            "_update_status",
            lambda job_id, status, **kw: statuses.append(status),
        )
        with pytest.raises(ValueError):
            processor_mod.process_job(JOB_ID, "uploads/../etc/passwd.las", {}, object())
        assert statuses == []


# ---------------------------------------------------------------------------
# The API fallback URL: http(s) only, job id encoded as one path segment
# ---------------------------------------------------------------------------


class TestApiJobUrl:
    def test_job_id_is_one_encoded_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(processor_mod, "API_BASE_URL", "http://api")
        url = processor_mod._api_job_url("a/../b?x=1#f", "/status?status=failed")
        assert url == "http://api/api/jobs/a%2F..%2Fb%3Fx%3D1%23f/status?status=failed"

    @pytest.mark.parametrize("base", ["file:///etc", "ftp://h", "gopher://h", "h"])
    def test_only_http_schemes(
        self, monkeypatch: pytest.MonkeyPatch, base: str
    ) -> None:
        monkeypatch.setattr(processor_mod, "API_BASE_URL", base)
        with pytest.raises(ValueError):
            processor_mod._api_job_url(JOB_ID)

    def test_fetch_job_record_uses_the_encoded_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        class Resp(io.BytesIO):
            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *a: object) -> None:
                pass

        def fake_urlopen(req: urllib.request.Request, timeout: float) -> Resp:
            seen.append(req.full_url)
            return Resp(json.dumps({"status": "queued"}).encode())

        monkeypatch.setenv("JOB_STORE_BACKEND", "memory")
        monkeypatch.setattr(processor_mod, "API_BASE_URL", "http://api")
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert processor_mod.fetch_job_record("x/y") == {"status": "queued"}
        assert seen == ["http://api/api/jobs/x%2Fy"]

    def test_missing_job_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def not_found(req: urllib.request.Request, timeout: float) -> None:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)  # type: ignore[arg-type]

        monkeypatch.setenv("JOB_STORE_BACKEND", "memory")
        monkeypatch.setattr(processor_mod, "API_BASE_URL", "http://api")
        monkeypatch.setattr(urllib.request, "urlopen", not_found)
        assert processor_mod.fetch_job_record(JOB_ID) is None
