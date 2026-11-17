"""Tests for the API hardening in docs/security/THREAT_MODEL.md.

Each class names the threat or finding it guards: the body-size cap (T44), no-store
on API responses (M35), required bucket settings (no default bucket names), job deletion
(T60), UUID ids and bounded fields (M56, M43), marking a job failed when it cannot be
queued (T66), and the API docs being off by default (M58).
"""

import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pointcloud_detect.api.app import app
from pointcloud_detect.api.deps import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    REQUIRED_BUCKET_SETTINGS,
    Settings,
    get_settings,
)
from pointcloud_detect.api.hosts import HostAllowlistMiddleware
from pointcloud_detect.api.middleware import BodySizeLimitMiddleware, NoStoreMiddleware
from pointcloud_detect.api.models import (
    S3_MAX_MULTIPART_PARTS,
    JobConfigRequest,
    JobCreateRequest,
    JobResponse,
    JobStatus,
    UploadCompleteRequest,
    UploadInitiateRequest,
)
from pointcloud_detect.api.routes import annotations
from pointcloud_detect.api.routes import jobs as jobs_route
from pointcloud_detect.api.routes.annotations import MAX_ANNOTATIONS, AnnotationSet
from pointcloud_detect.api.store import job_store

BUCKETS = {
    "S3_INPUT_BUCKET": "test-input",
    "S3_OUTPUT_BUCKET": "test-output",
    "S3_TILES_BUCKET": "test-tiles",
    "S3_MODELS_BUCKET": "test-models",
}


@pytest.fixture
def buckets(monkeypatch: pytest.MonkeyPatch):
    """The required bucket settings, as the chart and docker-compose set them."""
    for env, name in BUCKETS.items():
        monkeypatch.setenv(env, name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_buckets(monkeypatch: pytest.MonkeyPatch):
    for env in BUCKETS:
        monkeypatch.delenv(env, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_store():
    job_store.clear()
    annotations._annotations.clear()
    yield
    job_store.clear()
    annotations._annotations.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# --- T44: request-body cap ---------------------------------------------------------


def _probe(limit: int, calls: list[str]) -> FastAPI:
    """A one-route app behind the cap, recording whether its handler ran."""
    probe = FastAPI()

    @probe.post("/api/echo")
    async def echo(request: Request):
        calls.append("handler")
        return {"bytes": len(await request.body())}

    @probe.post("/api/json")
    async def parsed(body: dict):
        calls.append("handler")
        return {"keys": len(body)}

    probe.add_middleware(BodySizeLimitMiddleware, max_body_bytes=limit)
    return probe


def _run_asgi(asgi, scope: dict, receive, send) -> None:
    """Drive one ASGI call to completion outside the test client."""
    import anyio

    async def main() -> None:
        await asgi(scope, receive, send)

    anyio.run(main)


class TestBodySizeLimit:
    """T44: bodies were parsed with no size limit anywhere."""

    def test_the_app_installs_the_cap_from_settings(self) -> None:
        (mw,) = [m for m in app.user_middleware if m.cls is BodySizeLimitMiddleware]
        assert mw.kwargs["max_body_bytes"] == Settings().max_request_body_bytes
        assert Settings().max_request_body_bytes == DEFAULT_MAX_REQUEST_BODY_BYTES
        assert DEFAULT_MAX_REQUEST_BODY_BYTES == 1024 * 1024

    def test_the_cap_runs_before_cors_and_the_routes(self) -> None:
        # user_middleware is outermost first.
        order: list[object] = [m.cls for m in app.user_middleware]
        assert order == [
            HostAllowlistMiddleware,
            NoStoreMiddleware,
            BodySizeLimitMiddleware,
            CORSMiddleware,
        ]

    def test_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "2048")
        assert Settings().max_request_body_bytes == 2048
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_non_positive_cap_rejected(self) -> None:
        with pytest.raises(ValueError):
            BodySizeLimitMiddleware(FastAPI(), max_body_bytes=0)

    def test_declared_oversize_is_refused_without_reading_the_body(self) -> None:
        """413 on Content-Length alone: the app never runs and receive is never
        called, so not one byte is read, let alone parsed."""
        calls: list[str] = []
        mw = BodySizeLimitMiddleware(_probe(100, calls), max_body_bytes=100)
        sent: list[dict] = []

        async def receive() -> dict:
            raise AssertionError("the body was read")

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/json",
            "raw_path": b"/api/json",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"101"),
            ],
        }
        _run_asgi(mw, scope, receive, send)

        assert sent[0]["status"] == 413
        assert calls == []

    def test_real_app_refuses_an_oversized_json_body(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pointcloud_detect.api.routes import training_sessions

        def must_not_run(**_kwargs):
            raise AssertionError("an oversized body reached the route")

        monkeypatch.setattr(training_sessions.session_store, "create", must_not_run)
        body = json.dumps({"name": "x" * DEFAULT_MAX_REQUEST_BODY_BYTES}).encode()
        r = client.post(
            "/api/training-sessions",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413
        assert "limit" in r.json()["detail"]

    @pytest.mark.parametrize("route", ["/api/echo", "/api/json"])
    def test_streamed_oversize_is_aborted(self, route: str) -> None:
        """No Content-Length (chunked): counted as it arrives, stopped past the cap.

        Driven over ASGI directly, because the test client drains the whole request
        body before the app runs and so cannot show where reading stopped.
        """
        calls: list[str] = []
        mw = BodySizeLimitMiddleware(_probe(1000, calls), max_body_bytes=1000)
        pulled: list[int] = []
        sent: list[dict] = []

        async def receive() -> dict:
            pulled.append(1)
            return {"type": "http.request", "body": b"x" * 64, "more_body": True}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": route,
            "raw_path": route.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        }
        _run_asgi(mw, scope, receive, send)

        assert sent[0]["status"] == 413
        # Stopped at the chunk that crossed 1,000 bytes (the 16th of 64 bytes); an
        # endless body is never read to its end.
        assert len(pulled) == 16
        # The raw-body handler is entered and refused mid-read. For the JSON route
        # FastAPI reads the body before calling the handler, so it never runs.
        assert calls == (["handler"] if route == "/api/echo" else [])

    def test_streamed_oversize_through_the_client(self) -> None:
        calls: list[str] = []

        def chunks():
            for _ in range(50):
                yield b"x" * 64

        r = TestClient(_probe(1000, calls)).post(
            "/api/json", content=chunks(), headers={"content-type": "application/json"}
        )
        assert r.status_code == 413
        assert calls == []

    def test_a_body_under_the_cap_passes(self) -> None:
        calls: list[str] = []
        r = TestClient(_probe(1000, calls)).post("/api/echo", content=b"x" * 1000)
        assert r.status_code == 200
        assert r.json() == {"bytes": 1000}

    def test_invalid_content_length_is_refused(self) -> None:
        mw = BodySizeLimitMiddleware(_probe(100, []), max_body_bytes=100)
        sent: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        for value in (b"abc", b"-1"):
            sent.clear()
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/api/echo",
                "raw_path": b"/api/echo",
                "query_string": b"",
                "headers": [(b"content-length", value)],
            }
            _run_asgi(mw, scope, receive, send)
            assert sent[0]["status"] == 400


class TestLargestLegitimatePayloads:
    """The cap is sized from the largest bodies the product legitimately sends."""

    @staticmethod
    def _size(model) -> int:
        return len(model.model_dump_json().encode())

    def test_a_complete_upload_at_s3s_part_ceiling_fits(self) -> None:
        # S3 upload ids run to a few hundred characters; ETags are quoted MD5s.
        req = UploadCompleteRequest(
            upload_id="u" * 512,
            s3_key="uploads/" + str(uuid.uuid4()) + "/" + "s" * 200 + ".laz",
            parts=[
                {"part_number": n, "etag": '"' + "f" * 32 + '"'}
                for n in range(1, S3_MAX_MULTIPART_PARTS + 1)
            ],
        )
        size = self._size(req)
        assert size < DEFAULT_MAX_REQUEST_BODY_BYTES, size

    def test_a_full_annotation_set_fits(self) -> None:
        box = {
            # The longest float reprs JSON produces, and the longest class name.
            "center_xyz": [-1.2345678901234567e-05] * 3,
            "size_wlh": [-1.2345678901234567e-05] * 3,
            "heading": -1.2345678901234567e-05,
            "class_name": "c" * 64,
        }
        full = AnnotationSet(
            scene_id="s" * 128,
            point_cloud_path="p" * 1024,
            num_annotations=MAX_ANNOTATIONS,
            annotations=[box] * MAX_ANNOTATIONS,  # pyright: ignore[reportArgumentType]
        )
        size = self._size(full)
        assert size < DEFAULT_MAX_REQUEST_BODY_BYTES, size

    def test_one_box_past_the_ceiling_is_refused(self) -> None:
        box = {"center_xyz": [0, 0, 0], "size_wlh": [1, 1, 1], "heading": 0.0}
        with pytest.raises(ValidationError):
            AnnotationSet(
                annotations=[{**box, "class_name": "c"}] * (MAX_ANNOTATIONS + 1)  # pyright: ignore[reportArgumentType]
            )

    def test_more_parts_than_s3_accepts_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            UploadCompleteRequest(
                upload_id="u",
                s3_key="uploads/a/b.las",
                parts=[{"part_number": 1, "etag": "e"}] * (S3_MAX_MULTIPART_PARTS + 1),
            )


# --- M35: Cache-Control ------------------------------------------------------------


class TestNoStore:
    """M35 (part): /api responses must not be cached by the browser or a proxy."""

    def test_api_responses_are_no_store(self, client: TestClient) -> None:
        assert client.get("/api/models").headers["cache-control"] == "no-store"

    def test_errors_too(self, client: TestClient) -> None:
        r = client.get(f"/api/jobs/{uuid.uuid4()}")
        assert r.status_code == 404
        assert r.headers["cache-control"] == "no-store"

    def test_the_body_caps_413_too(self, client: TestClient) -> None:
        r = client.post(
            "/api/training-sessions",
            content=b"x" * (DEFAULT_MAX_REQUEST_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413
        assert r.headers["cache-control"] == "no-store"

    def test_health_is_outside_api(self, client: TestClient) -> None:
        assert "cache-control" not in client.get("/health").headers

    def _probe(self) -> TestClient:
        probe = FastAPI()
        probe.add_api_route(
            "/api/own",
            lambda: JSONResponse({}, headers={"Cache-Control": "max-age=60"}),
        )
        probe.add_api_route("/apiary", lambda: {})
        probe.add_middleware(NoStoreMiddleware)
        return TestClient(probe)

    def test_a_route_keeps_its_own_header(self) -> None:
        assert self._probe().get("/api/own").headers["cache-control"] == "max-age=60"

    def test_only_the_api_prefix(self) -> None:
        assert "cache-control" not in self._probe().get("/apiary").headers


# --- Required bucket settings: no default bucket names ------------------------------


class TestRequiredBuckets:
    """Default bucket names guessed at someone else's buckets; now they are required."""

    def test_no_default_names(self, no_buckets) -> None:
        s = Settings()
        for field in REQUIRED_BUCKET_SETTINGS:
            assert getattr(s, field) == ""
            assert Settings.model_fields[field].default == ""

    def test_all_missing_are_named(self, no_buckets) -> None:
        with pytest.raises(RuntimeError) as exc:
            Settings().require_buckets()
        for env in BUCKETS:
            assert env in str(exc.value)

    def test_one_missing_is_named(self, buckets, monkeypatch) -> None:
        monkeypatch.setenv("S3_TILES_BUCKET", "  ")
        assert Settings().missing_bucket_settings() == ["S3_TILES_BUCKET"]

    def test_set_passes(self, buckets) -> None:
        Settings().require_buckets()

    def test_the_api_fails_at_startup_without_them(self, no_buckets) -> None:
        with pytest.raises(RuntimeError, match="S3_INPUT_BUCKET"):
            with TestClient(app):
                pass

    def test_the_api_starts_with_them(self, buckets) -> None:
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200


# --- T60: DELETE /api/jobs/{id} ----------------------------------------------------


def _client_error(code: str = "AccessDenied") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "DeleteObject")


class FakeS3:
    """The four calls the delete path makes, over an in-memory set of keys."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, set[str]] = defaultdict(set)
        self.page_size = page_size
        self.refuse: set[str] = set()  # keys DeleteObjects reports as errors
        self.raise_on_delete_object = False
        self.batch_sizes: list[int] = []

    def put(self, bucket: str, *keys: str) -> None:
        self.objects[bucket].update(keys)

    def delete_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        if self.raise_on_delete_object:
            raise _client_error()
        self.objects[Bucket].discard(Key)
        return {}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:  # noqa: N803
        self.batch_sizes.append(len(Delete["Objects"]))
        assert len(Delete["Objects"]) <= 1000, "S3 accepts at most 1,000 keys"
        errors = []
        for obj in Delete["Objects"]:
            if obj["Key"] in self.refuse:
                errors.append({"Key": obj["Key"], "Code": "AccessDenied"})
            else:
                self.objects[Bucket].discard(obj["Key"])
        return {"Errors": errors} if errors else {}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        keys = sorted(k for k in self.objects[Bucket] if k.startswith(Prefix))
        for i in range(0, len(keys), self.page_size):
            yield {"Contents": [{"Key": k} for k in keys[i : i + self.page_size]]}
        if not keys:
            yield {}


@pytest.fixture
def s3(buckets, monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(jobs_route, "get_s3_client", lambda: fake)
    return fake


def _job(
    status: JobStatus = JobStatus.complete, input_key: str | None = None
) -> JobResponse:
    job_id = str(uuid.uuid4())
    job = JobResponse(
        job_id=job_id,
        filename="scene.las",
        s3_input_key=input_key or f"uploads/{uuid.uuid4()}/scene.las",
        config=JobConfigRequest(),
        status=status,
        created_at=datetime.now(UTC),
    )
    job_store.create(job)
    return job


def _stored(s3: FakeS3, job: JobResponse, tiles: int = 3) -> None:
    s3.put("test-input", job.s3_input_key)
    s3.put(
        "test-output",
        f"results/{job.job_id}/detections.json",
        f"results/{job.job_id}/segmentation.json",
    )
    s3.put(
        "test-tiles",
        f"tiles/{job.job_id}/index.json",
        *(f"tiles/{job.job_id}/detail_{i:04d}.bin" for i in range(tiles)),
    )


class TestDeleteJob:
    """T60: there was no way to delete a job's scan, results or record."""

    def test_deletes_input_results_tiles_annotations_and_record(
        self, client: TestClient, s3: FakeS3
    ) -> None:
        job = _job()
        other = _job()
        _stored(s3, job)
        _stored(s3, other)
        annotations._annotations[job.job_id] = {"annotations": []}

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 200, r.text
        assert r.json() == {
            "status": "deleted",
            "job_id": job.job_id,
            "deleted_objects": 1 + 2 + 4,
        }
        assert job_store.get(job.job_id) is None
        assert job.job_id not in annotations._annotations
        remaining = [k for keys in s3.objects.values() for k in keys]
        assert not [k for k in remaining if job.job_id in k]
        assert job.s3_input_key not in s3.objects["test-input"]
        # Another job's data is untouched.
        assert other.s3_input_key in s3.objects["test-input"]
        assert len([k for k in remaining if other.job_id in k]) == 6
        assert job_store.get(other.job_id) is not None

    def test_many_tiles_are_deleted_in_batches_of_at_most_1000(
        self, client: TestClient, s3: FakeS3
    ) -> None:
        job = _job()
        _stored(s3, job, tiles=2500)

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 200
        assert r.json()["deleted_objects"] == 1 + 2 + 2501
        assert max(s3.batch_sizes) <= 1000
        assert not s3.objects["test-tiles"]

    def test_failed_jobs_are_deletable(self, client: TestClient, s3: FakeS3) -> None:
        job = _job(JobStatus.failed)
        assert client.delete(f"/api/jobs/{job.job_id}").status_code == 200

    @pytest.mark.parametrize("status", [JobStatus.queued, JobStatus.processing])
    def test_in_flight_jobs_are_refused(
        self, client: TestClient, s3: FakeS3, status: JobStatus
    ) -> None:
        job = _job(status)
        _stored(s3, job)

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 409
        assert job_store.get(job.job_id) is not None
        assert job.s3_input_key in s3.objects["test-input"]
        assert len(s3.objects["test-tiles"]) == 4

    def test_refused_while_a_retry_of_the_same_input_runs(
        self, client: TestClient, s3: FakeS3
    ) -> None:
        job = _job(JobStatus.failed)
        _job(JobStatus.queued, input_key=job.s3_input_key)  # the dashboard's Retry
        _stored(s3, job)

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 409
        assert job.s3_input_key in s3.objects["test-input"]

    def test_a_shared_input_is_kept_until_the_last_job_goes(
        self, client: TestClient, s3: FakeS3
    ) -> None:
        job = _job(JobStatus.failed)
        retry = _job(JobStatus.complete, input_key=job.s3_input_key)
        _stored(s3, job)

        assert client.delete(f"/api/jobs/{job.job_id}").status_code == 200
        assert job.s3_input_key in s3.objects["test-input"]

        assert client.delete(f"/api/jobs/{retry.job_id}").status_code == 200
        assert job.s3_input_key not in s3.objects["test-input"]

    def test_a_retry_created_during_the_delete_keeps_its_input(
        self, client: TestClient, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Retry lands after the in-flight check has passed but before the input
        # is deleted: the delete must see it and keep the upload.
        job = _job(JobStatus.failed)
        _stored(s3, job)
        check = jobs_route.validate_input_key

        def retry_arrives(key: str) -> str:
            _job(JobStatus.queued, input_key=job.s3_input_key)
            return check(key)

        monkeypatch.setattr(jobs_route, "validate_input_key", retry_arrives)

        assert client.delete(f"/api/jobs/{job.job_id}").status_code == 200
        assert job.s3_input_key in s3.objects["test-input"]
        assert job_store.get(job.job_id) is None

    def test_unknown_is_404(self, client: TestClient, s3: FakeS3) -> None:
        assert client.delete(f"/api/jobs/{uuid.uuid4()}").status_code == 404

    @pytest.mark.parametrize("job_id", ["not-a-uuid", "%2E%2E", "x" * 36])
    def test_non_uuid_is_422(self, client: TestClient, s3: FakeS3, job_id) -> None:
        assert client.delete(f"/api/jobs/{job_id}").status_code == 422

    def test_second_delete_is_404(self, client: TestClient, s3: FakeS3) -> None:
        job = _job()
        assert client.delete(f"/api/jobs/{job.job_id}").status_code == 200
        assert client.delete(f"/api/jobs/{job.job_id}").status_code == 404

    def test_a_per_key_failure_keeps_the_record_for_a_retry(
        self, client: TestClient, s3: FakeS3
    ) -> None:
        job = _job()
        _stored(s3, job)
        s3.refuse.add(f"tiles/{job.job_id}/index.json")

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 502
        assert job_store.get(job.job_id) is not None
        assert "AccessDenied" not in r.text  # no internal detail to the client

        s3.refuse.clear()
        assert client.delete(f"/api/jobs/{job.job_id}").status_code == 200
        assert job_store.get(job.job_id) is None
        assert not s3.objects["test-tiles"]

    def test_an_s3_error_keeps_the_record(self, client: TestClient, s3: FakeS3) -> None:
        job = _job()
        s3.raise_on_delete_object = True

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 502
        assert job_store.get(job.job_id) is not None

    def test_an_invalid_stored_key_deletes_nothing_on_its_say_so(
        self, client: TestClient, s3: FakeS3
    ) -> None:
        # A record written before keys were validated could name any object.
        job = _job(input_key="results/someone-else/detections.json")
        s3.put("test-input", "results/someone-else/detections.json")

        r = client.delete(f"/api/jobs/{job.job_id}")

        assert r.status_code == 200
        assert "results/someone-else/detections.json" in s3.objects["test-input"]
        assert job_store.get(job.job_id) is None

    def test_prefixes_end_at_the_id(self, client: TestClient, s3: FakeS3) -> None:
        job = _job()
        # A key that shares the id as a string prefix but is not under its folder.
        s3.put("test-tiles", f"tiles/{job.job_id}-copy/index.json")

        client.delete(f"/api/jobs/{job.job_id}")

        assert f"tiles/{job.job_id}-copy/index.json" in s3.objects["test-tiles"]


# --- M56: ids are UUIDs; free text is bounded --------------------------------------


class TestIdValidation:
    """M56: path parameters are UUIDs (and tile ids plain names) on the server."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/jobs/not-a-uuid",
            "/api/jobs/%2E%2E",
            "/api/results/not-a-uuid/detections",
            "/api/results/%2E%2E/tiles/index",
            "/api/annotations/not-a-uuid",
            "/api/training/not-a-uuid",
            "/api/training-sessions/not-a-uuid",
            "/api/labels?job_id=not-a-uuid",
        ],
    )
    def test_a_non_uuid_id_is_refused(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 422

    @pytest.mark.parametrize("tile_id", ["%2E%2E", "a.b", "..%5Cx", "x" * 65])
    def test_an_unusual_tile_id_is_refused(
        self, client: TestClient, tile_id: str
    ) -> None:
        r = client.get(f"/api/results/{uuid.uuid4()}/tiles/{tile_id}")
        assert r.status_code == 422

    def test_ids_are_matched_in_canonical_form(self, client: TestClient) -> None:
        job = _job()
        r = client.get(f"/api/jobs/{job.job_id.upper()}")
        assert r.status_code == 200
        assert r.json()["job_id"] == job.job_id

    def test_annotations_use_the_canonical_id(self, client: TestClient) -> None:
        job_id = str(uuid.uuid4())
        body = {"annotations": []}
        assert (
            client.post(f"/api/annotations/{job_id.upper()}", json=body).status_code
            == 201
        )
        assert client.get(f"/api/annotations/{job_id}").status_code == 200

    def test_session_job_ids_are_uuids(self, client: TestClient) -> None:
        sid = client.post("/api/training-sessions", json={"name": "s"}).json()[
            "session_id"
        ]
        bad = client.post(f"/api/training-sessions/{sid}/jobs", json={"job_id": "x"})
        assert bad.status_code == 422
        job_id = str(uuid.uuid4())
        ok = client.post(
            f"/api/training-sessions/{sid}/jobs", json={"job_id": job_id.upper()}
        )
        assert ok.status_code == 200
        assert client.get(f"/api/training-sessions/{sid}").json()["job_ids"] == [job_id]


class TestFieldBounds:
    """M56, M43: free text and job configuration are bounded on the server."""

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "n" * 129},
            {"name": "n", "classes": ["c" * 65]},
            {"name": "n", "classes": [f"c{i}" for i in range(101)]},
        ],
    )
    def test_session_text(self, client: TestClient, body: dict) -> None:
        assert client.post("/api/training-sessions", json=body).status_code == 422

    def test_annotation_class_names(self, client: TestClient) -> None:
        box = {
            "center_xyz": [0, 0, 0],
            "size_wlh": [1, 1, 1],
            "heading": 0,
            "class_name": "c" * 65,
        }
        r = client.post(f"/api/annotations/{uuid.uuid4()}", json={"annotations": [box]})
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "config",
        [
            {"downsample_voxel_m": 0},
            {"downsample_voxel_m": -0.02},
            {"downsample_voxel_m": 11},
            {"max_points": 0},
            {"max_points": 2_000_000_000},
        ],
    )
    def test_job_config(self, config: dict) -> None:
        with pytest.raises(ValidationError):
            JobCreateRequest(s3_key="uploads/a/b.las", filename="b.las", config=config)  # pyright: ignore[reportArgumentType]

    def test_filenames(self) -> None:
        with pytest.raises(ValidationError):
            UploadInitiateRequest(filename="f" * 252 + ".las", file_size_bytes=1)
        with pytest.raises(ValidationError):
            JobCreateRequest(s3_key="uploads/a/b.las", filename="f" * 256)

    def test_upload_size_quota(self, client: TestClient) -> None:
        r = client.post(
            "/api/upload/initiate",
            json={
                "filename": "big.las",
                "file_size_bytes": Settings().max_upload_bytes + 1,
            },
        )
        assert r.status_code == 413
        assert "MAX_UPLOAD_BYTES" in r.json()["detail"]


# --- T66: a job that cannot be queued is marked failed -----------------------------


class TestQueueFailure:
    """T66: a failed SendMessage left the job in "queued" with nothing to run it."""

    def test_marked_failed_and_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class BrokenSqs:
            def send_message(self, **_kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "no"}}, "SendMessage"
                )

        monkeypatch.setattr(jobs_route, "get_sqs_client", lambda: BrokenSqs())
        # A default detection job runs pretrained weights, which are opt-in.
        monkeypatch.setenv("ALLOW_NONCOMMERCIAL_WEIGHTS", "1")
        r = client.post(
            "/api/jobs", json={"s3_key": "uploads/a/b.las", "filename": "b.las"}
        )
        assert r.status_code == 503
        (job,) = job_store.list()
        assert job.status == JobStatus.failed
        assert job.error_message


# --- M58: API docs off by default --------------------------------------------------


class TestApiDocsOff:
    """M58: /docs, /redoc and /openapi.json described every route to any caller."""

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_not_served(self, client: TestClient, path: str) -> None:
        assert client.get(path).status_code == 404

    def test_off_unless_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_DOCS_ENABLED", raising=False)
        assert Settings().api_docs_enabled is False
        monkeypatch.setenv("API_DOCS_ENABLED", "true")
        assert Settings().api_docs_enabled is True
