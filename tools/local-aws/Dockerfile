# Local S3 and SQS for docker-compose.yml (moto). Built here from the same pinned base
# as the backend, with moto and its dependencies locked in uv.lock, rather than pulled
# as a prebuilt emulator image whose contents this repository does not control.
#
# checkov:skip=CKV_DOCKER_2: No HEALTHCHECK. docker-compose.yml is the only consumer.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:8873a7cbc7c3d51d55a749c7c4c7894b7981f0514c269003c7952cf3ed550d27

# Without python3.12's ensurepip wheel, as in the backend Dockerfile.
RUN dnf install -y --setopt=install_weak_deps=0 --nodocs python3.12 shadow-utils \
    && dnf clean all && rm -rf /var/cache/dnf /var/cache/libdnf5 /usr/share/python3.12-wheels

# uv, pinned by version and digest, as in the backend Dockerfile.
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/local-aws

WORKDIR /srv
COPY pyproject.toml uv.lock ./
# moto ships its proxy mode's CA and certificate private keys, and an EC2 model with
# a hard-coded example access key id; secret scanners flag all three. Neither the
# proxy nor EC2 is used here, so both modules are dropped.
RUN uv sync --frozen --no-install-project \
    && rm -rf /opt/local-aws/lib/python3.12/site-packages/moto/moto_proxy \
              /opt/local-aws/lib/python3.12/site-packages/moto/ec2 \
    && chmod -R a+rX,go-w /opt/local-aws
COPY server.py ./

# The account in the queue URLs and ARNs docker-compose.yml configures.
ENV PATH="/opt/local-aws/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    MOTO_ACCOUNT_ID=000000000000

RUN useradd -r -u 999 -s /usr/sbin/nologin moto
USER 999
EXPOSE 4566
CMD ["python", "/srv/server.py"]
