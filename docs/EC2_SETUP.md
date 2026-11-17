# EC2 GPU Instance — Setup & Fine-Tuning Guide

## Instance Requirements

- **Instance type**: g5.xlarge (1x A10G GPU, 4 vCPU, 16GB RAM) or g4dn.xlarge
- **AMI**: Ubuntu 22.04 LTS
- **Storage**: 100GB+ EBS (gp3)
- **Security group**: SSH (port 22) from your IP only, as a `/32`. Do not open the viewer ports (8080, 8081) in the security group; reach them through an SSH tunnel (below), or through a Session Manager port-forward if the instance has no SSH at all

---

## Step 1: Launch & Connect

```bash
ssh -i your-key.pem ubuntu@<ec2-public-ip>
```

---

## Step 2: Clone the Repo

```bash
git clone <your-gitlab-repo-url> sample-point-cloud-object-detection-on-aws
cd sample-point-cloud-object-detection-on-aws
```

---

## Step 3: Run Setup Script

This installs, at pinned versions: uv 0.12.19 (checked against its published SHA-256), CPython 3.11.16 through it, PyTorch 2.13.0 for the CUDA 12 toolkit on the host, the MMDetection3D stack that `Dockerfile.gpu` uses, with mmcv compiled against that toolkit, and the pipeline. Compiling mmcv needs `nvcc`; on a Deep Learning AMI with several toolkits, `CUDA_HOME=/usr/local/cuda-12.8 bash scripts/setup-ec2.sh` picks one, and `MAX_JOBS` sets how many compile jobs run at once.

```bash
bash scripts/setup-ec2.sh
```

> **If the script fails mid-way** (e.g., CUDA mismatch), fix manually:
> ```bash
> export PATH="$HOME/.local/bin:$PATH"
> source .venv/bin/activate
> # Fix .gitignore backslashes (Windows → Linux)
> sed -i 's|\\|/|g' .gitignore
> # Install pipeline
> uv sync --frozen --extra io --extra api --extra dev
> # Match PyTorch to the CUDA 12 toolkit (nvcc --version): cu129 for 12.9, cu126 otherwise
> CUDA_TAG=cu126
> uv pip install torch==2.13.0 torchvision==0.28.0 --index-url "https://download.pytorch.org/whl/${CUDA_TAG}"
> # Install mmdet3d, at the versions scripts/setup-ec2.sh pins; mmcv compiles here, from
> # its checked sdist with the C++ standard raised to C++20 (README, "Optional: real ML inference")
> uv pip install numpy==1.26.4 setuptools==69.5.1 wheel==0.45.1 mmengine==0.10.7
> curl -fsSL -o /tmp/mmcv.tar.gz https://files.pythonhosted.org/packages/a7/19/f97f0fd4d432113a232e75f965335be6b7bb6557ae9faf8051e9a012cadd/mmcv-2.1.0.tar.gz
> echo "d387bcab66b467479b6660310e23746cfc79c6e57acf04094680adb499a5cd3f  /tmp/mmcv.tar.gz" | sha256sum -c -
> mkdir /tmp/mmcv && tar -xzf /tmp/mmcv.tar.gz -C /tmp/mmcv --strip-components=1 && sed -i 's/-std=c++1[47]/-std=c++20/g' /tmp/mmcv/setup.py
> FORCE_CUDA=1 MMCV_WITH_OPS=1 uv pip install /tmp/mmcv --no-build-isolation
> uv pip install mmdet==3.2.0 mmdet3d==1.4.0 --no-build-isolation --no-cache
> uv pip install setuptools==84.2.1 && python scripts/patch_mmengine_metadata.py
> # Drop plyfile (GPL-3.0) and lyft-dataset-sdk (CC BY-NC-SA 4.0), which this sample does not use
> python scripts/patch_mmdet3d_lyft.py && uv pip uninstall lyft-dataset-sdk plyfile pandas pytest black flake8
> ```

Compiling mmcv's CUDA kernels is most of the time: about 90 minutes in all on an 8-vCPU g4dn.2xlarge with `MAX_JOBS=6`, and longer at the default of 2.

---

## Step 4: Activate Environment

```bash
source .venv/bin/activate
```

---

## Step 5: Verify Installation

```bash
python -c "import torch; print(f'PyTorch: {torch.__version__}, CUDA available: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"CPU\"}')"

python -c "import mmdet3d; print(f'MMDetection3D: {mmdet3d.__version__}')"

pointcloud-detect --help
```

---

## Step 6: Download & Prepare Data

### Rohbau3D (Indoor Construction — CC BY 4.0, no registration)

This is the primary dataset for fine-tuning. Real construction site LiDAR with labeled doors, walls, windows.

```bash
cd /mnt/efs/sample-point-cloud-object-detection-on-aws

# Clone the Rohbau3D tool
git init -q data/rohbau3d
git -C data/rohbau3d fetch -q --depth 1 https://github.com/RauchLukas/rohbau3d.git f1f2e0bbb9b5605df606b5ca60ce2215c96862d9
git -C data/rohbau3d checkout -q FETCH_HEAD   # a reviewed commit, not the default branch
cd data/rohbau3d
uv pip install -e .

# Edit config for sites with doors/walls (site 0 = apartment, site 12 = apartment with doors)
sed -i 's/site_selection: \[all\]/site_selection: [0, 12]/' config/dataverse.yaml

# Download + extract (~30 min)
python scripts/download.py --config config/dataverse.yaml --download --extract

# Convert first scene to PLY for our pipeline
cd /mnt/efs/sample-point-cloud-object-detection-on-aws
python -c "
import numpy as np, trimesh
coord = np.load('data/rohbau3d/data/extract/rohbau3d/site_00/scene_00000/coord.npy')
color = np.load('data/rohbau3d/data/extract/rohbau3d/site_00/scene_00000/color.npy')
cloud = trimesh.PointCloud(coord, colors=np.column_stack([color, np.full((len(color),1), 255, dtype=np.uint8)]))
cloud.export('data/rohbau3d_scene_00000.ply')
print(f'Saved {len(coord):,} points')
"
```

### KITTI (Outdoor LiDAR — direct download, no registration)

```bash
pointcloud-detect data download --dataset kitti --data-root ./data
```

---

## Step 7: Run Real Inference (All Modes)

### Indoor Detection (Rohbau3D construction scene)
```bash
pointcloud-detect detect data/rohbau3d_scene_00000.ply --mode indoor --use-color --max-points 2000000
```

### KITTI Detection (outdoor vehicles)

> **KITTI needs a free account.** Sign in at
> https://www.cvlibs.net/datasets/kitti/user_login.php and download the archives from the
> [3D object page](https://www.cvlibs.net/datasets/kitti/eval_object.php?obj_benchmark=3d).
> `pointcloud-detect data download --dataset kitti` then checks each one against the
> SHA-256 this sample pins for it and refuses one that does not match, so a changed or
> substituted archive is never extracted. Copy the archives to the instance, for example
> through an S3 bucket you own.

```bash
# Option A: KITTI labels and calibration (21 MB), and optionally the velodyne scans
# (29 GB), downloaded after signing in and saved to data/kitti/
mkdir -p data/kitti
pointcloud-detect data download --dataset kitti --data-root ./data

# Option B: Create synthetic KITTI-like file for quick test
python -c "
import numpy as np
rng = np.random.default_rng(42)
pts = np.zeros((50000, 4), dtype=np.float32)
pts[:, 0] = rng.uniform(-40, 40, 50000)
pts[:, 1] = rng.uniform(-40, 40, 50000)
pts[:, 2] = rng.uniform(-2, 2, 50000)
pts[:, 3] = rng.uniform(0, 1, 50000)
pts.tofile('data/kitti_synthetic.bin')
print('Created data/kitti_synthetic.bin (50K points)')
"

# Run KITTI detection
pointcloud-detect detect data/kitti/training/velodyne/000000.bin --mode kitti --use-intensity
# Or with synthetic:
pointcloud-detect detect data/kitti_synthetic.bin --mode kitti --use-intensity
```

### Outdoor Detection (CenterPoint — nuScenes-style)
```bash
# CenterPoint expects 360° LiDAR sweeps; KITTI works as a proxy
pointcloud-detect detect data/kitti/training/velodyne/000000.bin --mode outdoor --use-intensity
# Or with synthetic:
pointcloud-detect detect data/kitti_synthetic.bin --mode outdoor --use-intensity
```

### Check Results
```bash
# Detection JSON
python -c "
import json
with open('output/rohbau3d_scene_00000_detections.json') as f:
    data = json.load(f)
print(f'Model: {data[\"metadata\"][\"model\"]}  |  {len(data[\"detections\"])} detections')
for det in data['detections']:
    print(f'  {det[\"label\"]:15s} conf={det[\"confidence\"]:.4f}  size={[round(s,2) for s in det[\"bbox_obb\"][\"size\"]]}')
"

# Segmentation (classes + point counts)
python -c "
import json, collections
with open('output/rohbau3d_scene_00000_segmentation.json') as f:
    d = json.load(f)
c = collections.Counter(d['labels'])
print('Segmentation classes:')
for k, v in sorted(c.items()):
    print(f'  {d[\"classes\"][k]:20s}: {v:>10,} points')
"
```

### Expected Results

Each mode should complete and write a detections JSON (and, with segmentation
enabled, a segmentation JSON). These modes run the pretrained checkpoints, which
need `ALLOW_NONCOMMERCIAL_WEIGHTS=1` (README, "Pretrained weights and
licensing"); this guide does not report their scores.

---

## Step 7b: Visualize Results

### 3D Detection Viewer (interactive HTML)
```bash
# Generate 3D viewer HTML
python scripts/visualize_3d.py \
    data/rohbau3d_scene_00000.ply \
    output/rohbau3d_scene_00000_detections.json \
    output/ \
    --subsample 500000

# Segmentation viewer
python scripts/visualize_seg.py \
    data/rohbau3d_scene_00000.ply \
    output/rohbau3d_scene_00000_segmentation.json \
    output/ \
    --subsample 500000

# Serve HTML files on the instance's loopback only
python -m http.server 8080 --bind 127.0.0.1 --directory output/
```

From your own machine, forward the port over SSH (or with Session Manager:
`aws ssm start-session --target <instance-id> --document-name AWS-StartPortForwardingSession --parameters portNumber=8080,localPortNumber=8080`):

```bash
ssh -i your-key.pem -N -L 8080:127.0.0.1:8080 ubuntu@<ec2-public-ip>
```

Then open in your browser:
- `http://localhost:8080/rohbau3d_scene_00000_3d.html` — detections
- `http://localhost:8080/rohbau3d_scene_00000_segmentation_3d.html` — segmentation

### Bird's Eye View (KITTI)
```bash
python scripts/visualize.py \
    data/kitti/training/velodyne/000000.bin \
    output/000000_detections.json \
    output/
```

---

## Step 8: Annotate Your Own Data (SUSTechPOINTS)

Before fine-tuning on Livox or COLMAP data, you need bounding box annotations.

### Install Annotation Tool
```bash
bash scripts/setup-sustechpoints.sh
```

### Prepare Your Point Clouds
```bash
# Livox scan (apply livox coordinate transform)
python scripts/prepare-for-annotation.py your_livox_scan.ply livox_scene_001 --coord-frame livox

# COLMAP sparse reconstruction
python scripts/prepare-for-annotation.py colmap_output.ply colmap_scene_001 --coord-frame colmap

# Rohbau3D scene (already Z-up)
python scripts/prepare-for-annotation.py data/rohbau3d_scene_00000.ply rohbau3d_scene_000
```

### Start Annotation Server
```bash
cd tools/SUSTechPOINTS
python main.py --port 8081
```

The annotation server has no authentication, so keep port 8081 closed in the security
group and forward it instead: `ssh -i your-key.pem -N -L 8081:127.0.0.1:8081 ubuntu@<ec2-public-ip>`,
then open `http://localhost:8081`.

### Draw Bounding Boxes
1. Select your scene from the dropdown
2. **Ctrl + drag** to draw a bounding box around a door
3. Set label to `door` in the right panel
4. Adjust box size/rotation as needed
5. Repeat for all doors in the scene
6. Annotations auto-save to `tools/SUSTechPOINTS/data/<scene>/label/`

### Convert Annotations to Training Format
```bash
cd /mnt/efs/sample-point-cloud-object-detection-on-aws

# Convert single scene
python scripts/convert-sustechpoints.py livox_scene_001

# Convert all annotated scenes
python scripts/convert-sustechpoints.py --all

# Check output
ls data/annotations/custom/
cat data/annotations/custom/livox_scene_001.json | python -m json.tool
```

---

## Step 9: Fine-Tune Models (Three Examples)

### Example 1: Wall Segmentation (Rohbau3D)

Improve segmentation accuracy on construction LiDAR:

```bash
# Prepare wall annotations from Rohbau3D instance labels
python -c "
import numpy as np, json
from pathlib import Path

# Prepare door annotations from Rohbau3D site_00 + site_12
annotations = []
for site_dir in [Path('data/rohbau3d/data/extract/rohbau3d/site_00'), Path('data/rohbau3d/data/extract/rohbau3d/site_12')]:
    for scene_dir in sorted(site_dir.iterdir()):
        if not scene_dir.is_dir():
            continue
        coord = np.load(scene_dir / 'coord.npy')
        classes = np.load(scene_dir / 'class.npy')
        instances = np.load(scene_dir / 'instance.npy')
        
        # Class 14 = Door Rough Opening
        door_mask = classes == 14
        if door_mask.sum() == 0:
            continue
        
        door_instances = np.unique(instances[door_mask])
        boxes = []
        for inst_id in door_instances:
            inst_mask = (instances == inst_id) & door_mask
            pts = coord[inst_mask]
            if len(pts) < 50:
                continue
            center = ((pts.max(axis=0) + pts.min(axis=0)) / 2).tolist()
            size = (pts.max(axis=0) - pts.min(axis=0)).tolist()
            boxes.append({'center_xyz': center, 'size_wlh': size, 'heading': 0.0, 'class_name': 'door'})
        
        if boxes:
            annotations.append({
                'scene_id': scene_dir.name,
                'point_cloud_path': str(scene_dir / 'coord.npy'),
                'num_annotations': len(boxes),
                'annotations': boxes
            })

# Save
out_dir = Path('data/annotations/rohbau3d_doors')
out_dir.mkdir(parents=True, exist_ok=True)
for ann in annotations:
    (out_dir / f'{ann[\"scene_id\"]}.json').write_text(json.dumps(ann, indent=2))
print(f'Created {len(annotations)} scene annotations with door bounding boxes in {out_dir}')
"
```

Then run fine-tuning:
```bash
python -m pointcloud_detect.training.trainer --config configs/finetune_doors.yaml
```

> **Note:** Update `configs/finetune_doors.yaml` to point `data_path` to `./data/annotations/rohbau3d_doors`

---

## Step 9: Fine-Tune Wall Segmentation (Rohbau3D)

Prepare wall segmentation training data:

```bash
python -c "
import numpy as np, json
from pathlib import Path

# Prepare wall annotations from Rohbau3D
annotations = []
for site_dir in [Path('data/rohbau3d/data/extract/rohbau3d/site_00'), Path('data/rohbau3d/data/extract/rohbau3d/site_12')]:
    for scene_dir in sorted(site_dir.iterdir()):
        if not scene_dir.is_dir():
            continue
        coord = np.load(scene_dir / 'coord.npy')
        classes = np.load(scene_dir / 'class.npy')
        instances = np.load(scene_dir / 'instance.npy')
        
        # Class 5 = Wall
        wall_mask = classes == 5
        if wall_mask.sum() == 0:
            continue
        
        wall_instances = np.unique(instances[wall_mask])
        boxes = []
        for inst_id in wall_instances:
            inst_mask = (instances == inst_id) & wall_mask
            pts = coord[inst_mask]
            if len(pts) < 100:
                continue
            center = ((pts.max(axis=0) + pts.min(axis=0)) / 2).tolist()
            size = (pts.max(axis=0) - pts.min(axis=0)).tolist()
            boxes.append({'center_xyz': center, 'size_wlh': size, 'heading': 0.0, 'class_name': 'wall'})
        
        if boxes:
            annotations.append({
                'scene_id': scene_dir.name,
                'point_cloud_path': str(scene_dir / 'coord.npy'),
                'num_annotations': len(boxes),
                'annotations': boxes
            })

out_dir = Path('data/annotations/rohbau3d_walls')
out_dir.mkdir(parents=True, exist_ok=True)
for ann in annotations:
    (out_dir / f'{ann[\"scene_id\"]}.json').write_text(json.dumps(ann, indent=2))
print(f'Created {len(annotations)} scene annotations with wall bounding boxes in {out_dir}')
"
```

Then run fine-tuning:
```bash
python -m pointcloud_detect.training.trainer --config configs/finetune_walls.yaml
```

> **Note:** Update `configs/finetune_walls.yaml` to point `data_path` to `./data/annotations/rohbau3d_walls`

---

## Step 10: Fine-Tune Door Detection — Livox Data

Adapt the door detection model to Livox Mid-360 non-repetitive scanning pattern:

```bash
# 1. Annotate Livox scans (you should have annotated via Step 8)
python scripts/convert-sustechpoints.py --all --output-dir data/annotations/livox_doors

# 2. Create Livox-specific training config
cat > configs/finetune_doors_livox.yaml << 'EOF'
base_model: "votenet_scannet"
base_weights: "pretrained"  # non-commercial weights: needs ALLOW_NONCOMMERCIAL_WEIGHTS=1
data_path: "./data/annotations/livox_doors"
val_split: 0.2
output_path: "./models/doors_livox"
epochs: 30
learning_rate: 0.0005
batch_size: 4
num_workers: 4
checkpoint_interval: 5
early_stopping_patience: 10
augmentation:
  random_flip: true
  random_rotate: true
  random_scale: [0.95, 1.05]
  random_jitter: 0.01
custom_classes:
  - door
run_on_eks: false
EOF

# 3. Run fine-tuning
python -m pointcloud_detect.training.trainer --config configs/finetune_doors_livox.yaml

# 4. Evaluate
python -m pointcloud_detect.evaluation.evaluator \
    --checkpoint models/doors_livox/model_final.json \
    --data data/annotations/livox_doors
```

---

## Step 11: Fine-Tune Door Detection — COLMAP Sparse Point Clouds

Adapt detection to structure-from-motion derived point clouds (much sparser, noisier):

```bash
# 1. Annotate COLMAP reconstructions (you should have annotated via Step 8)
python scripts/convert-sustechpoints.py --all --output-dir data/annotations/colmap_doors

# 2. Create COLMAP-specific training config
cat > configs/finetune_doors_colmap.yaml << 'EOF'
base_model: "votenet_scannet"
base_weights: "pretrained"  # non-commercial weights: needs ALLOW_NONCOMMERCIAL_WEIGHTS=1
data_path: "./data/annotations/colmap_doors"
val_split: 0.2
output_path: "./models/doors_colmap"
epochs: 50
learning_rate: 0.001
batch_size: 4
num_workers: 4
checkpoint_interval: 10
early_stopping_patience: 15
augmentation:
  random_flip: true
  random_rotate: true
  random_scale: [0.9, 1.1]
  random_jitter: 0.05
custom_classes:
  - door
run_on_eks: false
EOF

# 3. Run fine-tuning
python -m pointcloud_detect.training.trainer --config configs/finetune_doors_colmap.yaml

# 4. Evaluate + compare with base model
python -m pointcloud_detect.evaluation.evaluator \
    --checkpoint models/doors_colmap/model_final.json \
    --data data/annotations/colmap_doors \
    --compare-with pretrained
```

---

## Step 12: Package Shipped Models

After fine-tuning, register models and update config for shipping:

```bash
python -c "

---

## Step 10: Evaluate Fine-Tuned Models

```bash
# Evaluate door detection
python -m pointcloud_detect.evaluation.evaluator \
  --checkpoint models/doors_detection/model_final.json \
  --data data/annotations/scannet_doors

# Evaluate wall segmentation
python -m pointcloud_detect.evaluation.evaluator \
  --checkpoint models/walls_segmentation/model_final.json \
  --data data/annotations/s3dis_walls

# A/B comparison (fine-tuned vs base)
python -m pointcloud_detect.evaluation.evaluator \
  --checkpoint models/doors_detection/model_final.json \
  --data data/annotations/scannet_doors \
  --compare-with pretrained
```

---

## Step 11: Register Models in Registry

```bash
python -c "
from pointcloud_detect.registry.store import ModelRegistry

registry = ModelRegistry('./models')

# Register door detection model
registry.register(
    version='v1.0.0',
    checkpoint_path='models/doors_detection/model_final.json',
    base_model='votenet_scannet',
    eval_metrics={'mAP@0.25': 0.0, 'mAP@0.50': 0.0},  # Fill with actual metrics
    dataset_info='ScanNet doors-only, 30 epochs fine-tuned',
)

# Register wall segmentation model
registry.register(
    version='v1.0.0',
    checkpoint_path='models/walls_segmentation/model_final.json',
    base_model='pointnet2_s3dis',
    eval_metrics={'mIoU': 0.0},  # Fill with actual metrics
    dataset_info='S3DIS walls-only, 50 epochs fine-tuned',
)

print('Models registered. Check models/model_registry.json')
"
```

---

## Step 12: Run Detection with Fine-Tuned Model

```bash
# Use fine-tuned door detection model
pointcloud-detect detect your_scan.ply --mode custom \
  --config configs/finetune_doors.yaml

# Or specify weights directly
pointcloud-detect detect your_scan.ply --mode indoor
# (update config.yaml to point to models/doors_detection/model_final.pth)
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `CUDA out of memory` | Reduce `batch_size` in config, or use `--max-points 5000000` |
| `mmcv build fails` | Ensure `setuptools==69.5.1` is installed before mmcv, `nvcc` is on `PATH` (or `CUDA_HOME` is set), and torch's CUDA major version matches the toolkit's |
| `nvcc not found` | Inference needs only the driver, but compiling mmcv needs the CUDA toolkit: set `CUDA_HOME` to one under `/usr/local` |
| CUDA version mismatch | torch 2.13.0 has builds for CUDA 12.6, 12.9 and 13.0, and compiles extensions against a toolkit of the same major version (a different minor only warns), so it needs a CUDA 12 toolkit. Check with `nvcc --version`, then reinstall: `uv pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cu126 --force-reinstall` (or `cu129` for a 12.9 toolkit), and rebuild mmcv. |
| `ModuleNotFoundError: mmdet3d` | Re-run: `uv pip install mmdet==3.2.0 mmdet3d==1.4.0 --no-build-isolation --no-cache` |
| Training loss not decreasing | Try lower learning rate (0.0001) or more epochs |

---

## Cleanup (to avoid EC2 charges)

```bash
# Stop the instance when done (from your local machine or AWS Console)
aws ec2 stop-instances --instance-ids <instance-id>

# Or terminate if no longer needed
aws ec2 terminate-instances --instance-ids <instance-id>
```
