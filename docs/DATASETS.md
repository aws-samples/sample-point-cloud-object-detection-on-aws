# Datasets Used & Tested

This document consolidates all datasets used in the pipeline — for inference validation, fine-tuning, and annotation.

---

## Datasets Tested in Our Pipeline

| Dataset | Type | License | What We Used It For | Results |
|---------|------|---------|--------------------|---------| 
| **Rohbau3D** | Indoor construction LiDAR | CC BY 4.0 | Indoor test data (doors, walls) | — |
| **Livox office scans** (custom) | Indoor office LiDAR | Ours | Annotation + fine-tuning (doors) | — |
| **KITTI** | Outdoor driving LiDAR | CC BY-NC-SA 3.0 | Pre-trained inference (cars) | — |
| **nuScenes mini** | Outdoor driving LiDAR | CC BY-NC-SA 4.0 | Barrier conversion example (`convert-nuscenes-barriers.py`) | — |
| **S3DIS** | Indoor office LiDAR | Research use | Pre-trained segmentation (13 classes) | walls, floors, doors labeled |
| **DALES-2** | Aerial/drone LiDAR | MIT | Fine-tuning (vehicles, poles, buildings) | Available for aerial detection |

---

## Dataset Details

### 1. Rohbau3D (Indoor Construction)

- **Source**: https://github.com/RauchLukas/rohbau3d
- **License**: CC BY 4.0 (free for any use with attribution)
- **Format**: NumPy arrays (coord.npy, color.npy, class.npy, instance.npy)
- **Content**: German construction site LiDAR — raw concrete structures with labeled doors, walls, windows, columns
- **Size**: Multiple sites, ~2-10M points per scene
- **Classes**: 20+ construction-specific classes (Wall, Door Rough Opening, Window, Column, etc.)
- **How to get**:
  ```bash
  git init -q data/rohbau3d
  git -C data/rohbau3d fetch -q --depth 1 https://github.com/RauchLukas/rohbau3d.git f1f2e0bbb9b5605df606b5ca60ce2215c96862d9
  git -C data/rohbau3d checkout -q FETCH_HEAD   # a reviewed commit, not the default branch
  cd data/rohbau3d && uv pip install -e .
  python scripts/download.py --config config/dataverse.yaml --download --extract
  ```
- **Why we chose it**: CC BY 4.0 license (ScanNet is non-commercial), real construction data with doors/walls

### 2. Livox Office Scans (Custom)

- **Source**: Captured with Livox Mid-360 scanner in office environments
- **License**: Ours (internal)
- **Format**: Binary PCD (exported via `prepare-for-annotation.py`)
- **Content**: 4 office scenes with doors, chairs, ladders
- **Size**: 300K points per scene (subsampled from original)
- **Annotations**: Created using SUSTechPOINTS → `convert-sustechpoints.py`
- **Scenes**:
  - `office_scene` — single room with door
  - `office_ladder_scene` — room with door + ladder
  - `office_ladder_chair_scene` — room with door + ladder + chair
  - `office_chair_scene` — room with door + chair

### 3. KITTI (Outdoor Driving)

- **Source**: https://www.cvlibs.net/datasets/kitti/
- **License**: CC BY-NC-SA 3.0 (non-commercial)
- **Format**: Binary .bin (N × 4 float32: x, y, z, intensity)
- **Content**: Velodyne HDL-64E LiDAR, 64 beams, urban driving in Karlsruhe
- **Size**: 7,481 training frames, ~120K points per frame
- **Classes**: Car, Pedestrian, Cyclist (3D bounding boxes)
- **How to get**: KITTI asks every user to register and accept its terms. Sign in at
  https://www.cvlibs.net/datasets/kitti/user_login.php, download the archives you need
  from the [3D object page](https://www.cvlibs.net/datasets/kitti/eval_object.php?obj_benchmark=3d)
  into `data/kitti/`, then verify and extract them. Each archive is checked against the
  SHA-256 this sample pins for it, and one that does not match is refused:
  ```bash
  # data_object_label_2.zip and data_object_calib.zip (21 MB), and optionally
  # data_object_velodyne.zip (29 GB), saved to ./data/kitti/
  pointcloud-detect data download --dataset kitti --data-root ./data
  ```
- **Usage**: Pre-trained PointPillars inference validation

### 4. nuScenes Mini (Outdoor Driving)

- **Source**: https://www.nuscenes.org/download
- **License**: CC BY-NC-SA 4.0 (non-commercial)
- **Format**: Binary .bin (N × 5 float32: x, y, z, intensity, ring_index)
- **Content**: 32-beam LiDAR, 1000 scenes in Boston + Singapore
- **Size**: Mini = 10 scenes (404 keyframes); Full = 1000 scenes
- **Classes**: 10 (car, truck, bus, trailer, construction_vehicle, pedestrian, motorcycle, bicycle, traffic_cone, barrier)
- **How to get**:
  ```bash
  # Requires free registration at nuscenes.org
  # Or if available on S3:
  aws s3 cp s3://<bucket>/v1.0-mini.tgz /mnt/efs/datasets/nuscenes/
  tar -xzf v1.0-mini.tgz
  pip install nuscenes-devkit==1.2.0 pyquaternion==0.9.9
  ```
- **What we extracted**: 100 frames with "barrier" annotations → 1,205 bounding boxes
- **Conversion**: `python scripts/convert-nuscenes-barriers.py` (global→sensor frame transform)

### 5. S3DIS (Indoor Segmentation)

- **Source**: Stanford 3D Indoor Spaces dataset
- **License**: Research use
- **Format**: Point clouds with per-point semantic labels
- **Content**: 6 areas of office buildings (271 rooms)
- **Size**: ~696M points total
- **Classes**: 13 (ceiling, floor, wall, beam, column, window, door, table, chair, sofa, bookcase, board, clutter)
- **Usage**: Pre-trained PointNet2 segmentation model (no fine-tuning yet)

### 6. DALES-2 (Aerial/Drone LiDAR)

- **Source**: https://huggingface.co/datasets/mbendjilali/DALES-2
- **License**: MIT (fully open, commercial use allowed)
- **Format**: LAZ/LAS files with per-point semantic + instance labels
- **Content**: Large-scale aerial LiDAR over 10km², urban/suburban/rural mix
- **Size**: 2.77GB compressed, ~500M points across 40 tiles (500m×500m each)
- **Classes**: 15 (ground, vegetation, car, powerline, fence, tree, pickup, van_truck, heavy_duty, utility_pole, light_pole, traffic_pole, habitat, complex, annex)
- **Instance labels**: Yes — unique per-object IDs for vehicles, poles, buildings
- **How to get**:
  ```bash
  pip install huggingface_hub==2.0.0 laspy==2.7.0
  python -c "
  from huggingface_hub import snapshot_download
  snapshot_download(repo_id='mbendjilali/DALES-2', repo_type='dataset', local_dir='data/dales2')
  "
  ```
- **Conversion**: `python scripts/convert-dales-aerial.py`
- **Architecture**: PointPillars (outdoor, same as KITTI/nuScenes)
- **Why it matters**: Only aerial/drone LiDAR dataset in our pipeline. Demonstrates detection from above (buildings, vehicles in parking lots, utility infrastructure).

---

## How to Add a New Dataset

### For Pre-trained Inference (just test)

Any point cloud file works. Supported formats: `.ply`, `.las`, `.laz`, `.e57`, `.bin`, `.pcd`

```bash
pointcloud-detect detect your_file.ply --mode indoor    # VoteNet
pointcloud-detect detect your_file.bin --mode kitti     # PointPillars
pointcloud-detect detect your_file.bin --mode outdoor   # CenterPoint
```

### For Fine-Tuning (custom objects)

1. **Prepare point clouds** → `scripts/prepare-for-annotation.py`
2. **Annotate** → SUSTechPOINTS (3D bounding boxes)
3. **Convert** → `scripts/convert-sustechpoints.py --all`
4. **Train (indoor)** → `python -m pointcloud_detect.training.mmdet3d_trainer --config <yaml>`
5. **Train (outdoor)** → `python scripts/finetune_centerpoint.py --data-dir <dir> --classes <cls>`
6. **Infer** → `scripts/run_finetuned_inference.py` or `scripts/run_centerpoint_inference.py`
7. **Visualize** → `scripts/visualize_3d.py <points> <detections.json>`

### For External Datasets (e.g., nuScenes, Toronto-3D)

Write a conversion script (like `scripts/convert-nuscenes-barriers.py`) that outputs our unified annotation format:

```json
{
  "scene_id": "scene_001",
  "point_cloud_path": "points/scene_001.bin",
  "num_annotations": 5,
  "annotations": [
    {
      "center_xyz": [1.0, 2.0, 0.5],
      "size_wlh": [2.0, 0.5, 1.0],
      "heading": 1.57,
      "class_name": "guardrail"
    }
  ]
}
```

---

## Critical: Coordinate Frame Requirements

| Dataset Source | Native Frame | What Our Pipeline Expects |
|---------------|-------------|--------------------------|
| nuScenes annotations | **Global** (hundreds of meters) | **Sensor** (ego vehicle at origin) |
| nuScenes LiDAR .bin | Sensor (ego at origin) | Sensor ✓ |
| KITTI .bin | Sensor (ego at origin) | Sensor ✓ |
| DALES-2 .laz | Local UTM (Z = altitude) | Cropped + centered by convert script ✓ |
| Rohbau3D | Scene-local (Z-up) | Scene-local ✓ |
| Livox / SUSTechPOINTS | Scanner (Z-up) | Scanner ✓ |

**If annotations are in global frame, you MUST transform to sensor frame** (see `convert-nuscenes-barriers.py` for the ego_pose + calibrated_sensor transform).

---

## Quick Reference: Download Commands

```bash
# Rohbau3D (CC BY 4.0, no registration)
git init -q data/rohbau3d
git -C data/rohbau3d fetch -q --depth 1 https://github.com/RauchLukas/rohbau3d.git f1f2e0bbb9b5605df606b5ca60ce2215c96862d9
git -C data/rohbau3d checkout -q FETCH_HEAD   # a reviewed commit, not the default branch

# KITTI labels and calibration (21 MB, free account): download them from
# https://www.cvlibs.net/datasets/kitti/eval_object.php?obj_benchmark=3d after signing in,
# save them to data/kitti/, then verify and extract them:
pointcloud-detect data download --dataset kitti --data-root ./data

# nuScenes mini (requires registration at nuscenes.org)
# After downloading v1.0-mini.tgz:
tar -xzf v1.0-mini.tgz

# Convert nuScenes barriers for fine-tuning:
python scripts/convert-nuscenes-barriers.py \
    --nuscenes-root /mnt/efs/datasets/nuscenes \
    --version v1.0-mini \
    --output-dir data/annotations/barriers \
    --export-points

# DALES-2 aerial LiDAR (MIT license, no registration, 2.77GB)
pip install huggingface_hub==2.0.0 laspy==2.7.0
python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='mbendjilali/DALES-2', repo_type='dataset', local_dir='data/dales2')
"
```

---

## End-to-End: DALES-2 Aerial Vehicle Detection

Full workflow for detecting vehicles from drone/aerial LiDAR using DALES-2.

### Step 1: Download DALES-2

```bash
pip install huggingface_hub==2.0.0 laspy==2.7.0

python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='mbendjilali/DALES-2', repo_type='dataset', local_dir='data/dales2')
"
```

### Step 2: Convert to Training Format

The conversion script crops large 500m×500m tiles into 100m×100m training windows and extracts 3D bounding boxes from instance labels.

```bash
# Vehicles only (cars, pickups, vans/trucks)
python scripts/convert-dales-aerial.py \
    --dales-root data/dales2 \
    --output-dir data/annotations/dales_vehicles \
    --classes car pickup van_truck \
    --export-points

# Or just test with 2-3 tiles first:
python scripts/convert-dales-aerial.py \
    --dales-root data/dales2 \
    --output-dir data/annotations/dales_vehicles \
    --classes car pickup van_truck \
    --export-points \
    --max-tiles 3
```

### Step 3: Fine-Tune PointPillars

PointPillars is the correct architecture for aerial data (outdoor scale, voxel-based).

```bash
python scripts/finetune_centerpoint.py \
    --data-dir data/annotations/dales_vehicles \
    --classes car pickup van_truck \
    --epochs 20 --lr 0.001 \
    --output-dir models/dales_vehicles
```

### Step 4: Run Inference

```bash
# Pick any exported scene
SCENE=$(ls data/annotations/dales_vehicles/points/ | head -1)

# --trust-checkpoint: mmengine checkpoints need full unpickling. Only for files you trained.
python scripts/run_centerpoint_inference.py --trust-checkpoint \
    --checkpoint models/dales_vehicles/epoch_20.pth \
    --input data/annotations/dales_vehicles/points/$SCENE \
    --classes car pickup van_truck \
    --score-thr 0.1 \
    --output-json output/dales_detections.json
```

### Step 5: Visualize

```bash
python scripts/visualize_3d.py \
    data/annotations/dales_vehicles/points/$SCENE \
    output/dales_detections.json \
    output/

# Open output/*_3d.html in a browser
```

### Notes on Aerial LiDAR

- DALES-2 point density is ~20 pts/m² (much sparser than ground-level LiDAR)
- Objects are seen from above — cars appear as flat rectangles, buildings as footprints
- Z-axis is altitude (ground ≈ 0, buildings = height above ground)
- No heading estimation from aerial view (all headings set to 0.0)
- PointPillars with KITTI range works well — aerial tiles are cropped to 100m windows
