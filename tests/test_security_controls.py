"""Tests for the mitigations recorded in docs/security/THREAT_MODEL.md.

Each class names the threat it guards. They are written to fail against the code as
it stood before the mitigation, not to describe the new behaviour after the fact.
The disk-headroom control (T15) is tested alongside the other extract-directory
tests in test_memory_budget.py.
"""

import importlib.util
import json
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pointcloud_detect.api.app import CLIENT_HEADER, app
from pointcloud_detect.api.deps import Settings
from pointcloud_detect.api.hosts import HostAllowlistMiddleware, host_name
from pointcloud_detect.api.models import (
    JobCreateRequest,
    UploadInitiateRequest,
    validate_input_key,
)


class TestNoApplicationAuthentication:
    """T16, T31: authentication moved out of the app, to the ALB and to RBAC.

    The shared bearer token was the app's own authentication. It is gone, replaced
    by the ALB's Cognito action in public mode and by Kubernetes RBAC in port-forward
    mode, both pinned by tests/test_helm_render.py. These tests keep it gone: a
    half-removed token check would either lock every user out or read as a control
    that is not there. Per-user mode verifies the ALB's signed claims but issues and
    stores no credential of its own (tests/test_access_scope.py).
    """

    def test_auth_module_is_gone(self) -> None:
        assert importlib.util.find_spec("pointcloud_detect.api.auth") is None

    def test_no_token_settings(self) -> None:
        assert not [f for f in Settings.model_fields if f.startswith("api_auth")]

    def test_no_route_declares_a_security_scheme(self) -> None:
        schema = app.openapi()
        assert "securitySchemes" not in schema.get("components", {})
        for path, ops in schema["paths"].items():
            for method, op in ops.items():
                assert "security" not in op, f"{method.upper()} {path}"

    def test_api_served_without_credentials(self) -> None:
        # In shared mode, behind the ALB and on port-forward alike, the app asks for
        # nothing. Per-user mode is tested in tests/test_access_scope.py.
        assert TestClient(app).get("/api/models").status_code == 200

    def test_worker_callback_sends_no_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pointcloud_detect.worker import processor

        monkeypatch.setenv("JOB_STORE_BACKEND", "memory")
        monkeypatch.setattr(processor, "API_BASE_URL", "http://api.test")
        with mock.patch("urllib.request.urlopen") as urlopen:
            processor._update_status("job-1", "processing")
        req = urlopen.call_args.args[0]
        assert req.get_method() == "PUT"
        assert not req.has_header("Authorization")


class TestCors:
    """T5: a wildcard origin was configured alongside allow_credentials."""

    def test_no_cross_origin_by_default(self) -> None:
        r = TestClient(app).get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers

    def test_wildcard_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pointcloud_detect.api import app as app_mod

        monkeypatch.setattr(
            app_mod, "get_settings", lambda: Settings(cors_allowed_origins="*")
        )
        with pytest.raises(ValueError):
            app_mod._cors_origins()

    def test_origins_parsed_from_csv(self) -> None:
        s = Settings(cors_allowed_origins="https://a.example, https://b.example")
        assert s.cors_origins == ["https://a.example", "https://b.example"]

    @staticmethod
    def _cors_kwargs() -> dict:
        (cors,) = [m for m in app.user_middleware if m.cls is CORSMiddleware]
        return dict(cors.kwargs)

    def test_headers_are_content_type_and_the_client_header(self) -> None:
        kwargs = self._cors_kwargs()
        # No Authorization: nothing in the app reads it any more.
        assert kwargs["allow_headers"] == ["Content-Type", "X-PCD-Client"]
        assert kwargs["allow_credentials"] is False
        assert CLIENT_HEADER == "X-PCD-Client"

    @pytest.mark.parametrize(
        ("header", "allowed"),
        [("x-pcd-client", True), ("content-type", True), ("authorization", False)],
    )
    def test_preflight_for_an_allowed_origin(self, header: str, allowed: bool) -> None:
        # The same middleware settings, with one origin allowed, answering a real
        # preflight: what an adopter who sets CORS_ALLOWED_ORIGINS gets.
        probe = FastAPI()
        probe.add_middleware(
            CORSMiddleware,
            **{**self._cors_kwargs(), "allow_origins": ["https://a.example"]},
        )
        r = TestClient(probe).options(
            "/api/jobs",
            headers={
                "Origin": "https://a.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": header,
            },
        )
        assert (r.status_code == 200) is allowed


class TestHostAllowlist:
    """T69: a DNS-rebinding page could read the whole API through the api tunnel.

    `kubectl port-forward svc/api` had neither of the rules nginx applies on the
    frontend tunnel. A page that re-points its own name at 127.0.0.1 is same-origin
    with the tunnel, but its Host is still its own name.
    """

    ALLOWED = ["localhost", "127.0.0.1", "api.pointcloud-detection.svc.cluster.local"]

    @staticmethod
    def _client(allowed: list[str]) -> TestClient:
        probe = FastAPI()
        probe.add_api_route("/api/jobs", lambda: {"jobs": []})
        probe.add_api_route("/health", lambda: {"status": "ok"})
        probe.add_middleware(HostAllowlistMiddleware, allowed_hosts=allowed)
        return TestClient(probe)

    @pytest.mark.parametrize(
        "host",
        [
            "localhost:8000",
            "127.0.0.1:8000",
            "LOCALHOST:8000",
            "api.pointcloud-detection.svc.cluster.local",
            "api.pointcloud-detection.svc.cluster.local:80",
        ],
    )
    def test_own_names_are_served(self, host: str) -> None:
        r = self._client(self.ALLOWED).get("/api/jobs", headers={"host": host})
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "host", ["rebind.attacker.example:8000", "attacker.example", "", "[::1]:8000"]
    )
    def test_foreign_host_is_refused(self, host: str) -> None:
        r = self._client(self.ALLOWED).get("/api/jobs", headers={"host": host})
        assert r.status_code == 421
        assert "attacker" not in r.text  # the Host is not echoed back

    def test_health_is_exempt(self) -> None:
        # The kubelet and the ALB probe /health by pod IP.
        r = self._client(self.ALLOWED).get("/health", headers={"host": "10.0.3.7:8000"})
        assert r.status_code == 200

    def test_unset_checks_nothing(self) -> None:
        # The local modes and the test client set no list.
        r = self._client([]).get("/api/jobs", headers={"host": "anything.example"})
        assert r.status_code == 200

    @pytest.mark.parametrize(
        ("header", "name"),
        [
            ("Api.Example.com:443", "api.example.com"),
            ("[::1]:8000", "[::1]"),
            ("x", "x"),
        ],
    )
    def test_host_name(self, header: str, name: str) -> None:
        assert host_name(header) == name

    def test_the_app_reads_the_list_from_settings(self) -> None:
        (mw,) = [m for m in app.user_middleware if m.cls is HostAllowlistMiddleware]
        assert mw.kwargs["allowed_hosts"] == Settings().allowed_host_names
        s = Settings(allowed_hosts="localhost, pcd.example.com")
        assert s.allowed_host_names == ["localhost", "pcd.example.com"]


class TestJsonBodiesOnly:
    """T68: a POST that reads only query parameters is a CSRF-simple request.

    It was POST /api/models, which took its fields as query parameters. That route
    is gone (T34, tests/test_api_model_registry.py). The control it prompted,
    FastAPI's strict_content_type, still guards every other POST, checked here on
    training-session creation.
    """

    body = {"name": "doors", "classes": ["door"]}

    def test_query_parameters_are_refused(self) -> None:
        r = TestClient(app).post("/api/training-sessions", params=self.body)
        assert r.status_code == 422

    @pytest.mark.parametrize("content_type", [None, "text/plain"])
    def test_json_needs_a_json_content_type(self, content_type: str | None) -> None:
        # A cross-site form or no-cors fetch can send these without a preflight.
        # FastAPI must not read them as JSON (strict_content_type).
        headers = {"content-type": content_type} if content_type else {}
        r = TestClient(app).post(
            "/api/training-sessions",
            content=json.dumps(self.body).encode(),
            headers=headers,
        )
        assert r.status_code == 422

    def test_json_body_is_read(self) -> None:
        r = TestClient(app).post("/api/training-sessions", json=self.body)
        assert r.status_code == 201
        assert r.json()["classes"] == ["door"]


class TestStatusCallback:
    """T17: PUT /api/jobs/{id}/status let any client rewrite any job's state."""

    def test_refused_with_shared_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOB_STORE_BACKEND", "dynamodb")
        r = TestClient(app).put(
            "/api/jobs/5b1c2f7e-0000-4000-8000-000000000000/status",
            params={"status": "complete"},
        )
        assert r.status_code == 403


class TestInputKeyValidation:
    """T23: clients chose arbitrary input keys and filenames."""

    @pytest.mark.parametrize(
        "key",
        ["uploads/abc/scene.las", "uploads/test-001/000000.bin", "uploads/abc/S.E57"],
    )
    def test_accepts_uploads_keys(self, key: str) -> None:
        assert validate_input_key(key) == key

    @pytest.mark.parametrize(
        "key",
        [
            "results/abc/detections.json",  # outside uploads/
            "other/abc/scene.las",
            "uploads/scene.las",  # no id segment
            "uploads/abc/..",
            "uploads/../x/scene.las",
            "uploads//scene.las",
            "uploads/abc/scene.exe",
            "uploads\\abc\\scene.las",
        ],
    )
    def test_rejects(self, key: str) -> None:
        with pytest.raises(ValueError):
            validate_input_key(key)

    def test_job_create_rejects_foreign_key(self) -> None:
        with pytest.raises(ValidationError):
            JobCreateRequest(s3_key="results/x/detections.json", filename="x")

    @pytest.mark.parametrize("name", ["../x.las", "a/b.las", "..", "a\\b.las"])
    def test_upload_filename_must_be_bare(self, name: str) -> None:
        with pytest.raises(ValidationError):
            UploadInitiateRequest(filename=name, file_size_bytes=1)

    def test_worker_rechecks_key(self) -> None:
        # The queue is a second entry point: anything with sqs:SendMessage can
        # enqueue a job without going through the API's validation.
        from pointcloud_detect.worker.processor import process_job

        with pytest.raises(ValueError):
            process_job("j", "uploads/abc/..", {}, s3_client=None)


class TestCheckpointSelection:
    """T18: an unknown custom_weights_version was loaded as a checkpoint path."""

    @pytest.mark.parametrize(
        "value", ["/tmp/evil.pth", "https://evil.example/x.pth", "not-a-model"]
    )
    def test_unregistered_value_rejected(self, value: str) -> None:
        from pointcloud_detect.worker.processor import _build_pipeline_config

        with pytest.raises(ValueError, match="not a registered model"):
            _build_pipeline_config({"custom_weights_version": value}, "/tmp")

    def test_pretrained_id_keeps_published_weights(self) -> None:
        from pointcloud_detect.worker.processor import _build_pipeline_config

        cfg = _build_pipeline_config(
            {"custom_weights_version": "pointpillars_kitti"}, "/tmp"
        )
        assert cfg.model.detection.weights == "pretrained"
        assert cfg.model.detection.config == "pointpillars_kitti"


class TestE57Capacity:
    """T12: E57 buffers were sized from the header's declared point count."""

    def test_clamped_to_file_size(self) -> None:
        from pointcloud_detect.io_readers import e57_buffer_capacity

        assert e57_buffer_capacity(99_000_000, None, 4096) == 4096

    def test_honest_file_unaffected(self) -> None:
        from pointcloud_detect.io_readers import e57_buffer_capacity

        assert e57_buffer_capacity(1_000, None, 30_000) == 1_000
        assert e57_buffer_capacity(1_000, 500, 30_000) == 500
