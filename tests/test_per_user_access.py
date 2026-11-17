"""Two users, per-user mode: each reaches only their own objects (threat model T31).

Alice and Bob sign in as the ALB would sign them (tests/_identity.py). For every
route, Bob gets for Alice's object exactly what he gets for one that does not
exist, and nothing of Alice's changes.
"""

import io
import uuid

import pytest
from fastapi.testclient import TestClient

from pointcloud_detect.api.app import CLIENT_HEADER, app
from pointcloud_detect.api.deps import get_settings
from pointcloud_detect.api.models import JobStatus
from pointcloud_detect.api.routes import annotations
from pointcloud_detect.api.routes import jobs as jobs_route
from pointcloud_detect.api.routes import results as results_route
from pointcloud_detect.api.routes import upload as upload_route
from pointcloud_detect.api.store import job_store
from tests._identity import ALICE, BOB, per_user, signed_in

BUCKETS = {
    "S3_INPUT_BUCKET": "test-input",
    "S3_OUTPUT_BUCKET": "test-output",
    "S3_TILES_BUCKET": "test-tiles",
    "S3_MODELS_BUCKET": "test-models",
}


class NoSuchKeyError(Exception):
    pass


class FakeS3:
    """The S3 calls the upload, job and results routes make, in memory."""

    class exceptions:  # noqa: N801 - boto3's attribute name
        NoSuchKey = NoSuchKeyError

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.completed: list[str] = []
        self.aborted: list[str] = []

    def put(self, bucket: str, key: str, body: bytes = b"data") -> None:
        self.objects[(bucket, key)] = body

    def create_multipart_upload(self, Bucket: str, Key: str, **_) -> dict:  # noqa: N803
        return {"UploadId": f"upload-{uuid.uuid4()}"}

    def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:  # noqa: N803
        return f"https://{Params['Bucket']}.s3.example/{Params['Key']}"

    def complete_multipart_upload(self, Bucket: str, Key: str, **_) -> dict:  # noqa: N803
        self.completed.append(Key)
        self.put(Bucket, Key, b"scan")
        return {}

    def abort_multipart_upload(self, Bucket: str, Key: str, **_) -> dict:  # noqa: N803
        self.aborted.append(Key)
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise NoSuchKeyError(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        self.objects.pop((Bucket, Key), None)
        return {}

    def delete_objects(self, Bucket: str, Delete: dict) -> dict:  # noqa: N803
        for obj in Delete["Objects"]:
            self.objects.pop((Bucket, obj["Key"]), None)
        return {}

    def get_paginator(self, name: str) -> "FakeS3":
        return self

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803
        keys = [k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix)]
        yield {"Contents": [{"Key": k} for k in keys]} if keys else {}


class FakeSqs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, QueueUrl: str, MessageBody: str) -> dict:  # noqa: N803
        self.sent.append(MessageBody)
        return {}


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    for env, name in BUCKETS.items():
        monkeypatch.setenv(env, name)
    monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.example/queue")
    per_user(monkeypatch)
    s3, sqs = FakeS3(), FakeSqs()
    for module in (jobs_route, upload_route, results_route):
        monkeypatch.setattr(module, "get_s3_client", lambda: s3)
    monkeypatch.setattr(jobs_route, "get_sqs_client", lambda: sqs)
    job_store.clear()
    annotations._annotations.clear()
    yield s3, sqs
    job_store.clear()
    annotations._annotations.clear()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def as_user(sub: str) -> dict[str, str]:
    return {CLIENT_HEADER: "spa", **signed_in(sub)}


def upload(client: TestClient, sub: str, name: str = "scan.las") -> str:
    started = client.post(
        "/api/upload/initiate",
        json={"filename": name, "file_size_bytes": 1000},
        headers=as_user(sub),
    )
    assert started.status_code == 200, started.text
    key = started.json()["s3_key"]
    done = client.post(
        "/api/upload/complete",
        json={"upload_id": started.json()["upload_id"], "s3_key": key, "parts": []},
        headers=as_user(sub),
    )
    assert done.status_code == 200, done.text
    return key


def create_job(client: TestClient, sub: str, key: str) -> str:
    resp = client.post(
        "/api/jobs",
        json={"s3_key": key, "filename": key.rsplit("/", 1)[-1]},
        headers=as_user(sub),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["job_id"]


def alices_job(client: TestClient, status: JobStatus = JobStatus.complete) -> str:
    job_id = create_job(client, ALICE, upload(client, ALICE))
    job_store.update_status(job_id, status=status)
    return job_id


class TestUploads:
    def test_a_key_is_under_its_uploaders_id(self, aws, client: TestClient) -> None:
        key = upload(client, ALICE)
        assert key.startswith(f"uploads/{ALICE}/")
        assert key.endswith("/scan.las")

    def test_only_the_uploader_completes_it(self, aws, client: TestClient) -> None:
        s3, _ = aws
        started = client.post(
            "/api/upload/initiate",
            json={"filename": "scan.las", "file_size_bytes": 1000},
            headers=as_user(ALICE),
        ).json()
        body = {"upload_id": started["upload_id"], "s3_key": started["s3_key"]}
        resp = client.post(
            "/api/upload/complete", json={**body, "parts": []}, headers=as_user(BOB)
        )
        assert resp.status_code == 404
        assert s3.completed == []

    def test_only_the_uploader_aborts_it(self, aws, client: TestClient) -> None:
        s3, _ = aws
        key = upload(client, ALICE)
        resp = client.delete(
            "/api/upload/upload-1", params={"s3_key": key}, headers=as_user(BOB)
        )
        assert resp.status_code == 404
        assert s3.aborted == []
        mine = client.delete(
            "/api/upload/upload-1", params={"s3_key": key}, headers=as_user(ALICE)
        )
        assert mine.status_code == 200
        assert s3.aborted == [key]

    def test_no_job_on_another_users_upload(self, aws, client: TestClient) -> None:
        _, sqs = aws
        key = upload(client, ALICE)
        resp = client.post(
            "/api/jobs",
            json={"s3_key": key, "filename": "scan.las"},
            headers=as_user(BOB),
        )
        assert resp.status_code == 404
        assert job_store.list() == []
        assert sqs.sent == []

    @pytest.mark.parametrize(
        "key",
        [
            f"uploads/{uuid.uuid4()}/scan.las",  # a shared-mode key
            "uploads/test-001/scan.las",  # staged by hand
            f"uploads/{ALICE.upper()}/{uuid.uuid4()}/scan.las",
        ],
    )
    def test_a_key_no_user_owns(self, aws, client: TestClient, key: str) -> None:
        resp = client.post(
            "/api/jobs",
            json={"s3_key": key, "filename": "scan.las"},
            headers=as_user(ALICE),
        )
        assert resp.status_code == 404


class TestJobs:
    def test_each_user_lists_their_own(self, aws, client: TestClient) -> None:
        mine = alices_job(client)
        bobs = create_job(client, BOB, upload(client, BOB))
        listed = client.get("/api/jobs", headers=as_user(ALICE)).json()
        assert [j["job_id"] for j in listed["jobs"]] == [mine]
        assert listed["total"] == 1
        theirs = client.get("/api/jobs", headers=as_user(BOB)).json()
        assert [j["job_id"] for j in theirs["jobs"]] == [bobs]

    def test_another_users_job_is_not_found(self, aws, client: TestClient) -> None:
        job_id = alices_job(client)
        assert (
            client.get(f"/api/jobs/{job_id}", headers=as_user(ALICE)).status_code == 200
        )
        resp = client.get(f"/api/jobs/{job_id}", headers=as_user(BOB))
        missing = uuid.uuid4()
        absent = client.get(f"/api/jobs/{missing}", headers=as_user(BOB))
        assert resp.status_code == absent.status_code == 404
        assert resp.json()["detail"].replace(job_id, "") == absent.json()[
            "detail"
        ].replace(str(missing), "")

    def test_another_users_job_is_not_deleted(self, aws, client: TestClient) -> None:
        s3, _ = aws
        job_id = alices_job(client)
        s3.put(BUCKETS["S3_OUTPUT_BUCKET"], f"results/{job_id}/detections.json")
        before = dict(s3.objects)
        resp = client.delete(f"/api/jobs/{job_id}", headers=as_user(BOB))
        assert resp.status_code == 404
        assert s3.objects == before
        assert job_store.get(job_id) is not None
        mine = client.delete(f"/api/jobs/{job_id}", headers=as_user(ALICE))
        assert mine.status_code == 200
        assert job_store.get(job_id) is None

    def test_another_users_job_status_is_not_set(self, aws, client) -> None:
        job_id = alices_job(client, JobStatus.processing)
        resp = client.put(
            f"/api/jobs/{job_id}/status",
            params={"status": "failed", "error_message": "x"},
            headers=as_user(BOB),
        )
        assert resp.status_code == 404
        job = job_store.get(job_id)
        assert job is not None and job.status == JobStatus.processing


RESULT_PATHS = [
    (
        "/api/results/{job}/detections",
        "S3_OUTPUT_BUCKET",
        "results/{job}/detections.json",
    ),
    (
        "/api/results/{job}/segmentation",
        "S3_OUTPUT_BUCKET",
        "results/{job}/segmentation.json",
    ),
    ("/api/results/{job}/tiles/index", "S3_TILES_BUCKET", "tiles/{job}/index.json"),
    ("/api/results/{job}/tiles/tile_0", "S3_TILES_BUCKET", "tiles/{job}/tile_0.bin"),
    (
        "/api/results/{job}/tiles/tile_0/segmentation",
        "S3_TILES_BUCKET",
        "tiles/{job}/tile_0_seg.bin",
    ),
]


class TestResults:
    """The tiles rebuild the whole input scan, so every one of these is guarded."""

    @pytest.mark.parametrize(("path", "bucket", "key"), RESULT_PATHS)
    def test_only_the_owner_reads_them(
        self, aws, client: TestClient, path: str, bucket: str, key: str
    ) -> None:
        s3, _ = aws
        job_id = alices_job(client)
        s3.put(BUCKETS[bucket], key.format(job=job_id))
        url = path.format(job=job_id)
        assert client.get(url, headers=as_user(ALICE)).status_code == 200
        assert client.get(url, headers=as_user(BOB)).status_code == 404


class TestAnnotations:
    BODY = {
        "annotations": [
            {
                "center_xyz": [0, 0, 0],
                "size_wlh": [1, 1, 1],
                "heading": 0,
                "class_name": "chair",
            }
        ]
    }

    def test_only_the_owner_reads_and_writes(self, aws, client: TestClient) -> None:
        job_id = alices_job(client)
        url = f"/api/annotations/{job_id}"
        assert (
            client.post(url, json=self.BODY, headers=as_user(ALICE)).status_code == 201
        )
        assert client.get(url, headers=as_user(BOB)).status_code == 404
        overwrite = client.post(url, json={"annotations": []}, headers=as_user(BOB))
        assert overwrite.status_code == 404
        saved = client.get(url, headers=as_user(ALICE))
        assert saved.status_code == 200
        assert saved.json()["num_annotations"] == 1

    def test_no_annotations_for_a_job_that_does_not_exist(self, aws, client) -> None:
        url = f"/api/annotations/{uuid.uuid4()}"
        assert (
            client.post(url, json=self.BODY, headers=as_user(ALICE)).status_code == 404
        )
