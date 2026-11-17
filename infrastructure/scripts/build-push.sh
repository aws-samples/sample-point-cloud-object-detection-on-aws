#!/bin/bash
set -euo pipefail

# Build and push Docker images to ECR, tagged with the commit they were built from.
#
# Usage:
#   ./build-push.sh [--variant gpu|cpu|stub] <region> <backend-ecr-url> <frontend-ecr-url>
#   ./build-push.sh [--variant ...] --check   resolve and print the tag only; build nothing
#
# stdout carries exactly one line, IMAGE_TAG=<tag>, for deploy.sh to pass to the
# charts. Everything else, docker's output included, goes to stderr.
#
# VARIANTS. The three ways a customer runs this solution, each a different backend
# image built from a different Dockerfile:
#
#   gpu   (default)  Dockerfile.gpu: CUDA + PyTorch + mmdet3d + MinkowskiEngine. Real
#                    detection on GPU nodes, every model including FCAF3D. This is the
#                    default because it is what a dev/prod deployment runs; the ~9GB
#                    image needs a build host that can compile the CUDA extension
#                    (FORCE_CUDA is set in the Dockerfile, so a GPU is not required to
#                    build, but the build is long).
#   cpu              Dockerfile.cpu: the gpu image with mmcv's CUDA ops patched for
#                    CPU. Real detection, ~30-60x slower, no GPU node needed. It is
#                    built FROM the gpu image, so a gpu build (this commit's) must
#                    exist first; this script builds it if it is not already in ECR.
#                    FCAF3D still needs MinkowskiEngine's CUDA kernels, so CPU runs the
#                    other models (VoteNet, PointPillars, CenterPoint, PointNet++).
#   stub             Dockerfile: no mmdet3d, so detection falls back to the synthetic
#                    stub backend (placeholder boxes). For testing the pipeline, UI and
#                    infrastructure without the ML stack. Small and quick, and the only
#                    variant CI can build (its runners have no GPU and cannot compile
#                    the CUDA extension).
#
# TAGS. The frontend image does not change between variants, so it keeps the bare
# 12-char commit SHA. The backend tag carries the variant so the three never collide
# in the IMMUTABLE repository, using the suffix scheme the README's manual build
# documents: <sha>-gpu, <sha>-cpu, <sha>-stub. (A bare <sha> was the stub image under
# the old single-Dockerfile build; variant suffixes make the mode explicit for all
# three.) IMAGE_TAG on stdout is the backend tag for the chosen variant, which is what
# deploy.sh passes to the chart.
#
# The repositories are IMMUTABLE (modules/ecr), so a tag names one image for good and a
# second push under it is rejected (THREAT_MODEL.md T29). An image whose tag is already
# in its repository is not built again, so redeploying the same commit pushes nothing.
# `latest` is refused by name throughout.
#
# A tree with uncommitted or untracked files among the images' inputs is refused,
# because the image would not be the commit its tag names. To build one anyway, set
# IMAGE_TAG to a name of your own that has not been pushed before. Only the inputs
# count (IMAGE_INPUTS): an edited terraform.tfvars, which the deploy instructions ask
# for, goes into no image.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Keep stdout for the tag alone.
exec 3>&1 1>&2

# --variant comes before the positionals. Default gpu: the real dev/prod image.
VARIANT="gpu"
if [ "${1:-}" = "--variant" ]; then
  VARIANT="${2:-}"
  shift 2
fi
case "${VARIANT}" in
  gpu | cpu | stub) ;;
  *)
    echo "error: --variant must be gpu, cpu or stub, not '${VARIANT}'." >&2
    exit 1
    ;;
esac

# Everything that reaches any image: the root build context's allowlist (.dockerignore)
# with all three Dockerfiles, the mmcv CPU patch the cpu image applies, the mmengine
# and mmdet3d patches the GPU image applies, and the frontend's whole context. All variants share
# the inputs, so the dirty check is the same whichever one is built.
IMAGE_INPUTS=(Dockerfile Dockerfile.cpu Dockerfile.gpu .dockerignore
  pyproject.toml uv.lock README.md src scripts/patch_mmcv_cpu.py
  scripts/patch_mmengine_metadata.py scripts/patch_mmdet3d_lyft.py frontend)

if [ -n "${IMAGE_TAG:-}" ]; then
  BASE_TAG="${IMAGE_TAG}"
else
  if ! git -C "${PROJECT_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    echo "error: ${PROJECT_ROOT} is not a git checkout, so there is no commit to tag images with. Set IMAGE_TAG." >&2
    exit 1
  fi
  DIRTY="$(git -C "${PROJECT_ROOT}" status --porcelain -- "${IMAGE_INPUTS[@]}")"
  if [ -n "${DIRTY}" ]; then
    cat >&2 <<'MSG'
error: the images' sources have uncommitted or untracked files, so an image built from
them would not be the commit its tag names. Commit or remove them, or set IMAGE_TAG to
a tag of your own that has not been pushed before:
MSG
    printf '%s\n' "${DIRTY}" | head -n 20 | sed 's/^/    /' >&2
    exit 1
  fi
  BASE_TAG="$(git -C "${PROJECT_ROOT}" rev-parse --short=12 HEAD)"
fi

# Docker's tag grammar, checked on BASE_TAG, the part a commit SHA or IMAGE_TAG sets.
# `latest` is refused by name: it is the mutable tag this script exists to replace, and
# IMMUTABLE repositories accept it only once. The checks run on BASE_TAG, not the
# suffixed backend tag, so `IMAGE_TAG=latest` is caught rather than slipping through as
# `latest-gpu`; the variant suffix is a fixed, known-safe string.
TAG_RE='^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
if [ "${BASE_TAG}" = "latest" ] || ! [[ "${BASE_TAG}" =~ ${TAG_RE} ]]; then
  echo "error: '${BASE_TAG}' is not a usable image tag: use letters, digits, '_', '.' and '-' (not 'latest')." >&2
  exit 1
fi

# The frontend keeps the bare SHA; the backend tag carries the variant as a suffix.
BACKEND_TAG="${BASE_TAG}-${VARIANT}"
FRONTEND_TAG="${BASE_TAG}"

if [ "${1:-}" = "--check" ]; then
  echo "IMAGE_TAG=${BACKEND_TAG}" >&3
  exit 0
fi

REGION="${1:?Usage: build-push.sh [--variant gpu|cpu|stub] <region> <backend-ecr-url> <frontend-ecr-url>}"
ECR_BACKEND="${2:?Missing backend ECR URL}"
ECR_FRONTEND="${3:?Missing frontend ECR URL}"

# Succeeds if <repo-url>:<tag> is already in ECR, fails if it is not, and stops the
# script on any other error: a denied or throttled lookup must not read as "absent".
image_exists() {
  local url="$1" tag="$2" out
  if out="$(aws ecr describe-images --region "${REGION}" \
      --registry-id "${url%%.*}" --repository-name "${url#*/}" \
      --image-ids "imageTag=${tag}" --query 'imageDetails[0].imageDigest' \
      --output text 2>&1)"; then
    return 0
  fi
  case "${out}" in
    *ImageNotFoundException*) return 1 ;;
  esac
  echo "error: could not look up ${url}:${tag} in ECR:" >&2
  echo "${out}" >&2
  exit 1
}

# The immutable digest of <repo-url>:<tag>, for pinning one image as another's base.
image_digest() {
  local url="$1" tag="$2"
  aws ecr describe-images --region "${REGION}" \
    --registry-id "${url%%.*}" --repository-name "${url#*/}" \
    --image-ids "imageTag=${tag}" --query 'imageDetails[0].imageDigest' --output text
}

LOGGED_IN=""
ecr_login() {
  if [ -z "${LOGGED_IN}" ]; then
    echo ">>> Logging in to ECR..."
    aws ecr get-login-password --region "${REGION}" | \
      docker login --username AWS --password-stdin "${ECR_BACKEND%%/*}"
    LOGGED_IN=1
  fi
}

# <label> <repo-url> <tag> <dockerfile> <build context> [extra docker build args...]
build_push() {
  local label="$1" url="$2" tag="$3" dockerfile="$4" context="$5"
  shift 5
  if image_exists "${url}" "${tag}"; then
    echo ">>> ${label}: ${url}:${tag} is already in ECR; not rebuilding."
    echo "    Tags there are immutable, so this stays the image first pushed under it."
    echo "    To ship changes, commit them (or set a new IMAGE_TAG)."
    return 0
  fi
  ecr_login
  echo ">>> Building ${label} image..."
  docker build -t "${url}:${tag}" -f "${dockerfile}" "$@" "${context}"
  echo ">>> Pushing ${label} image..."
  docker push "${url}:${tag}"
}

case "${VARIANT}" in
  gpu)
    build_push "backend (gpu)" "${ECR_BACKEND}" "${BACKEND_TAG}" \
      "${PROJECT_ROOT}/Dockerfile.gpu" "${PROJECT_ROOT}"
    ;;
  stub)
    build_push "backend (stub)" "${ECR_BACKEND}" "${BACKEND_TAG}" \
      "${PROJECT_ROOT}/Dockerfile" "${PROJECT_ROOT}"
    ;;
  cpu)
    # The cpu image is FROM the gpu image of the same commit, pinned by digest. Build
    # the gpu image first if this commit's is not already pushed, so a cpu build is
    # always possible from a clean checkout without a separate manual step.
    gpu_tag="${BASE_TAG}-gpu"
    if ! image_exists "${ECR_BACKEND}" "${gpu_tag}"; then
      echo ">>> cpu needs the gpu image of this commit as its base; building it first."
      build_push "backend (gpu)" "${ECR_BACKEND}" "${gpu_tag}" \
        "${PROJECT_ROOT}/Dockerfile.gpu" "${PROJECT_ROOT}"
    fi
    ecr_login  # image_digest reads ECR; ensure we are authenticated
    gpu_digest="$(image_digest "${ECR_BACKEND}" "${gpu_tag}")"
    base_ref="${ECR_BACKEND}@${gpu_digest}"
    echo ">>> cpu base image: ${base_ref}"
    build_push "backend (cpu)" "${ECR_BACKEND}" "${BACKEND_TAG}" \
      "${PROJECT_ROOT}/Dockerfile.cpu" "${PROJECT_ROOT}" \
      --build-arg "BASE_IMAGE=${base_ref}"
    ;;
esac

build_push frontend "${ECR_FRONTEND}" "${FRONTEND_TAG}" \
  "${PROJECT_ROOT}/frontend/Dockerfile" "${PROJECT_ROOT}/frontend"

echo ">>> Done. Images:"
echo "  Backend (${VARIANT}):  ${ECR_BACKEND}:${BACKEND_TAG}"
echo "  Frontend:            ${ECR_FRONTEND}:${FRONTEND_TAG}"
echo "IMAGE_TAG=${BACKEND_TAG}" >&3
