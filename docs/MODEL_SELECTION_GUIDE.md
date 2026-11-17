# Model Selection Guide for Fine-Tuning

**CRITICAL**: Choosing the wrong model architecture for your data will produce poor or unusable results. The architecture must match the scale and characteristics of your point cloud data.

> **Licensing.** The base model names below choose an *architecture*. Training starts
> from scratch by default (`base_weights: scratch`). Their public pretrained weights were
> trained on non-commercially licensed data, so `base_weights: pretrained` is refused
> unless `ALLOW_NONCOMMERCIAL_WEIGHTS=1` is set; see the README sections "Pretrained
> weights and licensing" and "Commercially clean model".

## Model-to-Data Matching

| Data Type | Architecture | Base Model | Expected Range | Why |
|-----------|-------------|-----------|---------------|-----|
| **Indoor rooms** (5-10m) | VoteNet | `votenet_scannet` | ~5m radius | Point-based, ball-query radius tuned for room scale |
| **Outdoor driving** (50-100m) | PointPillars | `pointpillars_kitti` | 0-70m forward | Voxel/pillar-based, handles large sparse scenes natively |
| **Highway/road** (100m+) | PointPillars | `pointpillars_kitti` | 0-70m forward | Same as outdoor, clips to forward-facing range |
| **Construction site** (mixed) | PointPillars | `pointpillars_kitti` | 0-70m forward | Outdoor scale, sparse objects |

## What Happens When You Choose Wrong

| Scenario | Result | Root Cause |
|----------|--------|-----------|
| VoteNet on outdoor data | Very low confidence, if it detects anything | Ball query radius (0.2-1.2m) is tiny vs 100m scene. Seeds never reach targets. |
| PointPillars on indoor data | May work but suboptimal | Voxel grid at 0.16m resolution is too coarse for room-scale objects |
| Any model, wrong coord frame | Boxes and points do not line up, so neither detection nor training gives usable results | Points and boxes in different coordinate systems |

## Key Architectural Differences

### VoteNet (Indoor)
- **Input**: Raw points (20K sampled)
- **Backbone**: PointNet2 with Set Abstraction (ball query at 0.2, 0.4, 0.8, 1.2m radius)
- **Detection**: Voting mechanism — seeds propose object centers
- **Limitation**: Ball query radius is fixed. Objects >2m from any seed are invisible.
- **Sweet spot**: Rooms, offices, apartments — objects within 5m of sensor

### PointPillars (Outdoor/KITTI)
- **Input**: Voxelized pillars (0.16m × 0.16m × 4m columns)
- **Backbone**: PillarFeatureNet → BEV scatter → 2D CNN
- **Detection**: Anchor-based BEV detection
- **Limitation**: Range is fixed by point_cloud_range config. Objects outside are clipped.
- **Sweet spot**: Roads, highways, parking lots — objects within 70m forward, ±40m lateral

### CenterPoint (Outdoor/nuScenes)
- **Input**: 3D voxels (0.1m × 0.1m × 0.2m)
- **Backbone**: 3D Sparse Convolutions → BEV → CenterHead
- **Detection**: Center heatmap — anchor-free, predicts centers directly
- **Limitation**: Requires spconv (sparse convolution library). Heavier than PointPillars.
- **Sweet spot**: Dense urban scenes with many object types, multi-class detection

## Decision Flowchart

```
Is your data from an indoor scanner (room-scale, <10m)?
  └─ YES → Use VoteNet (votenet_scannet base)
  └─ NO → Is it outdoor LiDAR (driving, highway, construction)?
       └─ YES → Use PointPillars (pointpillars_kitti base)
       └─ NO → Is it aerial/terrestrial survey (large area)?
            └─ YES → Use PointPillars with custom point_cloud_range
```

## Training Scripts

| Architecture | Train Script | Inference Script |
|-------------|-------------|-----------------|
| VoteNet | `python -m pointcloud_detect.training.mmdet3d_trainer --config <yaml>` | `python scripts/run_finetuned_inference.py --trust-checkpoint` |
| PointPillars | `python scripts/finetune_centerpoint.py --data-dir <dir> --classes <cls>` | `python scripts/run_centerpoint_inference.py --trust-checkpoint` |

## Custom Class Handling (mmdet3d 1.4.0)

mmdet3d dataset classes (ScanNetDataset, KittiDataset, NuScenesDataset) have hardcoded class lists in `METAINFO['classes']`. To use custom classes:

```python
# Monkey-patch before training
from mmdet3d.datasets import ScanNetDataset  # or KittiDataset, NuScenesDataset
ScanNetDataset.METAINFO = {"classes": tuple(your_custom_classes)}
```

For KittiDataset, also override `parse_ann_info` to bypass camera calibration requirements when using LiDAR-only data.
