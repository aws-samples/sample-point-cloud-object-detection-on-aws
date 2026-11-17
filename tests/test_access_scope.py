"""Tests for ACCESS_SCOPE and the principal every /api route runs (threat model T31).

Startup refuses a missing or unusable scope; in per-user mode a request reaches a
handler only with a verified identity, and every /api route is covered.
"""

import logging
import re
import uuid

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi import routing as fastapi_routing
from fastapi.testclient import TestClient

from pointcloud_detect.api import identity
from pointcloud_detect.api.app import CLIENT_HEADER, app
from pointcloud_detect.api.deps import Settings, get_settings
from pointcloud_detect.api.identity import (
    ALB_CLAIMS_HEADER,
    AlbKeyCache,
    current_principal,
    require_access_settings,
)
from tests._identity import (
    ALB_ARN,
    ALICE,
    CLIENT_ID,
    POOL_ID,
    alb_token,
    per_user,
    signed_in,
)


@pytest.fixture
def per_user_mode(monkeypatch: pytest.MonkeyPatch):
    keys = per_user(monkeypatch)
    yield keys
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers={CLIENT_HEADER: "spa"})


def _settings(**values: str) -> Settings:
    base = {
        "aws_region": "us-east-1",
        "access_scope": "per-user",
        "alb_arn": ALB_ARN,
        "cognito_user_pool_id": POOL_ID,
        "cognito_client_id": CLIENT_ID,
    }
    base.update(values)
    return Settings(**base)  # pyright: ignore[reportArgumentType]


class TestStartup:
    @pytest.mark.parametrize("scope", ["", "everyone", "PER-USER", "per_user"])
    def test_scope_must_be_named(self, scope: str) -> None:
        with pytest.raises(RuntimeError, match="ACCESS_SCOPE"):
            require_access_settings(_settings(access_scope=scope), "dynamodb")

    def test_shared_needs_nothing_else(self) -> None:
        require_access_settings(
            Settings(access_scope="shared"),  # pyright: ignore[reportCallIssue]
            "memory",
        )

    def test_per_user_needs_the_shared_store(self) -> None:
        with pytest.raises(RuntimeError, match="JOB_STORE_BACKEND=dynamodb"):
            require_access_settings(_settings(), "memory")

    @pytest.mark.parametrize(
        ("field", "env"),
        [
            ("cognito_user_pool_id", "COGNITO_USER_POOL_ID"),
            ("cognito_client_id", "COGNITO_CLIENT_ID"),
        ],
    )
    def test_per_user_needs_the_user_pool(self, field: str, env: str) -> None:
        with pytest.raises(RuntimeError, match=env):
            require_access_settings(_settings(**{field: ""}), "dynamodb")

    @pytest.mark.parametrize(
        ("field", "value", "env"),
        [
            ("alb_arn", "not-an-arn", "ALB_ARN"),
            ("cognito_user_pool_id", "pool", "COGNITO_USER_POOL_ID"),
            ("cognito_client_id", "a client", "COGNITO_CLIENT_ID"),
        ],
    )
    def test_per_user_settings_must_be_valid(
        self, field: str, value: str, env: str
    ) -> None:
        with pytest.raises(RuntimeError, match=env):
            require_access_settings(_settings(**{field: value}), "dynamodb")

    def test_the_alb_must_be_in_the_apis_region(self) -> None:
        settings = _settings()
        settings.aws_region = "eu-west-1"
        with pytest.raises(RuntimeError, match="AWS_REGION"):
            require_access_settings(settings, "dynamodb")

    def test_an_unset_alb_arn_starts_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ALB exists only after the first deploy; the API waits, answering 503."""
        with caplog.at_level(logging.WARNING, logger=identity.__name__):
            require_access_settings(_settings(alb_arn=""), "dynamodb")
        assert "ALB_ARN is unset" in caplog.text

    def test_the_app_checks_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("S3_INPUT_BUCKET", "S3_OUTPUT_BUCKET", "S3_TILES_BUCKET"):
            monkeypatch.setenv(env, "test")
        monkeypatch.setenv("S3_MODELS_BUCKET", "test")
        monkeypatch.delenv("ACCESS_SCOPE")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="ACCESS_SCOPE"):
                with TestClient(app):
                    pass
        finally:
            get_settings.cache_clear()


class TestSharedMode:
    def test_served_without_an_identity(self, client: TestClient) -> None:
        assert client.get("/api/jobs").status_code == 200

    def test_the_claims_header_is_not_read(self, client: TestClient) -> None:
        resp = client.get("/api/jobs", headers={ALB_CLAIMS_HEADER: "garbage"})
        assert resp.status_code == 200


class TestPerUserMode:
    def test_a_verified_identity_is_served(self, per_user_mode, client) -> None:
        assert client.get("/api/jobs", headers=signed_in(ALICE)).status_code == 200

    def test_no_identity(self, per_user_mode, client: TestClient) -> None:
        assert client.get("/api/jobs").status_code == 401

    @pytest.mark.parametrize(
        "header",
        [
            "x-amzn-oidc-identity",
            "x-amzn-oidc-accesstoken",
        ],
    )
    def test_the_unsigned_headers_are_not_an_identity(
        self, per_user_mode, client: TestClient, header: str
    ) -> None:
        assert client.get("/api/jobs", headers={header: ALICE}).status_code == 401

    def test_two_claims_headers(self, per_user_mode, client: TestClient) -> None:
        token = alb_token()
        resp = client.get(
            "/api/jobs",
            headers=[(ALB_CLAIMS_HEADER, token), (ALB_CLAIMS_HEADER, token)],
        )
        assert resp.status_code == 401

    def test_claims_that_do_not_verify(self, per_user_mode, client) -> None:
        token = alb_token(header={"client": "another"})
        resp = client.get("/api/jobs", headers={ALB_CLAIMS_HEADER: token})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Sign-in required."}

    def test_no_alb_arn_yet(self, monkeypatch: pytest.MonkeyPatch, client) -> None:
        per_user(monkeypatch, alb_arn="")
        try:
            resp = client.get("/api/jobs", headers=signed_in(ALICE))
        finally:
            get_settings.cache_clear()
        assert resp.status_code == 503

    def test_an_alb_in_another_region(
        self, monkeypatch: pytest.MonkeyPatch, client
    ) -> None:
        per_user(monkeypatch)
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        get_settings.cache_clear()
        try:
            resp = client.get("/api/jobs", headers=signed_in(ALICE))
        finally:
            get_settings.cache_clear()
        assert resp.status_code == 503

    def test_no_signing_key(self, monkeypatch: pytest.MonkeyPatch, client) -> None:
        def unreachable(url: str) -> bytes:
            raise OSError("unreachable")

        keys = AlbKeyCache("us-east-1", fetch=unreachable)
        monkeypatch.setattr(identity, "key_cache_for", lambda region: keys)
        per_user(monkeypatch, keys=keys)  # pyright: ignore[reportArgumentType]
        try:
            resp = client.get("/api/jobs", headers=signed_in(ALICE))
        finally:
            get_settings.cache_clear()
        assert resp.status_code == 503

    def test_health_needs_no_identity(self, per_user_mode, client) -> None:
        """The kubelet and the ALB's health check probe it by pod IP."""
        assert client.get("/health").status_code == 200


def _api_routes(application: FastAPI) -> list:
    """Every route the app serves under /api, with the dependencies it runs.

    FastAPI 0.13x keeps included routers as their own objects; iter_route_contexts
    yields each route with the router's dependencies applied, as a request sees it.
    """
    walk = getattr(fastapi_routing, "iter_route_contexts", None)
    routes = walk(application.routes) if walk else application.routes
    return [
        r
        for r in routes
        if (getattr(r, "path", None) or "").startswith("/api")
        and getattr(r, "dependant", None) is not None
    ]


def _runs_principal(route) -> bool:
    pending = list(route.dependant.dependencies)
    while pending:
        dep = pending.pop()
        if dep.call is current_principal:
            return True
        pending.extend(dep.dependencies)
    return False


def _api_routes_without_principal(application: FastAPI) -> list[str]:
    return [
        f"{sorted(r.methods)} {r.path}"
        for r in _api_routes(application)
        if not _runs_principal(r)
    ]


def _sample_path(path: str) -> str:
    """``path`` with each parameter replaced by a value its pattern accepts."""
    return re.sub(
        r"\{(\w+)\}",
        lambda m: "tile_0" if m.group(1) == "tile_id" else str(uuid.uuid4()),
        path,
    )


class TestCoverage:
    """Every /api route runs current_principal, whoever adds the next route."""

    def test_every_api_route_runs_it(self) -> None:
        assert len(_api_routes(app)) >= 25
        assert _api_routes_without_principal(app) == []

    def test_every_api_route_refuses_an_anonymous_call(
        self, per_user_mode, client: TestClient
    ) -> None:
        """The same, observed: anonymous calls with a well-formed body all get 401.

        (A malformed JSON body gets 422, because FastAPI parses the body before it
        resolves dependencies; no handler runs either way, and the body is capped.)
        """
        answered = {}
        for route in _api_routes(app):
            for method in route.methods:
                url = _sample_path(route.path)
                resp = client.request(method, url, json={})
                answered[f"{method} {route.path}"] = resp.status_code
        assert answered and set(answered.values()) == {401}, answered

    def test_the_check_finds_a_router_without_it(self) -> None:
        router = APIRouter()

        @router.get("/x")
        def x() -> dict:
            return {}

        covered = APIRouter()

        @covered.get("/y")
        def y() -> dict:
            return {}

        probe = FastAPI()
        probe.include_router(router, prefix="/api/uncovered")
        probe.include_router(
            covered, prefix="/api/covered", dependencies=[Depends(current_principal)]
        )
        assert _api_routes_without_principal(probe) == ["['GET'] /api/uncovered/x"]
