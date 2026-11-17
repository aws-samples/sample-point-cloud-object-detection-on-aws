"""Convert SUSTechPOINTS annotations to unified fine-tuning format.

SUSTechPOINTS saves annotations as JSON in:
    tools/SUSTechPOINTS/data/<scene>/label/<frame>.json

Each annotation has:
    {
        "obj_type": "door",
        "psr": {
            "position": {"x": ..., "y": ..., "z": ...},
            "scale": {"x": ..., "y": ..., "z": ...},
            "rotation": {"x": ..., "y": ..., "z": ...}
        }
    }

This script converts to our unified format:
    {
        "scene_id": "...",
        "point_cloud_path": "...",
        "annotations": [
            {"center_xyz": [...], "size_wlh": [...], "heading": float, "class_name": "..."}
        ]
    }

Usage:
    python scripts/convert-sustechpoints.py <scene_name> [--output-dir data/annotations/custom]
    python scripts/convert-sustechpoints.py --all  # Convert all annotated scenes

Example:
    python scripts/convert-sustechpoints.py livox_office_001
"""

import argparse
import json
from pathlib import Path


def convert_scene(scene_name: str, output_dir: Path) -> dict | None:
    """Convert a single scene's annotations."""
    scene_dir = Path("tools/SUSTechPOINTS/data") / scene_name
    label_dir = scene_dir / "label"

    if not label_dir.exists():
        print(f"  No labels found for scene: {scene_name}")
        return None

    # Find the PCD/PLY source file
    lidar_dir = scene_dir / "lidar"
    pcd_files = list(lidar_dir.glob("*.pcd")) if lidar_dir.exists() else []
    point_cloud_path = str(pcd_files[0]) if pcd_files else ""

    # Parse all label frames
    all_annotations = []
    for label_file in sorted(label_dir.glob("*.json")):
        with open(label_file, encoding="utf-8") as f:
            frame_labels = json.load(f)

        for obj in frame_labels:
            obj_type = obj.get("obj_type", "unknown")
            psr = obj.get("psr", {})
            position = psr.get("position", {})
            scale = psr.get("scale", {})
            rotation = psr.get("rotation", {})

            center = [
                position.get("x", 0.0),
                position.get("y", 0.0),
                position.get("z", 0.0),
            ]
            size = [
                scale.get("x", 0.0),
                scale.get("y", 0.0),
                scale.get("z", 0.0),
            ]
            # SUSTechPOINTS uses rotation.z as yaw (heading around Z-axis)
            heading = rotation.get("z", 0.0)

            all_annotations.append(
                {
                    "center_xyz": center,
                    "size_wlh": size,
                    "heading": heading,
                    "class_name": obj_type,
                }
            )

    if not all_annotations:
        print(f"  No annotations in scene: {scene_name}")
        return None

    annotation = {
        "scene_id": scene_name,
        "point_cloud_path": point_cloud_path,
        "num_annotations": len(all_annotations),
        "annotations": all_annotations,
    }

    # Save
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{scene_name}.json"
    output_file.write_text(json.dumps(annotation, indent=2))
    print(f"  {scene_name}: {len(all_annotations)} annotations → {output_file}")

    return annotation


def main():
    parser = argparse.ArgumentParser(description="Convert SUSTechPOINTS annotations")
    parser.add_argument("scene_name", nargs="?", help="Scene name to convert")
    parser.add_argument(
        "--all", action="store_true", help="Convert all annotated scenes"
    )
    parser.add_argument(
        "--output-dir", default="data/annotations/custom", help="Output directory"
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    if args.all:
        data_dir = Path("tools/SUSTechPOINTS/data")
        if not data_dir.exists():
            print(
                "Error: tools/SUSTechPOINTS/data not found. Run setup-sustechpoints.sh first."
            )
            return

        scenes = [
            d.name for d in data_dir.iterdir() if d.is_dir() and (d / "label").exists()
        ]
        if not scenes:
            print("No annotated scenes found.")
            return

        print(f"Converting {len(scenes)} annotated scenes...")
        total = 0
        for scene in sorted(scenes):
            result = convert_scene(scene, output_dir)
            if result:
                total += result["num_annotations"]
        print(f"\nTotal: {total} annotations across {len(scenes)} scenes")

    elif args.scene_name:
        convert_scene(args.scene_name, output_dir)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
