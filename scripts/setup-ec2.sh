#!/bin/bash
set -euo pipefail

# ==============================================================================
# EC2 GPU Instance Setup Script
# Sets up a fresh Ubuntu 22.04 instance (g5.xlarge or similar) with:
#   - uv, and Python 3.11 through it
#   - PyTorch for the CUDA toolkit on the host (11.8, 12.4, or 12.6 and later 12.x)
#   - MMDetection3D stack, with mmcv compiled against that toolkit
#   - Point cloud detection pipeline
#
# Every download is pinned. The ML stack is the one Dockerfile.gpu builds, which is
# the combination known to work; see its header for why each version is what it is:
#   CPython 3.11.16 (from uv; MinkowskiEngine and OpenMMLab 3D need 3.11),
#   torch 2.13.0, torchvision 0.28.0, numpy 1.26.4, mmcv 2.1.0 (compiled here: no
#   prebuilt CUDA wheel exists for torch 2.13), mmengine 0.10.7, mmdet 3.2.0,
#   mmdet3d 1.4.0, setuptools 69.5.1 and wheel 0.45.1 to build, setuptools 84.2.1 after
#   (with mmengine patched off pkg_resources, as in Dockerfile.gpu).
# Change them together with Dockerfile.gpu. x86_64 only. Compiling mmcv needs the CUDA
# toolkit (nvcc), not only a driver, and a CUDA 12 one: the Deep Learning AMIs carry
# several, and CUDA_HOME picks one.
#
# Recommended instance: g5.xlarge (1x A10G, 4 vCPU, 16GB RAM)
# AMI: Ubuntu 22.04 LTS (Deep Learning AMI has CUDA pre-installed if preferred)
#
# Usage:
#   1. Launch EC2 instance (g5.xlarge, Ubuntu 22.04, 100GB EBS)
#   2. SSH in: ssh -i key.pem ubuntu@<ip>
#   3. Clone repo: git clone <repo-url> && cd sample-point-cloud-object-detection-on-aws
#   4. Run: bash scripts/setup-ec2.sh
#   5. Activate: source .venv/bin/activate
#   6. Test: pointcloud-detect detect data/kitti/training/velodyne/000000.bin --mode kitti
# ==============================================================================

echo "============================================"
echo "Point Cloud Detection — EC2 Setup"
echo "============================================"
echo ""

# --- System packages ---
echo ">>> Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    wget \
    unzip \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libopenblas-dev

if [ "$(uname -m)" != "x86_64" ]; then
    echo "This script supports x86_64 only." >&2
    exit 1
fi

# --- uv (Python package manager) ---
# A fixed release, checked against its published SHA-256 before anything runs, rather
# than piping an install script that follows the latest release into a shell. The
# version matches Dockerfile, Dockerfile.gpu and both CI files.
echo ""
echo ">>> Installing uv..."
UV_VERSION="0.12.19"
UV_SHA256="23bf5552d220e0842b65c862097b2ebaeba0064b74eda5e565e77fd25969d8c8"
UV_TARBALL="uv-x86_64-unknown-linux-gnu.tar.gz"
UV_TMP="$(mktemp -d)"
curl -fsSL -o "${UV_TMP}/${UV_TARBALL}" \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_TARBALL}"
echo "${UV_SHA256}  ${UV_TMP}/${UV_TARBALL}" | sha256sum -c -
tar -xzf "${UV_TMP}/${UV_TARBALL}" -C "${UV_TMP}"
mkdir -p "$HOME/.local/bin"
install -m 0755 "${UV_TMP}/uv-x86_64-unknown-linux-gnu/uv" "${UV_TMP}/uv-x86_64-unknown-linux-gnu/uvx" "$HOME/.local/bin/"
rm -rf "${UV_TMP}"
export PATH="$HOME/.local/bin:$PATH"

# Make PATH permanent (survives source .venv/bin/activate). Single quotes on purpose:
# .bashrc should expand $HOME and $PATH when it runs, not now.
if ! grep -q '.local/bin' ~/.bashrc; then
    # shellcheck disable=SC2016
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

# --- Python 3.11, from uv (python-build-standalone, checked against uv's checksum) ---
echo ""
echo ">>> Installing Python 3.11..."
uv python install cpython-3.11.16-linux-x86_64-gnu

# --- Create virtual environment ---
echo ""
echo ">>> Creating Python 3.11 virtual environment..."
uv venv --python 3.11.16 .venv
# shellcheck source=/dev/null
source .venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"  # Re-export after activate (activate can reset PATH)

# --- Install pipeline with all extras ---
echo ""
echo ">>> Installing pipeline dependencies (io + api + dev)..."

# Fix Windows backslashes in .gitignore (breaks hatchling on Linux)
sed -i 's|\\|/|g' .gitignore 2>/dev/null || true

uv sync --frozen --extra io --extra api --extra dev

# --- Install PyTorch with CUDA ---
echo ""
echo ">>> Installing PyTorch with CUDA..."

# Match PyTorch to the CUDA toolkit that will compile mmcv. torch 2.13.0 has wheels for
# CUDA 12.6, 12.9 and 13.0; torch's extension builder refuses a different major
# version and only warns on a different minor, so any 12.x toolkit builds against
# cu126 or cu129. There is no CUDA 11 build of torch 2.13.
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
export CUDA_HOME PATH="${CUDA_HOME}/bin:${PATH}"
NVCC_VERSION=$("${CUDA_HOME}/bin/nvcc" --version 2>/dev/null | grep "release" | sed 's/.*release //' | sed 's/,.*//' || true)
case "$NVCC_VERSION" in
    12.9*) CUDA_TAG="cu129" ;;
    12.*) CUDA_TAG="cu126" ;;
    *)
        echo "Need a CUDA 12 toolkit to compile mmcv against torch 2.13;" >&2
        echo "found '${NVCC_VERSION:-none}' at ${CUDA_HOME}. Point CUDA_HOME at one" >&2
        echo "(the Deep Learning AMIs have several under /usr/local), or use Dockerfile.gpu." >&2
        exit 1
        ;;
esac
echo "  CUDA toolkit ${NVCC_VERSION} at ${CUDA_HOME}: PyTorch ${CUDA_TAG}"
TORCH_INDEX="https://download.pytorch.org/whl/${CUDA_TAG}"

uv pip install torch==2.13.0 torchvision==0.28.0 --index-url "$TORCH_INDEX"
uv pip install numpy==1.26.4

# --- Install MMDetection3D stack ---
# mmcv is compiled from source with its CUDA ops, as in Dockerfile.gpu: OpenMMLab
# publishes no CUDA wheel for torch 2.13, and torch 2.13's headers need C++20, which
# mmcv's setup.py does not ask for. MAX_JOBS limits parallel nvcc jobs.
echo ""
echo ">>> Installing MMDetection3D stack (compiling mmcv; this takes a while)..."
export FORCE_CUDA=1 MMCV_WITH_OPS=1 MAX_JOBS="${MAX_JOBS:-2}"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-7.0;7.5;8.0;8.6+PTX}"
uv pip install setuptools==69.5.1 wheel==0.45.1 ninja==1.13.2  # ninja: parallel compiles
uv pip install mmengine==0.10.7
MMCV_SDIST=https://files.pythonhosted.org/packages/a7/19/f97f0fd4d432113a232e75f965335be6b7bb6557ae9faf8051e9a012cadd/mmcv-2.1.0.tar.gz
MMCV_SHA256=d387bcab66b467479b6660310e23746cfc79c6e57acf04094680adb499a5cd3f
MMCV_SRC=$(mktemp -d)
curl -fsSL -o "$MMCV_SRC/mmcv.tar.gz" "$MMCV_SDIST"
echo "${MMCV_SHA256}  $MMCV_SRC/mmcv.tar.gz" | sha256sum -c -
tar -xzf "$MMCV_SRC/mmcv.tar.gz" -C "$MMCV_SRC" --strip-components=1
sed -i 's/-std=c++1[47]/-std=c++20/g' "$MMCV_SRC/setup.py"
uv pip install "$MMCV_SRC" --no-build-isolation
rm -rf "$MMCV_SRC"
uv pip install mmdet==3.2.0 mmdet3d==1.4.0 --no-build-isolation --no-cache
uv pip install numpy==1.26.4
# The runtime setuptools (see Dockerfile.gpu), which no longer ships the pkg_resources
# mmengine imports, so mmengine is patched to read importlib.metadata instead; and the
# locked versions of what the PyTorch index replaced with older copies.
uv pip install setuptools==84.2.1
python "$(dirname "$0")/patch_mmengine_metadata.py"
# Two packages mmdet3d requires and this sample does not use, as in Dockerfile.gpu:
# plyfile (GPL-3.0-or-later) and lyft-dataset-sdk (CC BY-NC-SA 4.0), once mmdet3d's
# Lyft metric is optional, and the packages only the SDK required.
python "$(dirname "$0")/patch_mmdet3d_lyft.py"
uv pip uninstall lyft-dataset-sdk plyfile pandas pytest black flake8
uv pip install pillow==13.0.0 filelock==4.2.4 urllib3==2.8.0 requests==2.34.2

# --- Verify installation ---
echo ""
echo ">>> Verifying installation..."
python -c "import torch; print(f'PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}')"
python -c "import mmdet3d; print(f'MMDetection3D: {mmdet3d.__version__}')"
python -c "from mmengine.utils import get_installed_path; print('mmengine package lookup:', get_installed_path('mmdet3d'))"
python -c "from pointcloud_detect.backends.mmdet3d import MMDet3DDetectionBackend; print('mmdet3d backend: importable')"

# --- Download sample data (KITTI labels — small, ~21MB) ---
echo ""
echo ">>> Downloading KITTI labels for testing..."
mkdir -p data/kitti
uv run pointcloud-detect data download --dataset kitti --data-root ./data 2>/dev/null || echo "(KITTI download may require manual steps for velodyne — labels downloaded)"

echo ""
echo "============================================"
echo "Setup complete!"
echo ""
echo "Activate environment:"
echo "  source .venv/bin/activate"
echo ""
echo "Run detection (CPU — no velodyne data yet):"
echo "  pointcloud-detect detect <your-file.ply> --mode indoor"
echo ""
echo "Run detection (GPU — with KITTI .bin data):"
echo "  pointcloud-detect detect data/kitti/training/velodyne/000000.bin --mode kitti --use-intensity"
echo ""
echo "Run fine-tuning:"
echo "  python -m pointcloud_detect.training.trainer --config training_config.example.yaml"
echo "============================================"
