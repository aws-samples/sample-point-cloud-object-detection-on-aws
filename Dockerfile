# Multi-stage Dockerfile for Point Cloud Detection Backend
# Builds from pyproject.toml — same dependencies as local `uv sync`
#
# Every external input is pinned by digest, so a rebuild of the same commit gets the
# same bytes. The base is Amazon Linux 2023 minimal from ECR Public: AWS-maintained,
# with security fixes published through ALAS, and Python 3.12 from its own
# repository. Dependabot (docker ecosystem) refreshes the digest when the tag moves.
#
# checkov:skip=CKV_DOCKER_2: No HEALTHCHECK. Kubernetes ignores it; the Helm chart's
# liveness and readiness probes do this job, and the worker has no endpoint to probe.

FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4 AS base

WORKDIR /app

# Python 3.12, and the system libraries the point cloud I/O libraries load.
# shadow-utils provides useradd for the non-root user below. The pip wheel that
# python3.12 ships for ensurepip is removed: uv builds the venv without it, and its
# pip 23.2.1 carries advisories scanners report.
RUN dnf install -y --setopt=install_weak_deps=0 --nodocs \
    python3.12 mesa-libGL glib2 libSM libXext libXrender shadow-utils \
    && dnf clean all && rm -rf /var/cache/dnf /var/cache/libdnf5 /usr/share/python3.12-wheels

# uv, pinned by version and digest. Keep the version in step with Dockerfile.gpu and
# the `version:` of setup-uv in .github/workflows/ci.yml and `uv==` in .gitlab-ci.yml.
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /usr/local/bin/uv

# Build-time uv settings. The system Python is the interpreter (never a downloaded one,
# which would land under /root where the app user cannot read it), bytecode is compiled
# now because nothing can write it at runtime, and no download cache is left in a layer.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

# Install dependencies (cached layer — only rebuilds when deps change). The project
# itself is installed after src/ is copied, as an editable install, so compose can
# bind-mount ./src over /app/src for hot reload.
#
# /app stays owned by root and world-readable (0755 dirs, 0644 files): the app user
# can read and run it but not change it, and the Helm chart's parser container reads
# it as a different uid. The chmod normalises modes that COPY brings in from a build
# host with a restrictive umask; it runs in the same layer as the install so it does
# not duplicate the venv.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra io --extra api --no-install-project \
    && chmod -R a+rX,go-w /app

# Copy application source
COPY src/ ./src/
RUN chmod -R a+rX,go-w /app/src \
    && uv sync --frozen --no-dev --extra io --extra api

# Runtime contract with the Helm charts: the venv is at /app/.venv and its entry points
# are called directly, so nothing runs `uv run` (which may sync, and so write). With a
# read-only root filesystem the only writable path is the /tmp the pod mounts.
ENV PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user for security. It owns nothing under /app. /scratch is its disk-backed
# scratch space under docker compose, which mounts a named volume there and so gives
# the volume this directory's owner. On EKS the worker has an emptyDir over it, and the
# parser container (another uid) mounts only /scratch/ipc, so it has to be able to
# pass through this directory: 0755, not 0700.
RUN useradd -m -r -u 999 -s /bin/bash appuser \
    && install -d -o 999 -g 999 -m 0755 /scratch
USER 999

# Default: run API server (worker overrides this in Helm)
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "pointcloud_detect.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
