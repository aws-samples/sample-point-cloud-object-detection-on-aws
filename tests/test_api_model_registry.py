"""Tests for the API model registry and the training-job delete cascade.

Distinct from tests/test_registry.py, which covers the file-backed
``pointcloud_detect.registry.store.ModelRegistry`` keyed by version. This one
covers ``pointcloud_detect.api.model_registry``: the in-memory registry that
backs the UI model selector, plus the DELETE path that keeps it consistent with
the training-job store.
"""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from pointcloud_detect.api import model_registry as mr_mod
from pointcloud_detect.api.app import app
from pointcloud_detect.api.deps import get_settings
from pointcloud_detect.api.model_registry import ModelRegistrationError, ModelRegistry
from pointcloud_detect.api.routes import training as t
from pointcloud_detect.api.training_session_store import session_store

MODELS_BUCKET = "test-models-bucket"


def ckpt(name: str) -> str:
    """A checkpoint location the registry accepts: under the models bucket."""
    return f"s3://{MODELS_BUCKET}/models/{name}.pth"


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def models_bucket(monkeypatch: pytest.MonkeyPatch):
    """The registry validates checkpoints against S3_MODELS_BUCKET, which has no
    default."""
    monkeypatch.setenv("S3_MODELS_BUCKET", MODELS_BUCKET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolate_singletons():
    """Restore the module-level registry and training store around each test."""
    saved_models = {k: v.copy() for k, v in mr_mod.model_registry._models.items()}
    with t._lock:
        saved_jobs = dict(t._training_jobs)
    with session_store._lock:
        saved_sessions = dict(session_store._sessions)
    yield
    with mr_mod.model_registry._lock:
        mr_mod.model_registry._models.clear()
        mr_mod.model_registry._models.update(saved_models)
    with t._lock:
        t._training_jobs.clear()
        t._training_jobs.update(saved_jobs)
    with session_store._lock:
        session_store._sessions.clear()
        session_store._sessions.update(saved_sessions)


@pytest.fixture
def registry() -> ModelRegistry:
    """A fresh registry instance, seeded with the pretrained models."""
    return ModelRegistry()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestUnregisterByTrainingJob:
    def test_removes_the_model_a_job_produced(self, registry: ModelRegistry) -> None:
        registry.register(
            model_id="votenet_scannet_ft_abc12345",
            name="Doors v1",
            base_model="votenet_scannet",
            checkpoint_path=ckpt("votenet_scannet_finetuned_abc12345"),
            classes=["door"],
            training_job_id="job-abc",
        )
        assert registry.get("votenet_scannet_ft_abc12345") is not None

        removed = registry.unregister_by_training_job("job-abc")

        assert removed == ["votenet_scannet_ft_abc12345"]
        assert registry.get("votenet_scannet_ft_abc12345") is None

    def test_is_a_noop_for_an_unknown_job(self, registry: ModelRegistry) -> None:
        before = len(registry.list_all())
        assert registry.unregister_by_training_job("never-existed") == []
        assert len(registry.list_all()) == before

    def test_leaves_other_jobs_models_alone(self, registry: ModelRegistry) -> None:
        for suffix, job in (("aaa", "job-a"), ("bbb", "job-b")):
            registry.register(
                model_id=f"votenet_scannet_ft_{suffix}",
                name=f"model {suffix}",
                base_model="votenet_scannet",
                checkpoint_path=ckpt(suffix),
                classes=["door"],
                training_job_id=job,
            )

        assert registry.unregister_by_training_job("job-a") == [
            "votenet_scannet_ft_aaa"
        ]
        assert registry.get("votenet_scannet_ft_aaa") is None
        assert registry.get("votenet_scannet_ft_bbb") is not None

    def test_removes_every_model_from_one_job(self, registry: ModelRegistry) -> None:
        for suffix in ("one", "two"):
            registry.register(
                model_id=f"votenet_scannet_ft_{suffix}",
                name=f"model {suffix}",
                base_model="votenet_scannet",
                checkpoint_path=ckpt(suffix),
                classes=["door"],
                training_job_id="job-multi",
            )

        assert sorted(registry.unregister_by_training_job("job-multi")) == [
            "votenet_scannet_ft_one",
            "votenet_scannet_ft_two",
        ]
        assert [m for m in registry.list_all() if m["type"] == "finetuned"] == []

    def test_never_removes_a_pretrained_model(self, registry: ModelRegistry) -> None:
        """The type guard, not just the absence of a training_job_id, must hold.

        A pretrained entry that somehow carried a matching training_job_id would
        otherwise be deleted, permanently emptying the model selector for every
        user of the process.
        """
        pretrained_ids = [
            m["model_id"] for m in registry.list_all() if m["type"] == "pretrained"
        ]
        assert pretrained_ids, "expected seeded pretrained models"
        with registry._lock:
            registry._models[pretrained_ids[0]]["training_job_id"] = "job-x"

        assert registry.unregister_by_training_job("job-x") == []
        assert registry.get(pretrained_ids[0]) is not None

    def test_pretrained_models_survive_a_finetuned_removal(
        self, registry: ModelRegistry
    ) -> None:
        before = {
            m["model_id"] for m in registry.list_all() if m["type"] == "pretrained"
        }
        registry.register(
            model_id="votenet_scannet_ft_zzz",
            name="ft",
            base_model="votenet_scannet",
            checkpoint_path=ckpt("zzz"),
            classes=[],
            training_job_id="job-z",
        )
        registry.unregister_by_training_job("job-z")

        after = {
            m["model_id"] for m in registry.list_all() if m["type"] == "pretrained"
        }
        assert after == before


class TestDeleteTrainingJobCascade:
    """DELETE /api/training/{id} must not leave a selectable model behind."""

    # Training job ids are UUIDs, and the route refuses anything else (M56).
    JOB_DEL = "0d3c3f1e-5a53-4d7e-9b58-4c1b0f6a0001"
    JOB_KEEP = "0d3c3f1e-5a53-4d7e-9b58-4c1b0f6a0002"
    JOB_BARE = "0d3c3f1e-5a53-4d7e-9b58-4c1b0f6a0003"

    def _seed(
        self, job_id: str = JOB_DEL, model_id: str = "votenet_scannet_ft_del00000"
    ) -> None:
        with t._lock:
            t._training_jobs[job_id] = {
                "training_job_id": job_id,
                "job_id": new_id(),
                "status": "complete",
                "custom_classes": ["door"],
            }
        mr_mod.model_registry.register(
            model_id=model_id,
            name="Doors v1",
            base_model="votenet_scannet",
            checkpoint_path=ckpt(model_id),
            classes=["door"],
            training_job_id=job_id,
        )

    def test_delete_unregisters_the_model(self, client: TestClient) -> None:
        self._seed()
        assert mr_mod.model_registry.get("votenet_scannet_ft_del00000") is not None

        response = client.delete(f"/api/training/{self.JOB_DEL}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "deleted"
        assert body["unregistered_models"] == ["votenet_scannet_ft_del00000"]
        assert mr_mod.model_registry.get("votenet_scannet_ft_del00000") is None

    def test_deleted_model_disappears_from_the_selector(
        self, client: TestClient
    ) -> None:
        self._seed()
        listed = client.get("/api/models").json()["models"]
        assert "votenet_scannet_ft_del00000" in [m["model_id"] for m in listed]

        client.delete(f"/api/training/{self.JOB_DEL}")

        listed = client.get("/api/models").json()["models"]
        assert "votenet_scannet_ft_del00000" not in [m["model_id"] for m in listed]

    def test_delete_reports_nothing_when_the_job_produced_no_model(
        self, client: TestClient
    ) -> None:
        with t._lock:
            t._training_jobs[self.JOB_BARE] = {
                "training_job_id": self.JOB_BARE,
                "status": "processing",
            }

        response = client.delete(f"/api/training/{self.JOB_BARE}")

        assert response.status_code == 200
        assert response.json()["unregistered_models"] == []

    def test_delete_is_404_for_an_unknown_job(self, client: TestClient) -> None:
        assert client.delete(f"/api/training/{new_id()}").status_code == 404

    def test_a_non_uuid_id_is_refused(self, client: TestClient) -> None:
        assert client.delete("/api/training/never-existed").status_code == 422

    def test_second_delete_is_404(self, client: TestClient) -> None:
        self._seed()
        assert client.delete(f"/api/training/{self.JOB_DEL}").status_code == 200
        assert client.delete(f"/api/training/{self.JOB_DEL}").status_code == 404

    def test_delete_does_not_disturb_another_jobs_model(
        self, client: TestClient
    ) -> None:
        self._seed()
        self._seed(job_id=self.JOB_KEEP, model_id="votenet_scannet_ft_keep0000")

        client.delete(f"/api/training/{self.JOB_DEL}")

        assert mr_mod.model_registry.get("votenet_scannet_ft_keep0000") is not None
        with t._lock:
            assert self.JOB_KEEP in t._training_jobs


class TestPretrainedRoot:
    """Generated ids must stay a fixed shape however deep the lineage goes."""

    def test_a_pretrained_id_resolves_to_itself(self) -> None:
        assert t._pretrained_root("votenet_scannet") == "votenet_scannet"

    def test_an_unknown_id_resolves_to_itself(self) -> None:
        assert t._pretrained_root("not-in-the-registry") == "not-in-the-registry"

    def test_resolves_through_successive_generations(self) -> None:
        parent = "votenet_scannet"
        for gen in range(1, 5):
            child = f"votenet_scannet_ft_gen{gen}"
            mr_mod.model_registry.register(
                model_id=child,
                name=f"gen {gen}",
                base_model=parent,
                checkpoint_path=ckpt(child),
                classes=["door"],
                training_job_id=f"job-gen{gen}",
            )
            assert t._pretrained_root(child) == "votenet_scannet"
            parent = child

    def test_derived_ids_do_not_compound_across_generations(self) -> None:
        """The bug this guards: ids grew to votenet_scannet_ft_a_ft_b_ft_c."""
        parent = "votenet_scannet"
        for gen in range(1, 5):
            root = t._pretrained_root(parent)
            derived = f"{root}_ft_{f'gen{gen}':>8}".replace(" ", "0")
            assert derived.count("_ft_") == 1
            mr_mod.model_registry.register(
                model_id=derived,
                name=f"gen {gen}",
                base_model=parent,
                checkpoint_path=ckpt(derived),
                classes=["door"],
                training_job_id=f"job-gen{gen}",
            )
            parent = derived

    def test_a_cyclic_chain_terminates(self) -> None:
        for a, b in (("cyc_a", "cyc_b"), ("cyc_b", "cyc_a")):
            mr_mod.model_registry.register(
                model_id=a,
                name=a,
                base_model=b,
                checkpoint_path=ckpt(a),
                classes=[],
                training_job_id=f"job-{a}",
            )

        assert t._pretrained_root("cyc_a") in {"cyc_a", "cyc_b"}

    def test_a_self_referential_chain_terminates(self) -> None:
        mr_mod.model_registry.register(
            model_id="self_ref",
            name="self",
            base_model="self_ref",
            checkpoint_path=ckpt("self_ref"),
            classes=[],
            training_job_id="job-self",
        )
        assert t._pretrained_root("self_ref") == "self_ref"


class TestDeleteDuringTrainingRace:
    """A DELETE landing mid-completion must not leave an undeletable model.

    Registration used to sit outside the lock that DELETE pops the job under, so a
    DELETE arriving between the "complete" write and the register() call found
    nothing to unregister and the thread then registered a model whose
    training_job_id no longer resolved. DELETE 404s on a missing job, so nothing
    could ever remove it — and because the UI promotes the newest fine-tuned model
    to the default, the orphan became a sticky default pointing at weights the
    user believed they had deleted.

    The natural window is sub-millisecond, so this drives the interleaving
    explicitly rather than racing and hoping.
    """

    class _NoSleep:
        """Stand-in for training.py's `time`, so the stub delays cost nothing."""

        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

    def test_delete_mid_completion_leaves_no_orphaned_model(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = new_id()
        inside_window = threading.Event()
        release = threading.Event()
        real_update = t._update_linked_session

        def blocking_update(tj_id: str, artifact: str) -> None:
            real_update(tj_id, artifact)
            # Past the "complete" write, before registration: hold here so the
            # DELETE is guaranteed to land inside the window.
            inside_window.set()
            assert release.wait(timeout=10), "main thread never released the window"

        monkeypatch.setattr(t, "time", self._NoSleep)
        monkeypatch.setattr(t, "_update_linked_session", blocking_update)

        with t._lock:
            t._training_jobs[job_id] = {
                "training_job_id": job_id,
                "job_id": "src-job",
                "status": "queued",
                "custom_classes": ["chair", "table"],
            }

        thread = threading.Thread(
            target=t._simulate_training,
            args=(job_id, "votenet_scannet", 1),
            daemon=True,
        )
        thread.start()
        try:
            assert inside_window.wait(timeout=10), "never reached the unguarded window"

            response = client.delete(f"/api/training/{job_id}")
            assert response.status_code == 200
            # Nothing registered yet, so the cascade legitimately finds nothing.
            assert response.json()["unregistered_models"] == []
        finally:
            release.set()
            thread.join(timeout=10)

        assert not thread.is_alive(), "training thread did not finish"

        orphans = [
            m["model_id"]
            for m in mr_mod.model_registry.list_all()
            if m.get("training_job_id") == job_id
        ]
        assert orphans == [], f"deleted job left selectable model(s): {orphans}"

        # And the job really is gone, so no later DELETE could clean up an orphan.
        assert client.delete(f"/api/training/{job_id}").status_code == 404

    def test_session_records_its_trained_model_on_completion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session linked only by job-id membership must record its result.

        _update_linked_session used to also require status == "training", which
        only POST /training-sessions/{id}/train ever set. Once fine-tuning moved to
        the annotation viewer nothing set it, so the condition could never hold and
        sessions stayed "collecting" with model_artifact None forever.
        """
        job_id = "session-linked-job"
        source_job_id = "annotated-src"
        monkeypatch.setattr(t, "time", self._NoSleep)

        session = session_store.create(
            name="Doors",
            base_model="votenet_scannet",
            coord_frame="z_up",
            use_color=False,
            use_intensity=False,
            classes=["door"],
        )
        session_store.add_job(session["session_id"], source_job_id)
        # get() returns dict | None; bind and assert so a store miss fails as a clear
        # assertion rather than a TypeError on subscripting None.
        created = session_store.get(session["session_id"])
        assert created is not None
        assert created["status"] == "collecting"

        with t._lock:
            t._training_jobs[job_id] = {
                "training_job_id": job_id,
                "job_id": source_job_id,
                "status": "queued",
                "custom_classes": ["door"],
            }

        t._simulate_training(job_id, "votenet_scannet", 1)

        updated = session_store.get(session["session_id"])
        assert updated is not None
        assert updated["training_job_id"] == job_id
        assert updated["model_artifact"] is not None
        assert updated["status"] == "complete"

    def test_completion_without_a_delete_still_registers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard against 'fixing' the race by never registering at all."""
        job_id = "happy-job"
        monkeypatch.setattr(t, "time", self._NoSleep)

        with t._lock:
            t._training_jobs[job_id] = {
                "training_job_id": job_id,
                "job_id": "src-job",
                "status": "queued",
                "custom_classes": ["chair"],
            }

        t._simulate_training(job_id, "votenet_scannet", 1)

        registered = [
            m
            for m in mr_mod.model_registry.list_all()
            if m.get("training_job_id") == job_id
        ]
        assert len(registered) == 1
        assert registered[0]["classes"] == ["chair"]
        assert registered[0]["model_id"].count("_ft_") == 1


class TestRegistrationGuards:
    """T34: registration never overwrites, and only names the models bucket."""

    def _register(self, registry: ModelRegistry, **over) -> dict:
        kwargs = {
            "model_id": "votenet_scannet_ft_guard000",
            "name": "guard",
            "base_model": "votenet_scannet",
            "checkpoint_path": ckpt("guard"),
            "classes": ["door"],
            "training_job_id": "job-guard",
            **over,
        }
        return registry.register(**kwargs)

    def test_a_models_bucket_key_is_accepted(self, registry: ModelRegistry) -> None:
        model = self._register(registry)
        assert model["checkpoint_path"] == ckpt("guard")
        assert registry.get("votenet_scannet_ft_guard000") is not None

    def test_an_existing_id_is_never_overwritten(self, registry: ModelRegistry) -> None:
        self._register(registry)
        with pytest.raises(ModelRegistrationError, match="already registered"):
            self._register(registry, name="impostor", checkpoint_path=ckpt("other"))
        kept = registry.get("votenet_scannet_ft_guard000")
        assert kept is not None
        assert kept["name"] == "guard"
        assert kept["checkpoint_path"] == ckpt("guard")

    @pytest.mark.parametrize(
        "model_id",
        ["fcaf3d_scannet", "votenet_scannet", "centerpoint_nuscenes"],
    )
    def test_a_pretrained_id_cannot_be_shadowed(
        self, registry: ModelRegistry, model_id: str
    ) -> None:
        before = dict(registry.get(model_id) or {})
        with pytest.raises(ModelRegistrationError):
            self._register(registry, model_id=model_id)
        assert registry.get(model_id) == before
        assert before["type"] == "pretrained"

    def test_a_pretrained_id_stays_reserved_even_if_absent(
        self, registry: ModelRegistry
    ) -> None:
        # The reservation does not depend on the seeded entry still being present.
        with registry._lock:
            del registry._models["pointpillars_kitti"]
        with pytest.raises(ModelRegistrationError):
            self._register(registry, model_id="pointpillars_kitti")

    @pytest.mark.parametrize(
        "path",
        [
            "models/guard.pth",  # a bare key, as the stub used to register
            "/tmp/guard.pth",
            "https://evil.example/guard.pth",
            "pretrained",
            f"s3://{MODELS_BUCKET}/guard.pth",  # outside models/
            f"s3://{MODELS_BUCKET}/models/",  # no key
            f"s3://{MODELS_BUCKET}/models/../guard.pth",
            f"s3://{MODELS_BUCKET}/models/./guard.pth",
            f"s3://{MODELS_BUCKET}/models//guard.pth",
            f"s3://{MODELS_BUCKET}/models/a\\guard.pth",
            f"s3://{MODELS_BUCKET}-other/models/guard.pth",  # a bucket-name prefix
            "s3://another-bucket/models/guard.pth",
        ],
    )
    def test_a_checkpoint_outside_the_models_prefix_is_refused(
        self, registry: ModelRegistry, path: str
    ) -> None:
        with pytest.raises(ModelRegistrationError):
            self._register(registry, checkpoint_path=path)
        assert registry.get("votenet_scannet_ft_guard000") is None

    def test_no_models_bucket_means_nothing_is_accepted(
        self, registry: ModelRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("S3_MODELS_BUCKET")
        get_settings.cache_clear()
        with pytest.raises(ModelRegistrationError, match="S3_MODELS_BUCKET"):
            self._register(registry, checkpoint_path="s3:///models/guard.pth")


class TestPublicRegistrationRemoved:
    """T34: no client can register a model; listing and lookup remain."""

    def test_post_is_not_routed(self, client: TestClient) -> None:
        r = client.post(
            "/api/models",
            json={
                "model_id": "m-1",
                "name": "m",
                "base_model": "votenet_scannet",
                "checkpoint_path": ckpt("m-1"),
            },
        )
        assert r.status_code == 405
        assert mr_mod.model_registry.get("m-1") is None

    def test_no_write_method_on_the_models_routes(self) -> None:
        schema = app.openapi()["paths"]
        for path, ops in schema.items():
            if path.startswith("/api/models"):
                assert set(ops) == {"get"}, f"{path}: {sorted(ops)}"

    def test_list_and_get_still_work(self, client: TestClient) -> None:
        listed = client.get("/api/models")
        assert listed.status_code == 200
        assert "votenet_scannet" in [m["model_id"] for m in listed.json()["models"]]
        assert client.get("/api/models/votenet_scannet").status_code == 200
        assert client.get("/api/models/not-a-model").status_code == 404


class TestTrainingPathRegistration:
    """The one remaining registration path complies with the registry's rules."""

    class _NoSleep:
        @staticmethod
        def sleep(_seconds: float) -> None:
            return None

    def _run(self, monkeypatch: pytest.MonkeyPatch, job_id: str) -> dict:
        monkeypatch.setattr(t, "time", self._NoSleep)
        with t._lock:
            t._training_jobs[job_id] = {
                "training_job_id": job_id,
                "job_id": new_id(),
                "status": "queued",
                "custom_classes": ["door"],
            }
        t._simulate_training(job_id, "votenet_scannet", 1)
        with t._lock:
            return dict(t._training_jobs[job_id])

    def test_registers_under_the_models_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = new_id()
        job = self._run(monkeypatch, job_id)

        (model,) = [
            m
            for m in mr_mod.model_registry.list_all()
            if m.get("training_job_id") == job_id
        ]
        key = f"models/votenet_scannet_finetuned_{job_id[:8]}.pth"
        assert model["checkpoint_path"] == f"s3://{MODELS_BUCKET}/{key}"
        # The job still reports the key, as before.
        assert job["status"] == "complete"
        assert job["model_artifact"] == key

    def test_a_taken_model_id_fails_the_job_instead_of_overwriting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = new_id()
        taken = f"votenet_scannet_ft_{job_id[:8]}"
        mr_mod.model_registry.register(
            model_id=taken,
            name="first",
            base_model="votenet_scannet",
            checkpoint_path=ckpt("first"),
            classes=["door"],
            training_job_id="job-first",
        )

        job = self._run(monkeypatch, job_id)

        assert job["status"] == "failed"
        assert job["model_artifact"] is None
        assert "already registered" in job["error_message"]
        kept = mr_mod.model_registry.get(taken)
        assert kept is not None and kept["name"] == "first"


class TestTrainingRequestValidation:
    """M56: TrainingRequest.job_id is a UUID; the base model must be registered."""

    def test_a_non_uuid_job_id_is_refused(self, client: TestClient) -> None:
        r = client.post(
            "/api/training",
            json={"job_id": "https://evil.example/", "base_model": "votenet_scannet"},
        )
        assert r.status_code == 422
        with t._lock:
            assert not t._training_jobs

    @pytest.mark.parametrize("job_id", ["/\\evil.example", "..", "a" * 36])
    def test_navigation_payloads_are_refused(
        self, client: TestClient, job_id: str
    ) -> None:
        r = client.post("/api/training", json={"job_id": job_id})
        assert r.status_code == 422

    def test_an_unregistered_base_model_is_refused(self, client: TestClient) -> None:
        r = client.post(
            "/api/training", json={"job_id": new_id(), "base_model": "../../x"}
        )
        assert r.status_code == 422
        assert "base_model" in r.text

    def test_the_job_id_is_stored_in_canonical_form(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No thread: this is about what submit_training stores.
        monkeypatch.setattr(t.threading, "Thread", _InertThread)
        source = uuid.uuid4()
        r = client.post(
            "/api/training",
            json={"job_id": str(source).upper(), "base_model": "votenet_scannet"},
        )
        assert r.status_code == 201
        assert r.json()["job_id"] == str(source)

    @pytest.mark.parametrize(
        "body",
        [
            {"epochs": 0},
            {"epochs": 1001},
            {"custom_classes": ["x" * 65]},
            {"custom_classes": [f"c{i}" for i in range(101)]},
            {"base_model": "x" * 129},
        ],
    )
    def test_bounds(self, client: TestClient, body: dict) -> None:
        r = client.post("/api/training", json={"job_id": new_id(), **body})
        assert r.status_code == 422


class _InertThread:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass


class TestNoncommercialWeightsGate:
    """Pretrained checkpoints are non-commercially licensed and opt-in only."""

    JOB = {"s3_key": "uploads/a/b.las", "filename": "b.las"}

    @pytest.fixture(autouse=True)
    def real_inference_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Behave as the gpu/cpu images, where a pretrained model would be fetched.
        CI has no mmdet3d, which is the stub case tested separately below."""
        from pointcloud_detect.backends import checkpoints

        monkeypatch.setattr(checkpoints, "_ml_stack_installed", lambda: True)

    @pytest.fixture(autouse=True)
    def no_sqs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pointcloud_detect.api.routes import jobs as jobs_route

        class Sqs:
            def send_message(self, **_kwargs):
                return {"MessageId": "m"}

        monkeypatch.setattr(jobs_route, "get_sqs_client", lambda: Sqs())

    def _models(self, client: TestClient) -> dict[str, dict]:
        return {m["model_id"]: m for m in client.get("/api/models").json()["models"]}

    def test_pretrained_listed_but_unavailable_by_default(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        m = self._models(client)["votenet_scannet"]
        assert m["available"] is False
        assert m["noncommercial"] is True

    def test_pretrained_available_when_opted_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOW_NONCOMMERCIAL_WEIGHTS", "1")
        assert self._models(client)["votenet_scannet"]["available"] is True

    def test_listing_does_not_mutate_the_registry(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        self._models(client)
        stored = mr_mod.model_registry.get("votenet_scannet")
        assert stored is not None
        assert "available" not in stored

    def test_a_finetuned_model_is_available_without_the_opt_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        mr_mod.model_registry.register(
            model_id="votenet_scannet_ft_clean0001",
            name="Clean",
            base_model="votenet_scannet",
            checkpoint_path=ckpt("votenet_scannet_ft_clean0001"),
            classes=["door"],
        )
        m = self._models(client)["votenet_scannet_ft_clean0001"]
        assert m["available"] is True
        assert m.get("noncommercial") is not True

    def test_default_detection_job_is_refused_by_default(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        r = client.post("/api/jobs", json=self.JOB)
        assert r.status_code == 400
        assert "ALLOW_NONCOMMERCIAL_WEIGHTS" in r.json()["detail"]

    def test_a_stub_image_accepts_pretrained_selections_without_the_opt_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No mmdet3d: the backends fall back to placeholder boxes before any fetch.
        from pointcloud_detect.backends import checkpoints

        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        monkeypatch.setattr(checkpoints, "_ml_stack_installed", lambda: False)
        assert self._models(client)["votenet_scannet"]["available"] is True
        assert client.post("/api/jobs", json=self.JOB).status_code == 201

    def test_default_detection_job_is_accepted_when_opted_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOW_NONCOMMERCIAL_WEIGHTS", "1")
        assert client.post("/api/jobs", json=self.JOB).status_code == 201

    def test_a_job_on_a_finetuned_model_needs_no_opt_in(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        mr_mod.model_registry.register(
            model_id="votenet_scannet_ft_clean0002",
            name="Clean",
            base_model="votenet_scannet",
            checkpoint_path=ckpt("votenet_scannet_ft_clean0002"),
            classes=["door"],
        )
        body = {
            **self.JOB,
            "config": {"custom_weights_version": "votenet_scannet_ft_clean0002"},
        }
        assert client.post("/api/jobs", json=body).status_code == 201
