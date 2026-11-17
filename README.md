# Point cloud object detection on AWS

Sample code for the AWS Blog posts on point cloud object detection with Amazon EKS. It
demonstrates a 3D point cloud object detection and segmentation pipeline that ingests large LiDAR scans (E57, LAS/LAZ, PLY), adaptively tiles the scene, runs detection and segmentation inference, and outputs structured JSON with bounding boxes, labels, and confidence scores.

> **Sample code.** This repository is provided as sample code for non-production
> usage. You should work with your security and legal teams to meet your
> organizational security, regulatory and compliance requirements before deployment.
> Deploying it creates AWS resources, including GPU instances, that incur charges;
> see [Cleanup](docs/workshop/07-cleanup.md) to remove them.

## Features

- **Multi-format input**: E57, LAS, LAZ, PLY, KITTI .bin with auto-detection via magic bytes / extension
- **Adaptive tiling**: density-aware, axis-qualified octree subdivision (8-way for cubic indoor scenes, quadtree-like for thin outdoor scenes) with density-median split placement; handles large multi-gigabyte scenes. (Note: a configurable memory budget is defined but not yet enforced end-to-end — see `docs/diagrams/tiling-pipeline.md`.)
- **3D object detection**: oriented bounding boxes (7-DOF) + axis-aligned bounding boxes per object
- **Semantic segmentation**: per-point class labels for structural elements (walls, floors, ceilings, columns, beams) and objects
- **Models**: VoteNet, FCAF3D, CenterPoint, PointPillars and PointNet++ architectures. Their public pretrained weights are non-commercially licensed and **not downloaded unless you opt in** (`ALLOW_NONCOMMERCIAL_WEIGHTS=1`, see [Pretrained weights and licensing](#pretrained-weights-and-licensing)); the [commercially clean model](#commercially-clean-model) recipe trains one from scratch on CC BY 4.0 data instead
- **Preprocessing controls**: voxel downsampling, colour/intensity/normal channel selection, spatial cropping
- **Filtering**: confidence threshold, min/max size, excluded labels
- **Output normalisation**: optionally scale coordinates to [0,1] based on scene extents
- **GPU + CPU**: auto-detects CUDA; falls back to CPU
- **Extensible backend**: MMDetection3D integration with stub fallback for testing

## Installation

```bash
# Install uv at the version CI and the Dockerfiles use. A pinned release from PyPI,
# rather than piping an install script that follows the latest release into a shell.
python3 -m pip install --user uv==0.12.19
export PATH="$HOME/.local/bin:$PATH"

# Create a Python 3.12 virtual environment
uv venv --python 3.12 .venv
source .venv/bin/activate

# Install the pipeline (runs with stub backend for testing).
# This is the same set CI syncs, so a local run matches the pipeline: `io` carries the
# LAS/E57/PLY readers the test suite needs, `viz` carries matplotlib for scripts/visualize.py.
# --frozen installs exactly what uv.lock records, with its hashes.
uv sync --frozen --extra dev --extra api --extra io --extra viz
```

Requires Python 3.12+ for the pipeline, API and tests.

### Optional: real ML inference (MMDetection3D)

The inference stack is the one `Dockerfile.gpu` and `scripts/setup-ec2.sh` install, at
the same exact versions: torch 2.13.0, torchvision 0.28.0, numpy 1.26.4, mmcv 2.1.0,
mmengine 0.10.7, mmdet 3.2.0 and mmdet3d 1.4.0, on Python 3.11 (MinkowskiEngine and the
OpenMMLab 3D stack do not run on 3.12), so it needs a separate environment. OpenMMLab
publishes no mmcv CUDA wheel for torch 2.13, so mmcv compiles here, which needs a CUDA 12
toolkit (`nvcc`), not only a driver. Use the torch build `cu129` with a 12.9 toolkit and
`cu126` with any other 12.x:

```bash
uv python install cpython-3.11.16
uv venv --python 3.11.16 .venv311
source .venv311/bin/activate

CUDA_TAG=cu126
uv pip install torch==2.13.0 torchvision==0.28.0 --index-url "https://download.pytorch.org/whl/${CUDA_TAG}"
uv pip install numpy==1.26.4 setuptools==69.5.1 wheel==0.45.1
uv pip install mmengine==0.10.7
# Compiles mmcv's CUDA ops against the torch above; MAX_JOBS limits parallel nvcc jobs.
# The sdist is checked against PyPI's SHA-256, and its C++ standard raised to the C++20
# torch 2.13's headers need.
curl -fsSL -o /tmp/mmcv.tar.gz https://files.pythonhosted.org/packages/a7/19/f97f0fd4d432113a232e75f965335be6b7bb6557ae9faf8051e9a012cadd/mmcv-2.1.0.tar.gz
echo "d387bcab66b467479b6660310e23746cfc79c6e57acf04094680adb499a5cd3f  /tmp/mmcv.tar.gz" | sha256sum -c -
mkdir /tmp/mmcv && tar -xzf /tmp/mmcv.tar.gz -C /tmp/mmcv --strip-components=1
sed -i 's/-std=c++1[47]/-std=c++20/g' /tmp/mmcv/setup.py
FORCE_CUDA=1 MMCV_WITH_OPS=1 MAX_JOBS=2 uv pip install /tmp/mmcv --no-build-isolation
uv pip install mmdet==3.2.0 mmdet3d==1.4.0 --no-build-isolation --no-cache
uv pip install numpy==1.26.4   # mmdet3d's resolve can move it; the stack predates numpy 2
uv pip install setuptools==84.2.1   # after the builds, the version uv.lock pins
python scripts/patch_mmengine_metadata.py   # mmengine off pkg_resources, which 84 lacks
python scripts/patch_mmdet3d_lyft.py   # make mmdet3d's Lyft metric optional, then drop
uv pip uninstall lyft-dataset-sdk plyfile pandas pytest black flake8   # the SDK, what only it needs, GPL plyfile

# Install the pipeline without re-resolving the pinned stack
uv pip install -e '.[io]' --no-deps
```

> **Note:** Do not use `openmim` / `mim install` — it depends on `pkg_resources` which is
> unavailable in modern setuptools. Direct `uv pip install` works fine for these packages.
> The `--no-build-isolation` flag is required because `mmcv` uses `pkg_resources` in its
> build script but doesn't declare it as a build dependency. setuptools 69.5.1 is what
> the mmcv and MinkowskiEngine builds were verified with. At runtime mmengine imports
> `pkg_resources`, which setuptools 82 removed, so `scripts/patch_mmengine_metadata.py`
> rewrites those imports with `importlib.metadata` and setuptools can be the locked 84.2.1
> (see the header of `Dockerfile.gpu`). mmdet3d also requires `lyft-dataset-sdk`
> (CC BY-NC-SA 4.0) and `plyfile` (GPL-3.0-or-later), which this sample does not use;
> `scripts/patch_mmdet3d_lyft.py` makes the Lyft metric optional so both can be removed
> (`THIRD-PARTY-LICENSES`).
>
> Pretrained checkpoints are never downloaded unless `ALLOW_NONCOMMERCIAL_WEIGHTS=1` is
> set (see [Pretrained weights and licensing](#pretrained-weights-and-licensing)). When
> it is, they are downloaded on first use and checked against a pinned SHA-256, and
> every checkpoint loads weights-only (`src/pointcloud_detect/backends/checkpoints.py`).

### Optional: MinkowskiEngine (for FCAF3D indoor model)

FCAF3D, the indoor detector this sample prefers over VoteNet, requires MinkowskiEngine.
It compiles against the same CUDA 12 toolkit (`nvcc`) as mmcv, with four Thrust includes
added that CUDA 12 no longer brings in, CUB's NVTX ranges turned off (they clash with
the NVTX header MinkowskiEngine bundles), its CUDA files compiled as C++17, and one
`shared_ptr` assignment rewritten that nvcc rejects with CUDA 12's libcu++, as
`Dockerfile.gpu` does (see its header for each). In the `.venv311`
environment above:

```bash
export CUDA_HOME=/usr/local/cuda-12.6   # the CUDA 12 toolkit mmcv was built with
export PATH="$CUDA_HOME/bin:$PATH"
sudo apt-get install -y libopenblas-dev
export MAX_JOBS=2  # prevent OOM during compilation

# The commit Dockerfile.gpu builds, fetched by SHA and checked, not the default branch
ME_COMMIT=02fc608bea4c0549b0a7b00ca1bf15dee4a0b228
git init -q /tmp/MinkowskiEngine
git -C /tmp/MinkowskiEngine fetch -q --depth 1 https://github.com/NVIDIA/MinkowskiEngine.git "$ME_COMMIT"
git -C /tmp/MinkowskiEngine checkout -q FETCH_HEAD
test "$(git -C /tmp/MinkowskiEngine rev-parse HEAD)" = "$ME_COMMIT"
cd /tmp/MinkowskiEngine
sed -i '0,/^#include.*/s//&\n#include <thrust\/execution_policy.h>/' src/convolution_kernel.cuh src/3rdparty/concurrent_unordered_map.cuh
sed -i '0,/^#include.*/s//&\n#include <thrust\/unique.h>\n#include <thrust\/remove.h>/' src/coordinate_map_gpu.cu
sed -i '0,/^#include.*/s//&\n#include <thrust\/execution_policy.h>\n#include <thrust\/reduce.h>\n#include <thrust\/sort.h>/' src/spmm.cu
sed -i 's/NVCC_FLAGS += \["--expt-relaxed-constexpr", "--expt-extended-lambda"\]/&; NVCC_FLAGS.append("-std=c++17")/' setup.py
sed -i 's/m_map = map_type::create(/m_map = [](auto created) { auto deleter = created.get_deleter(); return std::shared_ptr<map_type>(created.release(), deleter); }(map_type::create(/; s/m_unused_element, m_unused_key, m_hasher, m_equal, m_map_allocator);/m_unused_element, m_unused_key, m_hasher, m_equal, m_map_allocator));/' src/coordinate_map_gpu.cuh
cd -
# Built with the setuptools the image builds it with, then back to the runtime version
uv pip install pip==26.2.1 setuptools==69.5.1
# A wheel installed without dependencies: `setup.py install` would fetch packages itself
(cd /tmp/MinkowskiEngine && NVCC_APPEND_FLAGS=-DNVTX_DISABLE python setup.py bdist_wheel --force_cuda --blas=openblas)
uv pip install --no-deps /tmp/MinkowskiEngine/dist/MinkowskiEngine-*.whl
uv pip install setuptools==84.2.1

# Run with FCAF3D, the indoor detector
pointcloud-detect detect scan.ply --mode indoor --use-color --coord-frame livox
```

## Quick Start

With the stub backend (no MMDetection3D installed) these run end to end and return
placeholder boxes. With real inference, every mode below uses a pretrained checkpoint,
so either opt in with `ALLOW_NONCOMMERCIAL_WEIGHTS=1` (only if your use is permitted,
see [Pretrained weights and licensing](#pretrained-weights-and-licensing)) or pass your
own weights with `--weights`.

```bash
# Detection with a model you trained (see "Commercially clean model")
uv run pointcloud-detect detect scan.e57 \
    --weights models/clean_indoor_craslab/model.pth --architecture fcaf3d_scannet

# Basic detection with default settings (indoor mode, pretrained VoteNet; opt-in)
ALLOW_NONCOMMERCIAL_WEIGHTS=1 uv run pointcloud-detect detect scan.e57

# Outdoor mode with custom confidence threshold
uv run pointcloud-detect detect site.laz --mode outdoor --confidence 0.7

# KITTI velodyne .bin file (KITTI-trained PointPillars model)
uv run pointcloud-detect detect data/kitti/training/velodyne/000000.bin --mode kitti --use-intensity

# Indoor RGB point cloud (Livox, TLS, etc.) with FCAF3D ScanNet model
uv run pointcloud-detect detect scan.ply --mode indoor --use-color --coord-frame livox

# Visualize Livox results
uv run pointcloud-detect visualize scan.ply output/scan_detections.json --coord-frame livox

# Full config file
uv run pointcloud-detect detect scan.ply --config config.yaml --output ./results

# Inspect a point cloud file
uv run pointcloud-detect info scan.e57
```

## CLI Reference

### `pointcloud-detect detect`

Run 3D object detection and segmentation on a point cloud file.

```
pointcloud-detect detect [OPTIONS] INPUT_FILE
```

| Option | Description |
|--------|-------------|
| `--config, -c PATH` | Pipeline config file (YAML or JSON) |
| `--output, -o PATH` | Output directory (overrides config) |
| `--mode, -m TEXT` | Model mode: `indoor`, `outdoor`, `kitti`, or `custom` |
| `--confidence FLOAT` | Confidence threshold (0.0-1.0) |
| `--raw` | Output all detections without confidence filtering |
| `--normalize` | Normalise output coordinates to [0,1] based on scene extents |
| `--downsample FLOAT` | Voxel size for downsampling in metres (0 to disable) |
| `--use-color / --no-color` | Include RGB channels for inference |
| `--use-intensity / --no-intensity` | Include intensity channel for inference |
| `--use-normals / --no-normals` | Compute and include surface normals |
| `--coord-frame TEXT` | Source coordinate frame: `z-up` (default), `y-up`, `colmap`, `ros`, `livox`, or `z-rot±N` / `x-rot±N` |
| `--min-size FLOAT` | Minimum detection size in metres (max bbox dimension) |
| `--max-size FLOAT` | Maximum detection size in metres (max bbox dimension) |
| `--exclude-label TEXT` | Label to exclude from output (repeatable) |
| `--max-points INT` | Maximum points to load (for large files, e.g. 20000000). Auto-capped at 20M for files >100M points. |

### `pointcloud-detect info`

Display point cloud file metadata (format, point count, bounds, available channels).

```
pointcloud-detect info INPUT_FILE
```

### `pointcloud-detect visualize`

Generate an interactive 3D HTML visualization of detection results.

```
pointcloud-detect visualize INPUT_FILE DETECTIONS_JSON [--output DIR]
```

Example:

```bash
# Run detection
uv run pointcloud-detect detect data/kitti/training/velodyne/000000.bin --mode kitti --use-intensity

# Visualize results (generates an interactive HTML file)
uv run pointcloud-detect visualize data/kitti/training/velodyne/000000.bin output/000000_detections.json

# Open output/000000_3d.html in a browser
# Or serve it: python -m http.server 8080 --directory output/
```

The HTML viewer supports:
- Rotate, zoom, and pan with mouse
- Click detection buttons in the sidebar to zoom to each object
- "Reset View" button to return to full scene

## Configuration

The pipeline is configurable via a YAML file. See [config.example.yaml](config.example.yaml) for all options.

Key sections:

```yaml
preprocessing:
  downsample: true
  voxel_size_m: 0.02
  use_color: true
  use_intensity: false
  use_normals: false

model:
  mode: "indoor"                    # indoor | outdoor | kitti | custom
  detection:
    config: "fcaf3d_scannet"        # or "centerpoint_nuscenes" or "pointpillars_kitti"
  segmentation:
    enabled: true
    config: "pointnet2_s3dis"       # or "pointnet2_scannet"

tiling:
  max_points_per_tile: 100000
  overlap_margin_m: 2.0

postprocessing:
  confidence_threshold: 0.5
  min_size_m: null                  # e.g., 0.1 to ignore tiny detections
  max_size_m: null                  # e.g., 50.0 to ignore huge detections
  excluded_labels: []               # e.g., ["curtain", "other furniture"]

output:
  normalize_coordinates: false
  include_aabb: true
  include_segmentation: true
```

## Output Format

The pipeline outputs one JSON file per input scene:

```json
{
  "metadata": {
    "source_file": "site_scan_001.e57",
    "model": "fcaf3d_scannet",
    "model_version": "1.0",
    "confidence_threshold": 0.5,
    "tile_count": 142,
    "processing_time_seconds": 387.2,
    "device": "cuda:0",
    "scene_bounds": {
      "min": [0.0, 0.0, -2.0],
      "max": [150.0, 200.0, 15.0]
    },
    "coordinates_normalized": false
  },
  "detections": [
    {
      "id": "det_0001",
      "label": "chair",
      "confidence": 0.92,
      "bbox_obb": {
        "center": [12.5, 34.2, 0.45],
        "size": [0.6, 0.6, 0.9],
        "heading": 1.57
      },
      "bbox_aabb": {
        "min": [12.1, 33.8, 0.0],
        "max": [12.9, 34.6, 0.9]
      }
    }
  ],
  "segmentation": {
    "format": "point_index_map",
    "classes": ["wall", "floor", "ceiling", "..."],
    "num_points": 5000000,
    "file": "site_scan_001_segmentation.json"
  }
}
```

## Available Models

### Detection (bounding boxes)

| Config key | Dataset | Classes | Input | Best for |
|-----------|---------|---------|-------|----------|
| `pointpillars_kitti` | KITTI | 3 classes (car, pedestrian, cyclist) | XYZ + intensity | KITTI velodyne data |
| `votenet_scannet` | ScanNet | 18 indoor classes (furniture, doors, windows) | XYZ (+ optional RGB) | Indoor point clouds (Livox, TLS, room scans) |
| `fcaf3d_scannet` | ScanNet | 18 indoor classes (furniture, doors, windows) | XYZ + RGB | Indoor RGB (requires MinkowskiEngine) |
| `centerpoint_nuscenes` | nuScenes | 10 outdoor classes (vehicles, pedestrians) | XYZ + intensity | 360° outdoor LiDAR (Velodyne, Ouster) |

### Segmentation (per-point labels)

| Config key | Dataset | Classes | Focus |
|-----------|---------|---------|-------|
| `pointnet2_scannet` | ScanNet | 20 classes | Indoor (wall, floor, ceiling, furniture) |
| `pointnet2_scannet_xyz` | ScanNet | 20 classes | Indoor, geometry-only (no colour) |
| `pointnet2_s3dis` | S3DIS | 13 classes | Structural (wall, floor, ceiling, column, beam) |

### Model Selection Guide

| Your Data | Recommended Mode | Detection Model | Segmentation |
|-----------|-----------------|-----------------|-------------|
| KITTI velodyne `.bin` | `--mode kitti` | PointPillars (3-class) | Disabled (no matching model) |
| Outdoor 360° LiDAR (nuScenes-like) | `--mode outdoor` | CenterPoint (10-class) | PointNet2 S3DIS |
| Indoor RGB scan (Livox Mid-360) | `--mode indoor --coord-frame livox` | FCAF3D ScanNet (18-class, auto) / VoteNet fallback | PointNet2 ScanNet/S3DIS |
| Indoor RGB scan (Leica RTC360) | `--mode indoor --coord-frame leica` | FCAF3D ScanNet (18-class, auto) / VoteNet fallback | PointNet2 ScanNet/S3DIS |
| Custom annotated data | `--mode custom` | Fine-tuned weights (Phase 3) | Fine-tuned weights |

> **Selecting the right mode is critical.** Using the wrong mode will produce poor or no detections:
> - `indoor` detects furniture-scale objects (0.3–3m) in room scans
> - `outdoor` detects vehicle-scale objects (2–15m) in street/urban scenes
> - `kitti` is only for KITTI velodyne `.bin` files specifically

> **Livox Mid-360 coordinate frame:** Livox data is Z-up but rotated ~27° around Y.
> Always use `--coord-frame livox` when running detection or visualization on Livox data.

> **Checkpoints are code.** A `.pth` checkpoint is unpickled when it loads, so loading
> one runs whatever it contains. `scripts/run_finetuned_inference.py` and
> `scripts/run_centerpoint_inference.py` load with `weights_only=True`, which most
> mmengine checkpoints do not survive; pass `--trust-checkpoint` to load them fully, and
> only for files you trained or obtained from a source you trust. The pretrained
> checkpoints, when opted in, are pinned by SHA-256 and load weights-only (threat model
> T28).

### Pretrained weights and licensing

Every pretrained checkpoint in the tables above was trained on a dataset licensed for
non-commercial use only (see [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES)). The code
that defines the architectures is permissively licensed; the weights are not. So this
sample **downloads none of them by default**:

- `src/pointcloud_detect/backends/checkpoints.py` refuses every pretrained checkpoint
  before any network or cache access unless `ALLOW_NONCOMMERCIAL_WEIGHTS` is `1`,
  `true` or `yes`. The CLI exits with that explanation, and the API answers `400`
  for a job that would need one rather than queueing it. The exception is the `stub`
  image, which has no ML stack and so cannot fetch weights: there any model can be
  selected, and every job returns placeholder boxes.
- Semantic segmentation, for which only pretrained weights exist, is off unless the
  same variable is set.
- Training starts from scratch (`base_weights: scratch`) unless you choose
  `pretrained` (gated the same way) or a `.pth` of your own.
- The web UI lists the pretrained models as unavailable and selects none by default.
- On EKS, set `ALLOW_NONCOMMERCIAL_WEIGHTS=1` for `deploy.sh`, or the Helm values
  `models.allowNoncommercialWeights` (backend) and `training.allowNoncommercialWeights`
  (training). With Docker Compose, export it before `docker compose up`.

Opt in only if your use is permitted by those datasets' licenses. Otherwise, detect
with a model trained on data you may use, such as the one below.

### Commercially clean model

A detector trained from scratch inherits no pretrained checkpoint, so its terms are
those of the data it was trained on. This recipe trains FCAF3D on the
[CRAS-lab indoor point cloud dataset](https://doi.org/10.5281/zenodo.7948116), licensed
CC BY 4.0, in your own AWS account. No weights are distributed with this repository.
FCAF3D needs MinkowskiEngine (the GPU image, or the
[MinkowskiEngine setup](#optional-minkowskiengine-for-fcaf3d-indoor-model) above). Without
it, set `base_model: "votenet_scannet"` in the config and pass
`--architecture votenet_scannet` to train and detect with VoteNet instead.

```bash
# 1. Download craslabannotated.zip (~4.3 GB) from the Zenodo record
mkdir -p data/craslab
curl -L -o data/craslab/craslabannotated.zip \
    "https://zenodo.org/records/7948116/files/craslabannotated.zip?download=1"

# 2. Convert the per-point labels into box-annotated training scenes (.bin + .json),
#    cut into room-sized blocks. Downsampling bounds memory on the ~585M-point cloud.
uv run python scripts/convert-zenodo-craslab.py \
    --input data/craslab/craslabannotated.zip \
    --out-dir data/craslab_scenes --downsample-voxel 0.03

# 3. Train from scratch (GPU, the MMDetection3D stack and MinkowskiEngine required)
python -m pointcloud_detect.training.mmdet3d_trainer \
    --config configs/train_clean_indoor_craslab.yaml

# 4. Detect with the result; no opt-in needed
uv run pointcloud-detect detect scan.e57 \
    --weights models/clean_indoor_craslab/model.pth --architecture fcaf3d_scannet
```

The converter derives boxes from semantic labels, so they are axis-aligned (heading 0)
and an object's box covers the points labelled with its class that touch each other.
Edit `custom_classes` in the config to the classes with enough instances in your
converted scenes; the converter prints a per-class count. Keep the attribution with
the weights and anything you publish from them:

> Abreu, N., Souza, R., Pinto, A., Matos, A., & Pires, M. (2023). *Indoor point cloud
> dataset for BIM related applications*. Zenodo. https://doi.org/10.5281/zenodo.7948116
> (CC BY 4.0)

## Dataset Download & Parsing

The pipeline includes utilities to download and parse public 3D datasets into the unified annotation format used for fine-tuning.

### `pointcloud-detect data`

Download datasets and/or parse them into the fine-tuning annotation format.

```
pointcloud-detect data [ACTION] [OPTIONS]
```

| Argument/Option | Description |
|-----------------|-------------|
| `ACTION` | `download`, `parse`, or `both` (default: `both`) |
| `--data-root, -d PATH` | Root directory for dataset storage (default: `./data`) |
| `--output, -o PATH` | Output directory for parsed annotations (default: `data/annotations`) |
| `--dataset TEXT` | Specific dataset: `kitti`, `scannet`, or `s3dis` (default: all) |

None of these datasets is licensed for commercial use, and none is downloaded by
this command: each needs an account or registration, so it prints where to get the
files and accept their terms yourself, then verifies (KITTI) and parses what you
downloaded. For commercially usable test data see
[Test Datasets](#test-datasets).

```bash
# Verify the KITTI archives you downloaded (non-commercial use only) and parse them
uv run pointcloud-detect data both --dataset kitti

# Parse already-downloaded datasets
uv run pointcloud-detect data parse --output ./annotations

# Just create directory structure and show download instructions
uv run pointcloud-detect data download --dataset scannet
```

### Supported Datasets

| Dataset | Type | Download | Size | Classes |
|---------|------|----------|------|---------|
| KITTI 3D Object | Outdoor LiDAR | Free account | ~29 GB velodyne, 21 MB labels | car, pedestrian, cyclist, van, truck |
| nuScenes mini | Outdoor 360° LiDAR | Free account | ~4 GB (10 scenes) | car, truck, bus, pedestrian, motorcycle, bicycle |
| ScanNet v2 | Indoor rooms | Manual (registration) | ~1.3 TB full, selective scenes possible | 18 classes (furniture, doors, windows) |
| S3DIS | Indoor structural | Manual (registration) | ~6 GB | 13 classes (ceiling, floor, wall, beam, column) |

### Unified Annotation Format

All datasets are parsed into a common JSON format compatible with Phase 3 fine-tuning:

```json
{
  "scene_id": "000001",
  "point_cloud_path": "data/kitti/training/velodyne/000001.bin",
  "num_annotations": 3,
  "annotations": [
    {
      "center_xyz": [10.0, 2.0, 0.85],
      "size_wlh": [1.6, 4.2, 1.5],
      "heading": 1.57,
      "class_name": "car"
    }
  ]
}
```

### Dataset Notes

- **KITTI**: Requires a free account at https://www.cvlibs.net/datasets/kitti/user_login.php. Download the archives from KITTI's 3D object page into `data/kitti/`, then run the `download` action, which checks each against its SHA-256 before extracting it and lists any that are missing. The velodyne point clouds are ~29 GB — labels and calibration alone are only 21 MB if you just need annotation parsing.
- **nuScenes mini**: Requires a free account at https://www.nuscenes.org/nuscenes. Download the `v1.0-mini` tarball (~4 GB, 10 scenes with 360° LiDAR sweeps). This is the correct test data for `--mode outdoor` (CenterPoint).
- **ScanNet**: Requires accepting Terms of Use at http://www.scan-net.org/. The `download` action creates the expected directory structure and prints the download commands.
- **S3DIS**: Requires registration at http://buildingparser.stanford.edu/dataset.html. Bounding boxes are derived from per-instance point cloud extents.

### Testing with nuScenes (outdoor mode)

```bash
# 1. Register at https://www.nuscenes.org/nuscenes (free)
# 2. Download v1.0-mini tarball and save to data/nuscenes/v1.0-mini.tgz
# 3. Extract:
mkdir -p data/nuscenes
tar -xzf data/nuscenes/v1.0-mini.tgz -C data/nuscenes

# 4. Run detection on a nuScenes LiDAR sweep (360°, matches CenterPoint training)
uv run pointcloud-detect detect data/nuscenes/samples/LIDAR_TOP/<sample>.pcd.bin --mode outdoor --use-intensity

# Or run on all samples:
for f in data/nuscenes/samples/LIDAR_TOP/*.bin; do
  uv run pointcloud-detect detect "$f" --mode outdoor --use-intensity --confidence 0.3
done
```

nuScenes `.bin` files are N×5 float32 (x, y, z, intensity, ring_index) — 360° sweeps from a rooftop-mounted LiDAR, exactly what CenterPoint was trained on.

## Architecture

```
Input File (E57/LAS/LAZ/PLY/KITTI .bin)
    |
    v
[I/O Readers] ─── format auto-detection, chunked reading
    |
    v
[Preprocessing] ── voxel downsample, channel selection, spatial crop
    |
    v
[Adaptive Tiling] ─ octree subdivision, overlap margins
    |
    v
[Detection] ─────── per-tile inference (MMDetection3D or stub)
[Segmentation] ──── per-tile inference (MMDetection3D or stub)
    |
    v
[Post-processing] ─ NMS, AABB, confidence/size/label filtering, normalisation
    |
    v
[Output] ────────── JSON (atomic writes)
```

The inference backend is selected automatically: if `mmdet3d` is installed, the real MMDetection3D backend is used. Otherwise, a stub backend generates synthetic results for pipeline testing.

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run pyright
```

## Local Development (Web UI + API)

Run the full web application locally for development and offline testing without EKS.

### Prerequisites

- Python 3.12+ with `uv` installed
- Node.js 22+ (for frontend; the build, both CIs and `frontend/Dockerfile` use Node 22)
- Docker (optional, for container testing)

### Backend (API Server)

```bash
# Install with API dependencies (same set CI syncs)
uv sync --frozen --extra dev --extra api --extra io --extra viz

# Start the API server (includes worker in-process for local dev). Bind it to
# loopback: it has no authentication, and anything that can reach the port can read
# and change every local job.
uv run uvicorn pointcloud_detect.api.local_backend:app --host 127.0.0.1 --port 8000 --reload

# API is now at http://localhost:8000
# Health check: curl http://localhost:8000/health
# Swagger docs: http://localhost:8000/docs
```

The local backend (`local_backend.py`) runs detection in-process rather than via SQS/worker,
so no queue or S3 setup is needed. Uploaded files and results are stored locally. It
parses uploads in the same process, with no isolation, so use it only for data you would
be content to open on your own machine (threat model T11, T33). Its CORS policy admits
only the local dev servers (`http://localhost:5173` and `:3000`, and their `127.0.0.1`
forms).

### Frontend

```bash
cd frontend

# Install dependencies
npm ci

# Start dev server (proxies /api to backend at :8000)
npm run dev

# Frontend is now at http://localhost:5173
```

The Vite dev server proxies API requests to the backend. No CORS configuration needed.

### Full Local Stack with Docker Compose

```bash
# Build and start all services (API + Worker + Frontend + a local S3/SQS emulator)
docker compose up --build

# Access (every port is bound to 127.0.0.1, so nothing else on the network reaches it):
#   Frontend: http://localhost:3000
#   API:      http://localhost:8000
#   S3/SQS (moto):  http://localhost:4566
```

The compose services run with `no-new-privileges`, and the api, worker and frontend
with a read-only root filesystem and a `/tmp` tmpfs, as they do on EKS. The worker's
`/tmp` is capped at 512 MB; its downloads, pipeline output and extracts go to the
disk-backed `pcd-scratch` volume at `/scratch`, as they go to the chart's scratch
`emptyDir`. `docker compose down -v` removes it.

### Local Testing Workflow

1. Start backend: `uv run uvicorn pointcloud_detect.api.local_backend:app --host 127.0.0.1 --port 8000 --reload`
2. Start frontend: `cd frontend && npm run dev`
3. Open http://localhost:5173
4. Upload a point cloud → job is processed locally → view results in 3D viewer
5. Enter annotation mode → draw bounding boxes → save → start training (15s stub)
6. Fine-tuned model appears in detection dropdown

## Test Datasets

The default test data is commercially usable (CC BY 4.0): Rohbau3D for the indoor
pipeline and annotation workflow, and CRAS-lab for training the
[commercially clean model](#commercially-clean-model). With no model yet, the stub
backend runs the whole pipeline on any file with placeholder boxes. The non-commercial
datasets are listed separately; use them only if your use is permitted by their terms.

### Quick-Start Test Files

| Dataset | Type | Size | License | Best For |
|---------|------|------|---------|----------|
| [Rohbau3D](https://doi.org/10.60776/ZWJFI4) | Indoor construction TLS (504 scans, 14 sites) | Large; download single sites or features | CC BY 4.0 | Indoor pipeline, door/wall annotation workflow |
| [CRAS-lab](https://doi.org/10.5281/zenodo.7948116) | Indoor office/lab TLS, per-point labels | ~4.3 GB zip | CC BY 4.0 | Training the commercially clean model |

Non-commercial: register and download them yourself, and use them only if your use is permitted:

| Dataset | Type | Size | License | Best For |
|---------|------|------|---------|----------|
| [KITTI 3D object](https://www.cvlibs.net/datasets/kitti/eval_object.php?obj_benchmark=3d) (free account) | Outdoor velodyne | ~29 GB velodyne, 21 MB labels and calibration | CC BY-NC-SA 3.0 | Outdoor detection (`--mode kitti`) |
| [nuScenes mini](https://www.nuscenes.org/nuscenes) (manual download after registering) | 360° outdoor LiDAR | ~4 GB (10 scenes) | CC BY-NC-SA 4.0 | Outdoor detection (`--mode outdoor`) |

### Rohbau3D (Recommended for Indoor/Annotation Testing)

Construction site LiDAR scans with clearly visible doors, walls, and structural elements.
Ideal for testing the annotation → fine-tuning → detection loop. Published by Rauch &
Braml on Open Data UniBw M ([doi:10.60776/ZWJFI4](https://doi.org/10.60776/ZWJFI4),
CC BY 4.0, described in [Sci Data 12, 1478 (2025)](https://doi.org/10.1038/s41597-025-05827-7));
cite it in anything you publish from it.

```bash
# The scans are published as per-feature NumPy arrays. The dataset's own repository
# downloads selected sites and converts a scan to E57 or PCD:
#   https://github.com/RauchLukas/rohbau3d

# Run detection on a converted scan
uv run pointcloud-detect detect rohbau3d_scan.e57 --mode indoor --use-color --coord-frame z-up

# Or use in the web UI:
# 1. Upload via Detection tab with VoteNet/FCAF3D model
# 2. Or upload via Training/Annotation tab to annotate doors manually
```

### Livox Mid-360 Scans (Custom Sensor Testing)

If you have access to a Livox Mid-360 scanner:

```bash
# Livox outputs PLY with RGB — use the livox coordinate frame
uv run pointcloud-detect detect livox_scan.ply --mode indoor --use-color --coord-frame livox

# For the web UI, upload and select VoteNet (Indoor/ScanNet) model
# The livox coord-frame is auto-applied for known Livox file patterns
```

### DALES Aerial LiDAR (Large-Scale Outdoor)

Aerial LiDAR over urban areas — useful for testing tiling with large files.

```bash
# Download from https://udayton.edu/engineering/research/centers/vision_lab/research/was_702702.php
# ~500M points per tile, tests the adaptive tiling pipeline
uv run pointcloud-detect detect dales_tile.las --mode outdoor --max-points 20000000
```

### Generating Synthetic Test Data

For unit testing without real datasets:

```bash
# The stub backend generates synthetic detections — no mmdet3d needed
uv sync --extra dev --extra api --extra io --extra viz

# Run on any PLY/LAS file — stub produces random boxes for pipeline testing
uv run pointcloud-detect detect any_file.ply --mode indoor

# Run the full test suite (uses synthetic data internally)
uv run pytest tests/ -v
```

## Project Structure

```
src/pointcloud_detect/
├── __init__.py
├── cli.py              # Typer CLI (detect, info, data commands)
├── config.py           # Pydantic configuration models
├── models.py           # Data models (PointCloud, Tile, Detection, etc.)
├── io_readers.py       # E57/LAS/LAZ/PLY/KITTI .bin readers
├── preprocessing.py    # Voxel downsample, channel selection, crop
├── tiling.py           # Adaptive octree tiling
├── inference.py        # Backend interface + stub + factory
├── postprocessing.py   # NMS, AABB, filtering, normalisation
├── output.py           # JSON output writer
├── pipeline.py         # Pipeline orchestration
├── backends/
│   ├── __init__.py
│   └── mmdet3d.py      # MMDetection3D backend implementation
└── data/
    ├── __init__.py
    ├── download.py     # Dataset download utilities (KITTI, ScanNet, S3DIS)
    └── parse.py        # Parse datasets into unified annotation format
```

## Phasing

- **Phase 1** (current): Standalone Python CLI, no Docker. Prove detection and segmentation locally.
- **Phase 2**: Containerise and deploy to AWS VAMS as a managed pipeline.
- **Phase 3**: Fine-tuning workflow for custom annotated LiDAR datasets.

### Phase 3 Plan: Fine-Tuning for Custom Sensors (Livox Mid-360)

Pre-trained models have a domain gap when applied to sensors they weren't trained on.
For production use with a Livox Mid-360 (or similar), fine-tuning is required:

1. **Base architecture**: FCAF3D — the starting point this sample uses for indoor RGB
   point clouds, for the reasons below. The code is Apache 2.0; its public pretrained
   weights are non-commercially licensed (see
   [Pretrained weights and licensing](#pretrained-weights-and-licensing)), so training
   starts from scratch unless you opt in
   - Works on arbitrary point cloud ranges (no fixed voxel grid)
   - Accepts XYZ + RGB (matches Livox Mid-360 colour output)
   - 18-class indoor detection covers doors, windows, furniture
   - Requires MinkowskiEngine (`Dockerfile.gpu` builds it on CUDA 12.9)
   - **Now available** via `.venv311` (Python 3.11, CUDA 12) — auto-selected when MinkowskiEngine is installed

2. **Annotation workflow**:
   - Capture Livox scans of target environments
   - Annotate with 7-DOF bounding boxes using SUSTechPOINTS or Segments.ai
   - Export in unified annotation format (see Dataset section above)

3. **Training pipeline**:
   - From scratch (`base_weights: scratch`, the default), from weights of your own, or,
     with `ALLOW_NONCOMMERCIAL_WEIGHTS=1`, transfer learning from the pretrained ones
   - Add custom classes (scaffolding, MEP, formwork, rebar) as needed
   - Evaluate mAP@0.25 and mAP@0.50 on held-out validation split

4. **Deployment**:
   - Register fine-tuned weights via `--mode custom` with custom weights path
   - No code changes needed — just point to new `.pth` file in config

```yaml
# Example config for fine-tuned Livox model
model:
  mode: "custom"
  detection:
    config: "fcaf3d_scannet"          # architecture stays the same
    weights: "./models/fcaf3d_livox_finetuned_v1.pth"
  segmentation:
    enabled: true                     # off by default: only pretrained weights exist
    config: "pointnet2_s3dis"
    weights: "./models/pointnet2_livox_finetuned_v1.pth"

# Livox Mid-360 coordinate frame (Z-up, +27 deg rotation around Y)
preprocessing:
  coord_frame: "livox"
```

See [pointcloud-detection-pipeline-spec.md](pointcloud-detection-pipeline-spec.md) for the full requirements and specification document.


## EKS Deployment

> **Sample code, not a production service.** It is provided for non-production use. For
> its security review it was deployed to a sandbox account in both access modes,
> port-forward and public, with the stub image; the GPU image was not deployed on a
> cluster. Work with your
> security and legal teams before you load real data, and read the
> [threat model](docs/security/THREAT_MODEL.md) first: it lists what is accepted, what
> is deferred, and what needs a live deployment to confirm.

> **Each signed-in user sees only their own jobs, by default.** In public mode
> `ACCESS_SCOPE` is `per-user`: the API verifies the load balancer's signed sign-in
> claims on every request, and each user reaches only the jobs, uploads, results, tiles,
> annotations, training jobs and fine-tuned models they created (threat model T31).
> `ACCESS_SCOPE=shared` lets every signed-in user list, open and delete every job, for a
> team whose members may all see each other's scans; create users then only for people
> who may see all of it. Port-forward mode is always shared: anyone with its access entry
> reaches every job.

> **Detections are advisory.** The models are unmodified public checkpoints with no
> adversarial robustness training: physically realisable objects and point perturbations can hide
> objects from them or make them see objects that are not there. Keep a person in the
> loop for any safety or access decision (T65).

### Prerequisites

- AWS account with ECR, EKS, S3, SQS configured (see `infrastructure/`)
- `docker`, `kubectl`, `helm`, `jq`, `terraform` and `aws` CLI installed; the
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  if you use the SSM relay for a private cluster endpoint
- **Account-level S3 Block Public Access, set by you.** The stack blocks public access
  on each of its own buckets, but it no longer manages the account-wide setting, which
  it would otherwise own and could change for every bucket in the account. Check it
  first, and turn it on if your account policy allows:

  ```bash
  aws s3control get-public-access-block --account-id "$(aws sts get-caller-identity --query Account --output text)"
  aws s3control put-public-access-block --account-id "$(aws sts get-caller-identity --query Account --output text)" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  ```

  To have Terraform manage it instead, set `manage_account_public_access_block = true`.
  It is then protected with `prevent_destroy`, and `destroy.sh` takes it out of state
  rather than delete it, so tearing the stack down never switches it off (T37).
- **CloudTrail** in the account, with S3 and DynamoDB data events if your policy asks
  for them. The sample does not create a trail (T45).
- ECR login: `aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com`

### Access

`infrastructure/scripts/deploy.sh` builds everything, and one choice decides how people
reach the result. Set it before the first run:

| `ACCESS_MODE` | Users open | Gate | You provide |
|---|---|---|---|
| `public` (default) | `https://<INGRESS_HOST>/` | The ALB signs every request in with Amazon Cognito before it reaches a pod | A hostname, an ACM certificate for it (`ACM_CERT_ARN`, `INGRESS_HOST`), and DNS pointing it at the ALB |
| `port-forward` | `http://localhost:8080` through `kubectl port-forward` | The user's own IAM identity: an EKS access entry and a namespaced Role that allows port-forwarding and nothing else | Nothing extra |

The application itself has no login code and ships no API key; sign-in is a
deployment prerequisite, handled by the ALB. In public mode the API then verifies the
ALB's signed claims on every request and gives each user only their own jobs
(`ACCESS_SCOPE=per-user`, the default; `shared` lets every user see every job). The charts refuse to render an
internet-facing listener without a certificate and Cognito, so there is no
unauthenticated internet mode. Inside the cluster, anyone who can write Ingresses, or
Services in the application namespace, can still take sign-in off a rule, so keep
both to cluster admins; `deploy.sh` stops if a Service in the namespace could bypass
the ALB.

**Public mode.** Self sign-up is off, and TOTP MFA is required by default. After
deploying, create each user (the script prints this command with the pool ID filled
in); Cognito emails a temporary password:

```bash
aws cognito-idp admin-create-user --region <region> \
  --user-pool-id "$(terraform -chdir=infrastructure output -json cognito | jq -r .user_pool_id)" \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

The user opens `https://<INGRESS_HOST>/`, is sent to the Cognito sign-in page, sets a
new password and an authenticator, and comes back signed in for 12 hours. **Sign out**
in the navigation bar ends the session in that browser: it expires the ALB's cookie
there and signs out of Cognito's hosted page. It revokes no token, so a copy of the
cookie taken earlier still works until the session ends. To cut a user off everywhere,
run `aws cognito-idp admin-user-global-sign-out` or `admin-disable-user`; access ends
within 15 minutes. With `alarm_email` set, at least 10 failed sign-ins in 5 minutes
raise the `<prefix>-cognito-failed-sign-ins` alarm (`cognito_failed_sign_in_alarm_threshold`).
The ALB forwards `x-amzn-oidc-*` claim headers to the pods; nothing here uses them, and
anything built on this must verify the signature of `x-amzn-oidc-data` before
authorizing on them, because they are only trustworthy when they came from the ALB.

**Cluster admins and tunnels.** The cluster creator is no longer made a permanent
cluster admin. Cluster-admin access entries are created for the principals in
`cluster_admin_principal_arns` or, when that is empty, for the identity that first
applied the stack. That identity is recorded in Terraform state and kept, so a later
apply or `deploy.sh` run by someone else (a second operator, a CI role, a break-glass
role) neither takes over the entry nor locks the first one out; the newcomer simply has
no access. Set the variable explicitly where more than one person or pipeline
administers the cluster, and `terraform apply -replace=module.eks.terraform_data.first_admin`
hands the default to the current caller on purpose. `kubectl port-forward`
is authorised by the caller's IAM identity: other people and CI roles need an entry in
`app_client_principal_arns`, and an IAM policy of their own that allows
`eks:DescribeCluster` on the cluster, which `aws eks update-kubeconfig` calls before
any port-forward. The access entry only authorizes Kubernetes API calls.

Port-forward mode, the UI and the API:

```bash
kubectl -n pointcloud-detection port-forward svc/frontend 8080:80   # UI at http://localhost:8080
kubectl -n pointcloud-detection port-forward svc/api 8000:80        # API at http://localhost:8000
```

Open the UI at exactly `http://localhost:8080`: it is the one origin the upload buckets
accept. 8080 is the default of the Terraform variable `port_forward_local_port`; if you
change it, use that port in both places, as `deploy.sh` prints.

Public mode, scripts only: use the API tunnel, `svc/api`, as above. A tunnel to
`svc/frontend` is no use there. Its nginx serves the SPA but does not proxy `/api`,
which the ALB routes itself, so every API call through it gets the SPA's HTML.

**Private cluster endpoint: the SSM relay.** The cluster's API server has a private
endpoint only, reachable from inside the VPC, and with `enable_bastion = true` (the
root default) you reach it through a relay. The bastion is a relay, not a workstation: it has no tools and no
cluster access of its own, its only egress is HTTPS inside the VPC, and three Systems
Manager interface endpoints serve it (about USD 22 a month, see
[cost estimate](docs/solution-guidance/cost-estimate.md)). You run kubectl and helm on
your own machine, under your own IAM identity, through a Session Manager
port-forward. Terraform prints the exact commands:

```bash
terraform -chdir=infrastructure output -raw bastion_relay_commands
```

They are, in outline: start a port-forward session to the endpoint host
(`aws ssm start-session --target <instance-id> --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters host=<endpoint host>,portNumber=443,localPortNumber=8443`);
write a separate kubeconfig with `aws eks update-kubeconfig --kubeconfig ~/.kube/<cluster>-relay`;
point it at `https://127.0.0.1:8443` with `--tls-server-name <endpoint host>`; and export
`KUBECONFIG`. `deploy.sh` and `destroy.sh` use kubeconfigs of their own and leave yours
alone; with the relay running, set `PCD_KUBE_RELAY_PORT=8443` and they connect through
it the same way. On a first deploy the relay does not exist until the apply has made it:
`deploy.sh` then stops at its cluster-access check, and running it again with the relay
up plans no changes and carries on. If the cluster cannot be reached at all,
`SKIP_KUBE_CLEANUP=1 destroy.sh` goes straight to the Terraform destroy; in public mode
delete the ALB the Ingress left behind first, or the VPC destroy fails on it. Your identity needs an access entry (`cluster_admin_principal_arns` or
`app_client_principal_arns`) and IAM permission to start a port-forwarding session on
the instance: attach `terraform -chdir=infrastructure output -raw bastion_operator_policy_json`
to your role. It allows `ssm:StartSession` on the relay with the
`AWS-StartPortForwardingSessionToRemoteHost` document only, and
`ssm:SessionDocumentAccessCheck` makes Session Manager refuse any other document, so
it gives no shell on the relay. Do not grant a bare `ssm:StartSession` on the instance:
that also allows the default shell document. The relay's own role can run SSM Agent's
session channels and nothing else; it cannot read Parameter Store. Session Manager
does not log port-forward content; CloudTrail records `StartSession`, and the EKS audit
log records every request under your identity. If your account policy asks for session
logging, configure Session Manager's preferences in the account; the sample does not,
because that document is shared by the whole account and Region.

See [the runbook](docs/EKS_DEPLOY_RUNBOOK.md) for the full procedure, user management,
upgrading an existing deployment and upgrading from the earlier token-based version.

### What deploy.sh does, and what it checks

- **Reviewed plans.** It runs `terraform plan -out` into a private temporary
  directory, shows the plan, and asks before it applies exactly that plan. Set
  `AUTO_APPROVE=1` to skip the question in automation; it is the only bypass.
  `destroy.sh` works the same way, and always asks its first question.
- **Commit-SHA image tags.** `build-push.sh` tags each image with the first 12
  characters of the commit, and refuses uncommitted or untracked files among the
  images' sources (`src/`, `frontend/`, the Dockerfile and the Python project files)
  unless you set `IMAGE_TAG` yourself. Edits elsewhere, such as your
  `terraform.tfvars`, do not count. Redeploying the same commit pushes nothing,
  because the tag is already in the (immutable) repository. The charts refuse an empty
  tag and `latest`, and pull `IfNotPresent`.
- **Pod Security.** It labels the namespace `pod-security.kubernetes.io/enforce`,
  `warn` and `audit` = `restricted`. Every pod the charts render meets that profile:
  non-root, all capabilities dropped, no privilege escalation, RuntimeDefault seccomp,
  a read-only root filesystem and no service-account token.
- **Add-on credentials.** After installing the Load Balancer Controller (public mode),
  Cluster Autoscaler, Fluent Bit and KEDA, it waits for each and searches its recent
  logs for credential errors (`AccessDenied`, `NoCredentialProviders`,
  `WebIdentityErr` and similar), and stops if it finds one. **Never work around such a
  failure by raising the nodes' IMDS hop limit**: that hands the node's IAM role to
  every pod on the node. Fix the service account's IRSA annotation or trust instead.
- **Cluster access.** It checks that you have cluster admin before it changes the
  cluster, and explains the access-entry model if you do not.

### Container Images

| Image | Dockerfile | Purpose |
|-------|-----------|---------|
| Backend (API + Worker stub) | `Dockerfile` | API server + worker with stub detection |
| Backend CPU (real inference) | `Dockerfile.cpu` | Worker with mmdet3d + CPU fallback patches, built on the GPU image by digest |
| Backend GPU | `Dockerfile.gpu` | Worker with mmdet3d + CUDA (requires GPU nodes) |
| Frontend | `frontend/Dockerfile` | React SPA served by nginx |

Every base image is pinned by tag and digest; the Python, Node and nginx bases come
from their verified ECR Public mirrors. Every image runs as a non-root user and works
with a read-only root filesystem.

### Build & Push

`deploy.sh` builds and pushes the backend and frontend images for you. To build by
hand, tag with the commit SHA, as `build-push.sh` does:

```bash
ECR=<account-id>.dkr.ecr.<region>.amazonaws.com
TAG=$(git rev-parse --short=12 HEAD)

# Backend (API + stub worker)
docker build -t $ECR/pointcloud-detection-backend:$TAG .
docker push $ECR/pointcloud-detection-backend:$TAG

# Backend GPU (real mmdet3d inference on GPU nodes)
docker build --no-cache -f Dockerfile.gpu -t $ECR/pointcloud-detection-backend:$TAG-gpu .
docker push $ECR/pointcloud-detection-backend:$TAG-gpu

# Backend CPU: built on the GPU image from the same commit, by digest. BASE_IMAGE is
# required, and anything that is not name@sha256:... is refused.
GPU_DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' $ECR/pointcloud-detection-backend:$TAG-gpu)
docker build --no-cache -f Dockerfile.cpu --build-arg BASE_IMAGE="$GPU_DIGEST" \
  -t $ECR/pointcloud-detection-backend:$TAG-cpu .
docker push $ECR/pointcloud-detection-backend:$TAG-cpu

# Frontend
docker build --no-cache -t $ECR/pointcloud-detection-frontend:$TAG ./frontend
docker push $ECR/pointcloud-detection-frontend:$TAG
```

The training chart is installed by hand, and needs the tag of a GPU image explicitly:
`helm upgrade --install training infrastructure/helm/training -n pointcloud-detection
--set image.repository=$ECR/pointcloud-detection-backend --set-string image.tag=$TAG-gpu
--set networkPolicy.vpcCidr="$(terraform -chdir=infrastructure output -raw vpc_cidr)"`.
Its arguments do not yet match the trainer's command line, which takes only
`--config`, so the Job does not start as rendered (see the threat model, M7 in 1.2).

### Deploy to EKS

Prefer re-running `deploy.sh`, which passes every value the charts need. To move a
running Deployment to another image by hand:

```bash
kubectl set image deployment/api api=$ECR/pointcloud-detection-backend:$TAG -n pointcloud-detection

# Worker (CPU inference with mmdet3d). The worker pod has two containers, worker and
# parser, which must run the same image.
kubectl set image deployment/worker worker=$ECR/pointcloud-detection-backend:$TAG-cpu \
  parser=$ECR/pointcloud-detection-backend:$TAG-cpu -n pointcloud-detection

kubectl set image deployment/frontend frontend=$ECR/pointcloud-detection-frontend:$TAG -n pointcloud-detection

# Check rollout status
kubectl rollout status deployment/api -n pointcloud-detection
kubectl rollout status deployment/worker -n pointcloud-detection
kubectl rollout status deployment/frontend -n pointcloud-detection
```

### How uploaded files are parsed

Uploaded point clouds are untrusted input to native parsers (laspy/lazrs, pye57 with
libE57 and Xerces-C, trimesh). On EKS the worker pod runs them in a separate `parser`
container: the same image as a different user, with no AWS credential, no Kubernetes
token and none of the worker's environment. It refuses to start if it can see any AWS
credential. The worker downloads the file into a shared spool, the parser turns it
into a fixed-layout point array, and the worker reads only that array, with numpy. A
parser exploit therefore reaches one job's data, not the worker's IAM role.

**What remains (T11).** The two containers share the pod's network, so a compromised
parser could still send that one job's scan out over HTTPS until the worker's egress is
narrowed further (T20). docker-compose, `local_backend`, the CLI and
`worker.parser.enabled=false` parse in-process, with no isolation.

In every mode the parser is chosen by the file's validated extension, and a file whose
magic bytes disagree is refused; PLY texture references are not followed; E57 XML DTDs
are disabled (`XERCES_DISABLE_DTD=1`); and LAS/LAZ headers that declare more points
than the file can hold are refused.

**Scratch sizing.** In sidecar mode a job's peak scratch use is the input plus its
canonical copy, 24 to 31 bytes a point: about 1× the input for LAS, 1.5 to 2× for
KITTI `.bin` and binary PLY, and 8 to 15× for LAZ. Size `worker.scratch.sizeLimit`
(default 150Gi) for at least the largest input × 16 when LAZ is accepted, or × 3
otherwise.

The parser settings, all set by the backend chart:

| Variable | Default | Description |
|----------|---------|-------------|
| `PCD_PARSER_MODE` | `in-process` (the chart sets `sidecar`) | Where uploads are parsed |
| `PCD_PARSER_SPOOL` | — (`/scratch/ipc`) | The directory the worker and parser share; created on first use |
| `PCD_PARSER_TIMEOUT_SECONDS` | `7200` | Per-job parse limit, at most 11 hours, below SQS's 12-hour visibility cap |
| `PCD_PARSER_MEMORY_LIMIT_BYTES` | unset | Optional address-space cap on each conversion |

### Personal data and retention

Mobile and terrestrial scans routinely capture people, faces, number plates and the
inside of buildings, and point clouds are often geo-referenced. Treat an upload as
personal data unless you know it is not. Whether and how GDPR, CCPA or similar laws
apply is **your responsibility as the operator of the deployment**; see AWS's
[Data Privacy FAQ](https://aws.amazon.com/compliance/data-privacy-faq/),
[GDPR Center](https://aws.amazon.com/compliance/gdpr-center/) and
[CCPA page](https://aws.amazon.com/compliance/california-consumer-privacy-act/), and
the shared responsibility model.

What each store keeps, and for how long, on the shipped defaults:

| Store | What it holds | Kept for |
|---|---|---|
| S3 input, `uploads/<id>/`, or `uploads/<user id>/<id>/` in per-user mode | The uploaded scan. In per-user mode the key carries the uploader's Cognito user id (`sub`), a random identifier, as the job record does | 30 days; earlier versions 7 days after a delete or overwrite |
| S3 output, `results/<id>/` | Detections, and `segmentation.json` with every point's coordinates | 90 days; earlier versions 7 days |
| S3 tiles, `tiles/<id>/` | Level-of-detail copies of the scan | 30 days; earlier versions 7 days |
| S3 models | Fine-tuned weights (derived from training data) | Until deleted; earlier versions 30 days |
| S3 access logs | Requester, object key, source IP | 90 days; earlier versions 7 days |
| DynamoDB jobs table | Filename, input key, status, counts, error text | 90 days (`JOBS_TTL_DAYS`); point-in-time recovery keeps 35 days of history |
| SQS queue and DLQ | Job id, input key, config | Until processed; up to 14 days in the DLQ |
| CloudWatch Logs `/aws/eks/<cluster>/workloads` | Filenames, keys, error text | 30 days |
| EKS control-plane log group | Kubernetes API audit, including user names | Until you set a retention (none by default) |
| Amazon Cognito user pool (public mode) | Each user's email address (also the username), password verifier, TOTP secret, sign-in history | Until the user, or the pool, is deleted; the pool is deleted with the stack |
| Worker scratch (`emptyDir`) | The input, its canonical copy, tile extracts | Deleted with the pod |

**Deleting one job's data.** `DELETE /api/jobs/{id}` (the **Delete** action on the
dashboard, for complete and failed jobs) removes the job's input, everything under
`results/<id>/` and `tiles/<id>/`, its annotations and its record. It refuses while the
job, or another job using the same input, is still queued or processing, and keeps the
input while another job record still uses it.

```bash
curl -X DELETE -H 'X-PCD-Client: spa' http://localhost:8000/api/jobs/<JOB_ID>   # through the API tunnel
```

What the API cannot reach, and the manual steps for an erasure request:

- **Earlier object versions** expire 7 days after the delete. To remove them at once:
  `aws s3api list-object-versions --bucket <bucket> --prefix <prefix>` and
  `aws s3api delete-object --bucket <bucket> --key <key> --version-id <id>` for each
  version and delete marker, with the bucket names from `terraform output`.
- **DynamoDB point-in-time recovery** keeps the deleted record in its 35-day backup
  window; it cannot be purged selectively. Disable and re-enable PITR to drop the
  history if you must.
- **Logs.** Filenames and keys stay in CloudWatch Logs for the retention above; delete
  the log streams if needed.
- **A user.** `aws cognito-idp admin-disable-user`, then `admin-delete-user`, removes a
  user and their personal data from the pool.

**Review users periodically.** Nothing removes people who have left. List the pool's
users (`aws cognito-idp list-users --user-pool-id <id>`) on a schedule, and disable, then
delete, anyone who should no longer have access (T70).

### Upgrading an existing deployment

This version changes a few resources in ways an existing stack must prepare
for. The [runbook](docs/EKS_DEPLOY_RUNBOOK.md#upgrading-an-existing-deployment) has the
exact commands:

- **Cluster admin entry.** Existing clusters already hold the creator's access entry, so
  import it into the new explicit entries (or delete it) before applying.
- **ECR encryption.** The repositories move to a customer managed KMS key, which
  replaces them. Delete the old repositories (`aws ecr delete-repository --force`) once
  before applying; `deploy.sh` pushes the images again, and a training image must be
  pushed again by hand.
- **Bastion.** It is replaced by the SSM relay on the next apply, which also removes the
  old kubectl and helm binaries and its cluster-admin access.
- **Account-level Block Public Access.** Terraform forgets it without deleting it. Check
  it afterwards with the command in [Prerequisites](#prerequisites).
- **AWS provider 6 and S3 bucket names.** The stack needs AWS provider 6.37 or later,
  and creates every bucket in the account's regional namespace
  (`…-<account>-<region>-an`), which no other account can use. That replaces all five
  buckets: copy what you want to keep, such as fine-tuned checkpoints, and empty the old
  buckets, every version included, before applying. Bahrain (`me-south-1`) and UAE
  (`me-central-1`) do not offer the namespace.
- **Terraform state encryption.** `deploy.sh` now writes the state with the state
  bucket's KMS key instead of SSE-S3. Its first run re-initialises the backend in
  place; nothing moves.
- **No public cluster endpoint.** `enable_public_endpoint` and `public_access_cidrs`
  are gone: the API server is private only, and you reach it through the SSM relay. If
  your tfvars set either, delete them (Terraform warns about undeclared variables) and
  set `enable_bastion = true`, or keep it at the default. The apply turns the public
  endpoint off in place.
- **GPU image bases.** `Dockerfile.gpu` builds on the AWS Deep Learning Containers base
  (CUDA 12.9) and ships on Ubuntu 22.04, both from ECR Public, with torch's CUDA 12.9
  build. Its CUDA builds start at Turing, so V100 (`p3`) nodes no longer run it, and
  the plan refuses a GPU pool of them; the node pools use T4, A10G, L4 and A100.

### Scaling

```bash
# Scale API (1 replica for demo — in-memory job store doesn't share across pods)
kubectl scale deployment api -n pointcloud-detection --replicas=1

# Scale worker
kubectl scale deployment worker -n pointcloud-detection --replicas=1

# Scale frontend
kubectl scale deployment frontend -n pointcloud-detection --replicas=2
```

### Checking Logs

```bash
# Worker logs (see detection/segmentation output)
kubectl logs deployment/worker -c worker -n pointcloud-detection --tail=30

# The parser container's logs (conversion failures)
kubectl logs deployment/worker -c parser -n pointcloud-detection --tail=30

# Stream worker logs in real-time
kubectl logs deployment/worker -c worker -n pointcloud-detection --tail=10 -f

# API logs
kubectl logs deployment/api -n pointcloud-detection --tail=30

# Frontend pod logs (nginx access logs)
kubectl logs deployment/frontend -n pointcloud-detection --tail=20

# Check all pod statuses
kubectl get pods -n pointcloud-detection

# Check which image a deployment is running
kubectl get pods -n pointcloud-detection -l app=worker -o jsonpath='{.items[0].spec.containers[0].image}'

# Exec into a running worker container (the root filesystem is read-only)
kubectl exec -it deployment/worker -c worker -n pointcloud-detection -- /bin/bash

# Check if mmdet3d is available in the worker
kubectl exec deployment/worker -c worker -n pointcloud-detection -- /app/.venv/bin/python3 -c "import mmdet3d; print(mmdet3d.__version__)"
```

`kubectl logs` only reaches a pod that still exists. A worker that OOM-killed, was
evicted, or was rolled takes its logs with it — and those are the only record of why a
job failed. `deploy.sh` installs Fluent Bit, on every node including the GPU nodes, to
ship pod logs to CloudWatch, which keeps them past the pod:

```bash
# Log group is /aws/eks/<cluster>/workloads, stream prefix <cluster>-
aws logs tail "/aws/eks/${CLUSTER_NAME}/workloads" --follow --region "${REGION}"

# Just one job, across whichever pod happened to run it
aws logs filter-log-events \
  --log-group-name "/aws/eks/${CLUSTER_NAME}/workloads" \
  --filter-pattern '"<job-id>"' \
  --region "${REGION}"
```

The log group name is not arbitrary. The Fluent Bit IAM policy is scoped to
`/aws/eks/<cluster>/*`, so moving the group outside that prefix makes Fluent Bit fail
with `AccessDenied` on `CreateLogGroup` and silently drop every line. Retention is set
to 30 days at install. The worker writes one JSON object per log record.

### Testing the Pipeline

In public mode with per-user access, the default, the API serves only requests signed
in through the ALB, so through this tunnel only `/health` answers; use the UI, or
deploy with `ACCESS_SCOPE=shared`.

```bash
# Scripts reach the API through a tunnel authorised by your IAM identity; no token
kubectl -n pointcloud-detection port-forward svc/api 8000:80 &

# Check API health
curl http://localhost:8000/health

# List jobs
curl http://localhost:8000/api/jobs

# List available models
curl http://localhost:8000/api/models

# Check tile index for a completed job (verify segmentation)
curl http://localhost:8000/api/results/<JOB_ID>/tiles/index | python3 -m json.tool

# Check S3 for job outputs. The bucket names come from Terraform, not from a fixed name.
OUTPUT_BUCKET=$(terraform -chdir=infrastructure output -raw s3_output_bucket)
TILES_BUCKET=$(terraform -chdir=infrastructure output -raw s3_tiles_bucket)
aws s3 ls "s3://${OUTPUT_BUCKET}/results/<JOB_ID>/" --region <region>
aws s3 ls "s3://${TILES_BUCKET}/tiles/<JOB_ID>/" --region <region>
```

### UI URL

Public mode: `https://<INGRESS_HOST>/`. Point that name at the ALB, whose DNS name is
`kubectl -n pointcloud-detection get ingress pointcloud-spa -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'`.
Do not browse to the ALB's own name: every rule matches `INGRESS_HOST`, so it returns
404, and Cognito would not return you to it. Browsers remember the HSTS header the
ALB sends for a year, so use a name you will keep on HTTPS. Port-forward mode:
`http://localhost:8080`.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Job stuck in "processing" | Worker crashed or wrong image | Check `kubectl logs deployment/worker -c worker` and `-c parser` |
| Every job fails with a parser "timeout" | The parser container is not running | Check `kubectl logs deployment/worker -c parser`; it refuses to start if it can see AWS credentials |
| "mmdet3d not installed" in logs | Worker using stub image, not CPU image | Deploy the `<sha>-cpu` tag to both worker containers |
| S3 PUT 403 on upload | KMS permission or SigV4 issue | Check backend-role IAM policy |
| Browser console reports a Content-Security-Policy violation | A changed SPA loads from another origin | Add the origin to the policy in `infrastructure/helm/frontend/templates/_helpers.tpl`, or set `contentSecurityPolicy.reportOnly=true` while you work it out |
| Dashboard flickers | Multiple API pods with in-memory store | Scale API to 1 replica |
| `has_segmentation: false` | Point count mismatch in tile gen | Ensure processor.py has preprocess fix |
| `build-push.sh` refuses to build | Uncommitted or untracked files among the images' sources, which it lists | Commit them, or set `IMAGE_TAG` explicitly |
| `deploy.sh` stops on credential errors | An add-on's IRSA annotation or trust is wrong | Fix the service account; do not raise the IMDS hop limit |
| "CUDA not found" in worker | Expected on CPU nodes | Use `Dockerfile.cpu` with mmcv patches |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_REGION` | `us-east-1` | AWS region |
| `SQS_QUEUE_URL` | — | SQS job queue URL (set via Helm) |
| `S3_INPUT_BUCKET` | — (required in AWS mode) | Input file bucket, from `terraform output -raw s3_input_bucket` |
| `S3_OUTPUT_BUCKET` | — (required in AWS mode) | Detection results bucket, from `terraform output -raw s3_output_bucket` |
| `S3_TILES_BUCKET` | — (required in AWS mode) | LOD tile bucket, from `terraform output -raw s3_tiles_bucket` |
| `S3_MODELS_BUCKET` | — (required in AWS mode) | Fine-tuned model bucket, from `terraform output -raw s3_models_bucket`; the only place a registered model may point |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | Largest request body the API reads; larger ones get 413 before they are parsed |
| `MAX_UPLOAD_BYTES` | `5368709120` | Largest upload the API will start (5 GiB); stage larger files to the input bucket directly |
| `API_DOCS_ENABLED` | `false` | Serve `/docs`, `/redoc` and `/openapi.json`; for development only |
| `ACCESS_SCOPE` | — (required in AWS mode) | `per-user`: each signed-in user reaches only what they created; `shared`: every caller reaches everything. The backend chart sets `per-user` in public mode and `shared` in port-forward mode; `deploy.sh` passes `ACCESS_SCOPE` through as `accessScope` |
| `ALB_ARN`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` | — (per-user mode) | What the API checks each request's signed sign-in claims against: the ALB that signed them, the user pool that issued them, and the app client. `deploy.sh` sets them (`identity.*`); until `ALB_ARN` is set, `/api` answers 503 |
| `WORKER_DEVICE` | `auto` | Which jobs this worker pool accepts: `auto`, `cpu`, or `gpu`. Set via `worker.device`. See [Device routing](#device-routing) before changing it |
| `SQS_MAX_RECEIVE_COUNT` | `3` | Delivery attempt at which a job is reported failed. Must stay at or below the queue's redrive `maxReceiveCount` |
| `JOBS_TTL_DAYS` | `90` | Job record retention, written as the `expires_at` TTL attribute; expired records are hidden at once |
| `API_BASE_URL` | `http://api:8000` | Internal API URL (worker → API, memory store only) |
| `XERCES_DISABLE_DTD` | `1` | Keeps the E57 reader's XML parser from processing DTDs; do not unset |
| `ALLOW_NONCOMMERCIAL_WEIGHTS` | `0` | `1` lets the API, worker and trainer download the non-commercially licensed pretrained checkpoints. Set via `models.allowNoncommercialWeights`. See [Pretrained weights and licensing](#pretrained-weights-and-licensing) |

The `PCD_PARSER_*` settings are in [How uploaded files are parsed](#how-uploaded-files-are-parsed).

### Device routing

A job carries a `device_preference` of `auto`, `cpu`, or `gpu`. A worker pool carries a
matching capability in `worker.device`. A pool accepts a job when either side says
`auto`, or when the two agree.

**The default is a single pool at `auto`, which accepts everything.** Leave it there
unless you are actually running more than one pool. The mechanism has a cost that is
easy to miss:

All pools share one SQS queue, and SQS has no way to inspect a message without
receiving it. So when a pool declines a job it has already spent one of that message's
delivery attempts against the queue's redrive `maxReceiveCount`. Bouncing a job between
mismatched pools burns the same budget that exists to catch genuinely broken jobs.

The worker bounds this rather than letting a healthy job disappear: on its final
delivery a mismatched pool stops deferring and reports the job `failed` with
`No worker available for device_preference=...`. You get a red badge and a reason
instead of a job stuck at `processing` while its message sits in the dead-letter queue.

If you do run split pools:

- Raise the delivery budget so routing bounces do not consume it. Set Terraform
  `var.max_receive_count` and Helm `jobStore.maxReceiveCount` together, keeping the
  Helm value at or below the Terraform one.
- Keep at least one replica of each pool warm, or size the budget to cover node
  provisioning. A GPU pool scaled to zero takes minutes to come up, which is far longer
  than a few bounces at five seconds apart.
- Deploy one chart release per pool, each with its own `worker.device`.

For production heterogeneous fleets, prefer one queue per pool over shared-queue
filtering. Routing then happens at submit time and never consumes a job's retry budget,
and KEDA scales each pool on its own queue depth. That is a larger change — an extra
queue and dead-letter queue per pool in Terraform, per-queue `ScaledObject`s, and
queue selection in the API — and is not what this chart ships.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.
The threat model is [docs/security/THREAT_MODEL.md](docs/security/THREAT_MODEL.md): what
can go wrong, what is fixed, what is accepted and by whom, what is deferred, and the
scanner results. The earlier data-path record is
[docs/THREAT_MODEL_DATA_PATH.md](docs/THREAT_MODEL_DATA_PATH.md).

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

This project references third-party software, container images and pre-trained model
weights under their own licenses, some of them non-commercial or proprietary. See
[THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) before you deploy it. No model weights are
distributed with it, and the non-commercially licensed pretrained weights are not
downloaded unless you set `ALLOW_NONCOMMERCIAL_WEIGHTS=1`.
