"""FastAPI application for Point Cloud Detection."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pointcloud_detect.api.deps import get_settings
from pointcloud_detect.api.hosts import HostAllowlistMiddleware
from pointcloud_detect.api.identity import current_principal, require_access_settings
from pointcloud_detect.api.middleware import BodySizeLimitMiddleware, NoStoreMiddleware
from pointcloud_detect.api.routes import (
    annotations,
    jobs,
    labels,
    models,
    results,
    training,
    training_sessions,
    upload,
)
from pointcloud_detect.api.store import configured_backend


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail at startup, naming what is missing, rather than on the first request that
    # needs a bucket (api/deps.py). This is the AWS-mode app; the local filesystem
    # backend is a separate app and needs no buckets.
    get_settings().require_buckets()
    require_access_settings(get_settings(), configured_backend())
    yield


# /docs, /redoc and /openapi.json are served only with API_DOCS_ENABLED=true.
_docs = get_settings().api_docs_enabled

app = FastAPI(
    title="Point Cloud Detection API",
    version="0.1.0",
    description="REST API for 3D point cloud object detection and segmentation",
    # FastAPI's default since 0.132, set explicitly because the CSRF controls depend on
    # it: a body without a JSON Content-Type is never read as JSON, and a cross-site
    # page can send one only after a preflight.
    strict_content_type=True,
    lifespan=_lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

# Users do not authenticate to this application. On EKS every request reaches the API
# through one of two gates, both outside it:
#
#   public mode        the ALB runs Cognito sign-in on every listener rule and only
#                      forwards /api requests that carry CLIENT_HEADER (the CSRF
#                      control for its session cookie);
#   port-forward mode  the caller's IAM identity, EKS access entry and a namespaced
#                      RBAC Role.
#
# See infrastructure/helm/frontend/templates/ingress.yaml. What a caller may reach is
# decided here (threat model T31): with ACCESS_SCOPE=per-user, every /api router runs
# current_principal first, which verifies the ALB's signed x-amzn-oidc-data and its
# signer (api/identity.py), and each user reaches only what they created. The unsigned
# x-amzn-oidc-* headers are never read: anything the NetworkPolicy admits can reach
# this process directly and set them itself.
CLIENT_HEADER = "X-PCD-Client"


def _cors_origins() -> list[str]:
    """Allowed cross-origin callers, from CORS_ALLOWED_ORIGINS.

    This was a wildcard with allow_credentials=True. Browsers refuse that exact
    combination, so it never did what it appeared to, and "restrict in production"
    was a comment rather than a setting. The shipped deployment is same-origin and
    needs no CORS at all, so the default is none.
    """
    origins = get_settings().cors_origins
    if "*" in origins:
        raise ValueError(
            "CORS_ALLOWED_ORIGINS must list explicit origins; '*' is not accepted."
        )
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", CLIENT_HEADER],
)
# Outside CORS and the routes, so an oversized body is refused before anything reads
# it (T44). The cap is MAX_REQUEST_BODY_BYTES.
app.add_middleware(
    BodySizeLimitMiddleware, max_body_bytes=get_settings().max_request_body_bytes
)
# Outside the body cap, so its 413s are marked too.
app.add_middleware(NoStoreMiddleware)
# Added last, so it runs first: a request for a foreign Host, as from a DNS-rebinding
# page on the api tunnel, is refused before anything else sees it.
app.add_middleware(
    HostAllowlistMiddleware, allowed_hosts=get_settings().allowed_host_names
)

# Every /api router runs current_principal, here rather than route by route, so a new
# route cannot leave it out (tests/test_access_scope.py walks every route to check).
# Routes that need the caller declare it too; FastAPI resolves it once per request.
_API_ROUTERS = [
    (upload.router, "/api/upload", "upload"),
    (jobs.router, "/api/jobs", "jobs"),
    (results.router, "/api/results", "results"),
    (models.router, "/api/models", "models"),
    (training_sessions.router, "/api/training-sessions", "training-sessions"),
    (training.router, "/api/training", "training"),
    (labels.router, "/api/labels", "labels"),
    (annotations.router, "/api/annotations", "annotations"),
]
for _router, _prefix, _tag in _API_ROUTERS:
    app.include_router(
        _router,
        prefix=_prefix,
        tags=[_tag],
        dependencies=[Depends(current_principal)],
    )


@app.get("/health")
async def health():
    """Health check for readiness/liveness probes."""
    return {"status": "ok"}
