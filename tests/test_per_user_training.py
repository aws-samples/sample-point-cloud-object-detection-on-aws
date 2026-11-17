"""Two users, per-user mode: sessions, training, models and labels (threat model T31).

Alice's training sessions, training jobs and fine-tuned models are hers alone; Bob gets
what he would get for ones that do not exist. Pretrained models stay everyone's.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from pointcloud_detect.api import model_registry as mr_mod
from pointcloud_detect.api.app import CLIENT_HEADER, app
from pointcloud_detect.api.deps import get_settings
from pointcloud_detect.api.models import JobStatus
from pointcloud_detect.api.routes import training as t
from pointcloud_detect.api.store import job_store
from pointcloud_detect.api.training_session_store import session_store
from tests._identity import ALICE, BOB, per_user, signed_in
from tests.test_job_store_dynamo import _job


class _NoSleep:
    @staticmethod
    def sleep(_seconds: float) -> None:
        pass


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("S3_MODELS_BUCKET", "test-models")
    per_user(monkeypatch)
    # Training runs in a background thread; the tests run it themselves, at once.
    monkeypatch.setattr(t.threading, "Thread", _InertThread)
    monkeypatch.setattr(t, "time", _NoSleep)
    registry = mr_mod.model_registry
    saved = (dict(registry._models), dict(registry._owners))
    job_store.clear()
    yield
    job_store.clear()
    with registry._lock:
        registry._models.clear()
        registry._models.update(saved[0])
        registry._owners.clear()
        registry._owners.update(saved[1])
    with t._lock:
        t._training_jobs.clear()
        t._training_owners.clear()
    with session_store._lock:
        session_store._sessions.clear()
        session_store._owners.clear()
    get_settings.cache_clear()


class _InertThread:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def as_user(sub: str) -> dict[str, str]:
    return {CLIENT_HEADER: "spa", **signed_in(sub)}


def a_job(owner: str) -> str:
    job_id = str(uuid.uuid4())
    job_store.create(_job(job_id), owner=owner)
    job_store.update_status(job_id, status=JobStatus.complete)
    return job_id


def a_session(client: TestClient, sub: str, classes: list[str] | None = None) -> str:
    resp = client.post(
        "/api/training-sessions",
        json={"name": "doors", "classes": classes or []},
        headers=as_user(sub),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def a_trained_model(client: TestClient, sub: str) -> tuple[str, str]:
    """Train on one of ``sub``'s jobs; return the training job and model ids."""
    resp = client.post(
        "/api/training",
        json={"job_id": a_job(sub), "base_model": "votenet_scannet", "epochs": 1},
        headers=as_user(sub),
    )
    assert resp.status_code == 201, resp.text
    tj_id = resp.json()["training_job_id"]
    t._simulate_training(tj_id, "votenet_scannet", 1)
    models = client.get("/api/models", headers=as_user(sub)).json()["models"]
    (tuned,) = [m for m in models if m.get("training_job_id") == tj_id]
    return tj_id, tuned["model_id"]


class TestSessions:
    def test_each_user_lists_their_own(self, api, client: TestClient) -> None:
        mine = a_session(client, ALICE)
        listed = client.get("/api/training-sessions", headers=as_user(ALICE)).json()
        assert [s["session_id"] for s in listed["sessions"]] == [mine]
        bobs = client.get("/api/training-sessions", headers=as_user(BOB)).json()
        assert bobs["sessions"] == []

    def test_another_users_session_is_not_found(self, api, client) -> None:
        sid = a_session(client, ALICE)
        url = f"/api/training-sessions/{sid}"
        assert client.get(url, headers=as_user(ALICE)).status_code == 200
        assert client.get(url, headers=as_user(BOB)).status_code == 404
        add = client.post(
            f"{url}/jobs", json={"job_id": a_job(BOB)}, headers=as_user(BOB)
        )
        assert add.status_code == 404

    def test_a_session_takes_only_the_callers_jobs(self, api, client) -> None:
        sid = a_session(client, ALICE)
        url = f"/api/training-sessions/{sid}/jobs"
        theirs = client.post(url, json={"job_id": a_job(BOB)}, headers=as_user(ALICE))
        assert theirs.status_code == 404
        mine = a_job(ALICE)
        ok = client.post(url, json={"job_id": mine}, headers=as_user(ALICE))
        assert ok.status_code == 200
        session = client.get(
            f"/api/training-sessions/{sid}", headers=as_user(ALICE)
        ).json()
        assert session["job_ids"] == [mine]

    def test_the_owner_is_not_returned(self, api, client: TestClient) -> None:
        sid = a_session(client, ALICE)
        body = client.get(f"/api/training-sessions/{sid}", headers=as_user(ALICE))
        assert ALICE not in body.text


class TestLabels:
    def test_another_users_session_classes_are_not_served(self, api, client) -> None:
        sid = a_session(client, ALICE, classes=["secret-class"])
        job_id = a_job(ALICE)
        client.post(
            f"/api/training-sessions/{sid}/jobs",
            json={"job_id": job_id},
            headers=as_user(ALICE),
        )
        url = f"/api/labels?job_id={job_id}"
        mine = client.get(url, headers=as_user(ALICE)).json()["labels"]
        assert [label["name"] for label in mine] == ["secret-class"]
        theirs = client.get(url, headers=as_user(BOB)).json()["labels"]
        assert "secret-class" not in [label["name"] for label in theirs]


class TestTraining:
    def test_only_on_the_callers_job(self, api, client: TestClient) -> None:
        resp = client.post(
            "/api/training",
            json={"job_id": a_job(ALICE), "base_model": "votenet_scannet"},
            headers=as_user(BOB),
        )
        assert resp.status_code == 404
        assert t._training_jobs == {}

    def test_another_users_training_job_is_not_found(self, api, client) -> None:
        tj_id, _ = a_trained_model(client, ALICE)
        url = f"/api/training/{tj_id}"
        assert client.get(url, headers=as_user(BOB)).status_code == 404
        assert client.delete(url, headers=as_user(BOB)).status_code == 404
        assert client.get(url, headers=as_user(ALICE)).status_code == 200
        listed = client.get("/api/training", headers=as_user(BOB)).json()
        assert listed == {"training_jobs": [], "total": 0}

    def test_the_owner_deletes_it(self, api, client: TestClient) -> None:
        tj_id, model_id = a_trained_model(client, ALICE)
        resp = client.delete(f"/api/training/{tj_id}", headers=as_user(ALICE))
        assert resp.status_code == 200
        assert resp.json()["unregistered_models"] == [model_id]


class TestModels:
    def test_a_tuned_model_is_its_trainers(self, api, client: TestClient) -> None:
        _, model_id = a_trained_model(client, ALICE)
        url = f"/api/models/{model_id}"
        assert client.get(url, headers=as_user(ALICE)).status_code == 200
        assert client.get(url, headers=as_user(BOB)).status_code == 404
        listed = client.get("/api/models", headers=as_user(BOB)).json()["models"]
        assert model_id not in [m["model_id"] for m in listed]
        assert ALICE not in client.get(url, headers=as_user(ALICE)).text

    def test_pretrained_models_are_everyones(self, api, client) -> None:
        listed = client.get("/api/models", headers=as_user(BOB)).json()["models"]
        ids = {m["model_id"] for m in listed}
        assert {m["model_id"] for m in mr_mod.PRETRAINED_MODELS} <= ids

    def test_no_training_on_another_users_model(self, api, client) -> None:
        _, model_id = a_trained_model(client, ALICE)
        resp = client.post(
            "/api/training",
            json={"job_id": a_job(BOB), "base_model": model_id},
            headers=as_user(BOB),
        )
        assert resp.status_code == 422

    def test_no_detection_with_another_users_model(
        self, api, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pointcloud_detect.api.routes import jobs as jobs_route

        class Sqs:
            def send_message(self, **_kwargs) -> dict:
                return {}

        monkeypatch.setattr(jobs_route, "get_sqs_client", lambda: Sqs())
        _, model_id = a_trained_model(client, ALICE)

        def job(owner: str, model: str) -> int:
            body = {
                "s3_key": f"uploads/{owner}/{uuid.uuid4()}/scan.las",
                "filename": "scan.las",
                "config": {"custom_weights_version": model},
            }
            return client.post(
                "/api/jobs", json=body, headers=as_user(owner)
            ).status_code

        assert job(BOB, model_id) == 422
        assert (
            job(BOB, "no_such_model") == 422
        )  # the same answer, so ids cannot be probed
        assert job(ALICE, model_id) == 201
