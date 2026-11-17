#!/usr/bin/env python3
"""Convert DALES/DALES-2 aerial LiDAR dataset to unified fine-tuning format.

DALES-2 is a large-scale aerial (drone) LiDAR dataset with semantic + instance labels.
15 classes including vehicles, buildings, poles, fences, powerlines.

Prerequisites:
    pip install laspy huggingface_hub

    # Download DALES-2 from Hugging Face (MIT license, 2.77GB):
    python -c "
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id='mbendjilali/DALES-2', repo_type='dataset', local_dir='data/dales2')
    "

Usage:
    # Convert all vehicle instances (cars, trucks, vans):
    python scripts/convert-dales-aerial.py \
        --dales-root data/dales2 \
        --output-dir data/annotations/dales_vehicles \
        --classes car pickup van_truck heavy_duty

    # Convert just cars:
    python scripts/convert-dales-aerial.py \
        --dales-root data/dales2 \
        --output-dir data/annotations/dales_cars \
        --classes car

    # Convert buildings:
    python scripts/convert-dales-aerial.py \
        --dales-root data/dales2 \
        --output-dir data/annotations/dales_buildings \
        --classes habitat complex annex

    # Convert all instances (vehicles + poles + buildings):
    python scripts/convert-dales-aerial.py \
        --dales-root data/dales2 \
        --output-dir data/annotations/dales_all \
        --classes car pickup van_truck heavy_duty utility_pole light_pole traffic_pole habitat complex annex

Output format (unified annotation JSON):
    {
        "scene_id": "dales2_tile_5080_54435_crop_00",
        "point_cloud_path": "points/dales2_tile_5080_54435_crop_00.bin",
        "num_annotations": 12,
        "annotations": [
            {"center_xyz": [...], "size_wlh": [...], "heading": 0.0, "class_name": "car"}
        ]
    }

Notes:
    - DALES-2 covers 10km² at ~20 pts/m² (aerial perspective, Z = altitude)
    - Each tile is large (500m×500m). We crop into smaller windows for training.
    - Instance IDs provide per-object segmentation; we compute 3D AABBs from them.
    - Coordinate frame: local UTM (meters), Z-up. Compatible with PointPillars.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# DALES-2 class mapping (from dataset card)
DALES2_CLASSES = {
    0: "ground",
    1: "vegetation",
    2: "car",
    3: "powerline",
    4: "fence",
    5: "tree",
    6: "pickup",
    7: "van_truck",
    8: "heavy_duty",
    9: "utility_pole",
    10: "light_pole",
    11: "traffic_pole",
    12: "habitat",  # residential building
    13: "complex",  # commercial/industrial building
    14: "annex",  # outbuilding/garage
}

# Reverse map
CLASS_NAME_TO_ID = {v: k for k, v in DALES2_CLASSES.items()}


def load_las_file(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load a LAS/LAZ file, return (xyz, classification, instance_id)."""
    try:
        import laspy
    except ImportError:
        print("ERROR: laspy not installed. Run: pip install laspy")
        sys.exit(1)

    las = laspy.read(str(path))
    # las.x/y/z are laspy scaled-array views rather than plain ndarrays, so each
    # is materialised before stacking.
    xyz = np.column_stack(
        [np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)]
    ).astype(np.float64)

    # Classification (semantic label)
    classification = np.array(las.classification, dtype=np.int32)

    # Instance ID — DALES-2 stores this in an extra dimension
    instance = np.zeros(len(xyz), dtype=np.int32)
    # extra_dims is not part of PointFormat's public typed surface;
    # extra_dimension_names is, and yields the same names directly.
    if hasattr(las, "instance") or "instance" in list(
        las.point_format.extra_dimension_names
    ):
        instance = np.array(las["instance"], dtype=np.int32)
    elif hasattr(las, "user_data"):
        # Some versions use user_data for instance
        instance = np.array(las.user_data, dtype=np.int32)

    return xyz, classification, instance


def extract_bboxes_from_instances(
    xyz: np.ndarray,
    classification: np.ndarray,
    instance: np.ndarray,
    target_class_ids: list[int],
    min_points: int = 20,
) -> list[dict]:
    """Extract 3D bounding boxes from instance-labeled points."""
    boxes = []

    for class_id in target_class_ids:
        class_mask = classification == class_id
        if class_mask.sum() == 0:
            continue

        class_instances = np.unique(instance[class_mask])
        # Skip instance 0 (stuff/unlabeled)
        class_instances = class_instances[class_instances > 0]

        for inst_id in class_instances:
            inst_mask = class_mask & (instance == inst_id)
            pts = xyz[inst_mask]

            if len(pts) < min_points:
                continue

            # Compute AABB
            min_pt = pts.min(axis=0)
            max_pt = pts.max(axis=0)
            center = ((min_pt + max_pt) / 2).tolist()
            size = (max_pt - min_pt).tolist()

            # Skip degenerate boxes (flat or tiny)
            if min(size) < 0.1 or max(size) > 200:
                continue

            class_name = DALES2_CLASSES[class_id]
            boxes.append(
                {
                    "center_xyz": [round(c, 3) for c in center],
                    "size_wlh": [round(s, 3) for s in size],
                    "heading": 0.0,  # Aerial view — no heading estimation
                    "class_name": class_name,
                }
            )

    return boxes


def crop_tile(
    xyz: np.ndarray,
    classification: np.ndarray,
    instance: np.ndarray,
    crop_size: float = 100.0,
    stride: float = 80.0,
    min_points: int = 1000,
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Crop a large tile into smaller windows for training.

    Returns list of (xyz_local, classification, instance, centroid).
    """
    x_min, y_min = xyz[:, 0].min(), xyz[:, 1].min()
    x_max, y_max = xyz[:, 0].max(), xyz[:, 1].max()

    crops = []
    x = x_min
    while x + crop_size <= x_max + stride:
        y = y_min
        while y + crop_size <= y_max + stride:
            mask = (
                (xyz[:, 0] >= x)
                & (xyz[:, 0] < x + crop_size)
                & (xyz[:, 1] >= y)
                & (xyz[:, 1] < y + crop_size)
            )
            if mask.sum() >= min_points:
                crop_xyz = xyz[mask].copy()
                # Center the crop (local coordinates)
                centroid = np.array(
                    [x + crop_size / 2, y + crop_size / 2, crop_xyz[:, 2].mean()]
                )
                crop_xyz -= centroid
                crops.append(
                    (
                        crop_xyz.astype(np.float32),
                        classification[mask],
                        instance[mask],
                        centroid,
                    )
                )
            y += stride
        x += stride

    return crops


def main():
    parser = argparse.ArgumentParser(
        description="Convert DALES-2 aerial LiDAR to unified format"
    )
    parser.add_argument(
        "--dales-root",
        required=True,
        help="Path to DALES-2 dataset root (contains .las/.laz files)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/annotations/dales_vehicles",
        help="Output directory for unified annotations",
    )
    parser.add_argument(
        "--classes",
        nargs="+",
        default=["car"],
        help="Classes to extract (default: car). Options: car, pickup, van_truck, heavy_duty, "
        "utility_pole, light_pole, traffic_pole, habitat, complex, annex, fence, powerline",
    )
    parser.add_argument(
        "--crop-size",
        type=float,
        default=100.0,
        help="Crop window size in meters (default: 100m)",
    )
    parser.add_argument(
        "--stride",
        type=float,
        default=80.0,
        help="Crop stride in meters (default: 80m, gives 20m overlap)",
    )
    parser.add_argument(
        "--max-tiles",
        type=int,
        default=None,
        help="Max number of tiles to process (for testing)",
    )
    parser.add_argument(
        "--min-points-instance",
        type=int,
        default=20,
        help="Min points per instance to include (default: 20)",
    )
    parser.add_argument(
        "--export-points",
        action="store_true",
        help="Export point cloud .bin files for training",
    )
    args = parser.parse_args()

    dales_root = Path(args.dales_root)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.export_points:
        (out_dir / "points").mkdir(parents=True, exist_ok=True)

    # Find LAS/LAZ files
    las_files = sorted(dales_root.glob("**/*.la[sz]"))
    if not las_files:
        las_files = sorted(dales_root.glob("**/*.LAS")) + sorted(
            dales_root.glob("**/*.LAZ")
        )
    if not las_files:
        print(f"ERROR: No LAS/LAZ files found in {dales_root}")
        print(
            f'  Download with: python -c "from huggingface_hub import snapshot_download; '
            f"snapshot_download(repo_id='mbendjilali/DALES-2', repo_type='dataset', local_dir='{dales_root}')\""
        )
        sys.exit(1)

    if args.max_tiles:
        las_files = las_files[: args.max_tiles]

    # Resolve target class IDs
    target_class_ids = []
    for cls_name in args.classes:
        if cls_name not in CLASS_NAME_TO_ID:
            print(
                f"WARNING: Unknown class '{cls_name}'. Available: {list(CLASS_NAME_TO_ID.keys())}"
            )
            continue
        target_class_ids.append(CLASS_NAME_TO_ID[cls_name])

    if not target_class_ids:
        print("ERROR: No valid target classes specified")
        sys.exit(1)

    print(f"[dales] Processing {len(las_files)} tiles")
    print(f"[dales] Target classes: {args.classes} (IDs: {target_class_ids})")
    print(f"[dales] Crop: {args.crop_size}m, stride: {args.stride}m")

    total_scenes = 0
    total_boxes = 0

    for tile_idx, las_path in enumerate(las_files):
        print(f"\n[{tile_idx + 1}/{len(las_files)}] Loading {las_path.name}...")
        xyz, classification, instance = load_las_file(las_path)
        print(
            f"  {len(xyz):,} points, x=[{xyz[:, 0].min():.0f},{xyz[:, 0].max():.0f}] "
            f"y=[{xyz[:, 1].min():.0f},{xyz[:, 1].max():.0f}] z=[{xyz[:, 2].min():.1f},{xyz[:, 2].max():.1f}]"
        )

        # Check if any target instances exist in this tile
        has_targets = any((classification == cid).sum() > 0 for cid in target_class_ids)
        if not has_targets:
            print("  No target class instances found, skipping")
            continue

        # Crop tile into training-sized windows
        crops = crop_tile(xyz, classification, instance, args.crop_size, args.stride)
        print(f"  Cropped into {len(crops)} windows")

        for crop_idx, (crop_xyz, crop_cls, crop_inst, centroid) in enumerate(crops):
            # Extract bounding boxes from instances in this crop
            boxes = extract_bboxes_from_instances(
                crop_xyz,
                crop_cls,
                crop_inst,
                target_class_ids,
                args.min_points_instance,
            )

            if not boxes:
                continue

            scene_id = f"dales2_{las_path.stem}_crop_{crop_idx:02d}"

            # Export point cloud
            pc_path = ""
            if args.export_points:
                # Save as 5-dim: x, y, z, intensity (0), ring (0)
                out_pts = np.zeros((len(crop_xyz), 5), dtype=np.float32)
                out_pts[:, :3] = crop_xyz
                bin_name = f"{scene_id}.bin"
                out_pts.tofile(str(out_dir / "points" / bin_name))
                pc_path = f"points/{bin_name}"

            annotation = {
                "scene_id": scene_id,
                "point_cloud_path": pc_path,
                "num_annotations": len(boxes),
                "annotations": boxes,
            }

            json_path = out_dir / f"{scene_id}.json"
            json_path.write_text(json.dumps(annotation, indent=2))

            total_scenes += 1
            total_boxes += len(boxes)

    print(f"\n{'=' * 60}")
    print("CONVERSION COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Tiles processed: {len(las_files)}")
    print(f"  Training scenes: {total_scenes}")
    print(f"  Total bounding boxes: {total_boxes}")
    print(f"  Output: {out_dir}")
    print("\nNext steps:")
    print("  # Fine-tune PointPillars (outdoor architecture):")
    print("  python scripts/finetune_centerpoint.py \\")
    print(f"      --data-dir {out_dir} \\")
    print(f"      --classes {' '.join(args.classes)} \\")
    print("      --epochs 20 --lr 0.001 \\")
    print(f"      --output-dir models/dales_{args.classes[0]}")


if __name__ == "__main__":
    main()
