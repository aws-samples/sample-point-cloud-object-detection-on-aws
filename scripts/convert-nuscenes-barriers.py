#!/usr/bin/env python3
"""Convert nuScenes 'barrier' annotations to unified fine-tuning format.

Downloads relevant scenes from nuScenes (or uses local data) and extracts
barrier/guardrail 3D bounding boxes into our annotation format for fine-tuning.

Prerequisites:
    pip install nuscenes-devkit pyquaternion

    Download nuScenes dataset (mini or full):
        https://www.nuscenes.org/download
    Extract to a directory, e.g.:
        /mnt/efs/datasets/nuscenes/

    Directory structure expected:
        <nuscenes_root>/
            maps/
            samples/
            sweeps/
            v1.0-mini/  (or v1.0-trainval/)

Usage:
    # Convert all barrier annotations from nuScenes mini:
    python scripts/convert-nuscenes-barriers.py \
        --nuscenes-root /mnt/efs/datasets/nuscenes \
        --version v1.0-mini \
        --output-dir data/annotations/barriers

    # Convert with additional classes (e.g., traffic_cone as bollard):
    python scripts/convert-nuscenes-barriers.py \
        --nuscenes-root /mnt/efs/datasets/nuscenes \
        --version v1.0-mini \
        --output-dir data/annotations/road_objects \
        --classes barrier traffic_cone

    # Limit number of scenes (for quick testing):
    python scripts/convert-nuscenes-barriers.py \
        --nuscenes-root /mnt/efs/datasets/nuscenes \
        --version v1.0-mini \
        --output-dir data/annotations/barriers \
        --max-scenes 10

    # Also export point clouds as .bin files for training:
    python scripts/convert-nuscenes-barriers.py \
        --nuscenes-root /mnt/efs/datasets/nuscenes \
        --version v1.0-mini \
        --output-dir data/annotations/barriers \
        --export-points

Output format (per scene JSON, matches our unified format):
    {
        "scene_id": "nuscenes_scene_0001_frame_00",
        "point_cloud_path": "points/nuscenes_scene_0001_frame_00.bin",
        "num_annotations": 5,
        "annotations": [
            {
                "center_xyz": [x, y, z],
                "size_wlh": [w, l, h],
                "heading": float,
                "class_name": "guardrail"
            }
        ]
    }
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# nuScenes class name -> our unified class name mapping
CLASS_MAP = {
    "barrier": "guardrail",
    "traffic_cone": "traffic_cone",
    "movable_object.barrier": "guardrail",
    "movable_object.trafficcone": "traffic_cone",
}


def quaternion_to_yaw(q: list[float]) -> float:
    """Convert quaternion [w, x, y, z] to yaw angle (rotation around Z)."""
    w, x, y, z = q
    # yaw = atan2(2*(w*z + x*y), 1 - 2*(y*y + z*z))
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = np.arctan2(siny_cosp, cosy_cosp)
    return float(yaw)


def convert_nuscenes(
    nuscenes_root: str,
    version: str,
    output_dir: str,
    target_classes: list[str],
    max_scenes: int | None,
    export_points: bool,
    min_points_in_box: int,
) -> None:
    """Convert nuScenes barrier annotations to unified format."""
    try:
        from nuscenes.nuscenes import NuScenes  # pyright: ignore[reportMissingImports]
    except ImportError:
        print("ERROR: nuscenes-devkit not installed.")
        print("  Install with: pip install nuscenes-devkit pyquaternion")
        sys.exit(1)

    print(f"[nuscenes] Loading {version} from {nuscenes_root}...")
    nusc = NuScenes(version=version, dataroot=nuscenes_root, verbose=False)
    print(
        f"[nuscenes] Loaded: {len(nusc.scene)} scenes, {len(nusc.sample_annotation)} annotations"
    )

    # Build set of target category tokens
    target_category_tokens = set()
    for cat in nusc.category:
        cat_name = cat["name"]
        # Match both top-level ("barrier") and full name ("movable_object.barrier")
        for target in target_classes:
            if target in cat_name or cat_name in CLASS_MAP:
                target_category_tokens.add(cat["token"])
                print(f"  [class] {cat_name} -> {CLASS_MAP.get(cat_name, target)}")

    if not target_category_tokens:
        print(f"ERROR: No matching categories found for {target_classes}")
        print(f"  Available: {[c['name'] for c in nusc.category]}")
        sys.exit(1)

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    points_dir = out_path / "points"
    if export_points:
        points_dir.mkdir(parents=True, exist_ok=True)

    total_scenes = 0
    total_annotations = 0
    scenes_with_barriers = 0

    scenes = nusc.scene[:max_scenes] if max_scenes else nusc.scene

    for scene in scenes:
        # Walk through keyframes in this scene
        sample_token = scene["first_sample_token"]
        frame_idx = 0

        while sample_token:
            sample = nusc.get("sample", sample_token)

            # Get LiDAR data for this sample
            lidar_token = sample["data"]["LIDAR_TOP"]
            lidar_data = nusc.get("sample_data", lidar_token)

            # Get annotations for this sample
            annotations = []

            # Get ego pose and sensor calibration to transform global -> sensor frame
            sd_record = nusc.get("sample_data", lidar_token)
            cs_record = nusc.get(
                "calibrated_sensor", sd_record["calibrated_sensor_token"]
            )
            pose_record = nusc.get("ego_pose", sd_record["ego_pose_token"])

            from pyquaternion import Quaternion as PQ

            ego_translation = np.array(pose_record["translation"])
            ego_rotation = PQ(pose_record["rotation"])
            sensor_translation = np.array(cs_record["translation"])
            sensor_rotation = PQ(cs_record["rotation"])

            for ann_token in sample["anns"]:
                ann = nusc.get("sample_annotation", ann_token)

                # Resolve category name — handle different devkit versions
                if "category_name" in ann:
                    cat_name = ann["category_name"]
                elif "category_token" in ann:
                    cat_name = nusc.get("category", ann["category_token"])["name"]
                else:
                    continue

                # Check if this is a target class
                matched_class = None
                for target in target_classes:
                    if target in cat_name:
                        matched_class = CLASS_MAP.get(cat_name, "guardrail")
                        break
                if not matched_class:
                    continue

                # Skip annotations with very few LiDAR points
                if ann.get("num_lidar_pts", 0) < min_points_in_box:
                    continue

                # Transform center from global frame to sensor (LiDAR) frame
                center_global = np.array(ann["translation"])
                center_ego = ego_rotation.inverse.rotate(
                    center_global - ego_translation
                )
                center_sensor = sensor_rotation.inverse.rotate(
                    center_ego - sensor_translation
                )

                # Transform rotation to sensor-relative yaw
                ann_rotation = PQ(ann["rotation"])
                relative_rotation = (
                    sensor_rotation.inverse * ego_rotation.inverse * ann_rotation
                )
                yaw = relative_rotation.yaw_pitch_roll[0]

                size = ann["size"]  # [w, l, h]
                class_name = matched_class

                annotations.append(
                    {
                        "center_xyz": [
                            float(center_sensor[0]),
                            float(center_sensor[1]),
                            float(center_sensor[2]),
                        ],
                        "size_wlh": [float(size[0]), float(size[1]), float(size[2])],
                        "heading": float(yaw),
                        "class_name": class_name,
                    }
                )

            if annotations:
                scene_id = f"nuscenes_{scene['name']}_frame_{frame_idx:02d}"

                # Export point cloud if requested
                point_cloud_path = ""
                if export_points:
                    pcl_path = Path(nuscenes_root) / lidar_data["filename"]
                    if pcl_path.exists():
                        # nuScenes LiDAR is in [x, y, z, intensity, ring_index] format
                        points = np.fromfile(str(pcl_path), dtype=np.float32).reshape(
                            -1, 5
                        )
                        # Save as 6-dim (xyz + pad with zeros for rgb) to match our pipeline
                        out_points = np.zeros((len(points), 6), dtype=np.float32)
                        out_points[:, :3] = points[:, :3]
                        out_points[:, 3] = points[:, 3]  # intensity in R channel
                        bin_name = f"{scene_id}.bin"
                        out_points.tofile(str(points_dir / bin_name))
                        point_cloud_path = f"points/{bin_name}"

                unified = {
                    "scene_id": scene_id,
                    "point_cloud_path": point_cloud_path,
                    "num_annotations": len(annotations),
                    "annotations": annotations,
                }

                # Save annotation JSON
                json_file = out_path / f"{scene_id}.json"
                json_file.write_text(json.dumps(unified, indent=2))

                scenes_with_barriers += 1
                total_annotations += len(annotations)

            total_scenes += 1
            frame_idx += 1
            sample_token = sample["next"]

    print(f"\n{'=' * 60}")
    print("CONVERSION COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Scenes processed: {total_scenes}")
    print(f"  Scenes with barriers: {scenes_with_barriers}")
    print(f"  Total barrier annotations: {total_annotations}")
    print(f"  Output directory: {out_path}")
    if export_points:
        print(f"  Point clouds exported to: {points_dir}")
    print("\nNext steps:")
    print("  1. Create training config (copy configs/finetune_doors_livox.yaml)")
    print(f"  2. Update data_path to: {out_path}")
    print("  3. Update custom_classes to: ['guardrail']")
    print("  4. Run training:")
    print(
        "     python -m pointcloud_detect.training.mmdet3d_trainer --config configs/finetune_guardrails.yaml"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Convert nuScenes barrier annotations to unified format"
    )
    parser.add_argument(
        "--nuscenes-root",
        required=True,
        help="Path to nuScenes dataset root (contains maps/, samples/, v1.0-*/)",
    )
    parser.add_argument(
        "--version",
        default="v1.0-mini",
        help="nuScenes version (v1.0-mini, v1.0-trainval, v1.0-test)",
    )
    parser.add_argument(
        "--output-dir",
        default="data/annotations/barriers",
        help="Output directory for unified annotations",
    )
    parser.add_argument(
        "--classes",
        nargs="+",
        default=["barrier"],
        help="nuScenes classes to extract (default: barrier)",
    )
    parser.add_argument(
        "--max-scenes",
        type=int,
        default=None,
        help="Maximum number of scenes to process (for testing)",
    )
    parser.add_argument(
        "--export-points",
        action="store_true",
        help="Also export point cloud .bin files for training",
    )
    parser.add_argument(
        "--min-points",
        type=int,
        default=5,
        help="Minimum LiDAR points in box to include annotation (default: 5)",
    )
    args = parser.parse_args()

    convert_nuscenes(
        nuscenes_root=args.nuscenes_root,
        version=args.version,
        output_dir=args.output_dir,
        target_classes=args.classes,
        max_scenes=args.max_scenes,
        export_points=args.export_points,
        min_points_in_box=args.min_points,
    )


if __name__ == "__main__":
    main()
