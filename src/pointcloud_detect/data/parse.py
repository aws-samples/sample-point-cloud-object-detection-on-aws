"""Dataset parsing utilities for fine-tuning.

Converts raw dataset formats into the unified annotation format:
    {
        "scene_id": "...",
        "point_cloud_path": "...",
        "annotations": [
            {
                "center_xyz": [x, y, z],
                "size_wlh": [w, l, h],
                "heading": float,
                "class_name": "..."
            }
        ]
    }

Supports:
- KITTI 3D Object Detection: .bin point clouds + label_2 .txt annotations
- ScanNet v2: .ply meshes + _bbox.npy axis-aligned bounding boxes
- S3DIS: per-class annotation .txt files (segmentation labels, bboxes derived)
"""

import json
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

KITTI_CLASSES = {
    "Car": "car",
    "Van": "van",
    "Truck": "truck",
    "Pedestrian": "pedestrian",
    "Person_sitting": "person_sitting",
    "Cyclist": "cyclist",
    "Tram": "tram",
    "Misc": "misc",
}

SCANNET_CLASSES = [
    "cabinet",
    "bed",
    "chair",
    "sofa",
    "table",
    "door",
    "window",
    "bookshelf",
    "picture",
    "counter",
    "desk",
    "curtain",
    "refrigerator",
    "shower curtain",
    "toilet",
    "sink",
    "bathtub",
    "otherfurniture",
]

S3DIS_CLASSES = [
    "ceiling",
    "floor",
    "wall",
    "beam",
    "column",
    "window",
    "door",
    "table",
    "chair",
    "sofa",
    "bookcase",
    "board",
    "clutter",
]


def _write_annotation(output_path: Path, annotation: dict) -> None:
    """Write annotation dict to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(annotation, f, indent=2)


def parse_kitti_label(label_path: Path) -> list[dict]:
    """Parse a single KITTI label_2 .txt file into annotation boxes.

    KITTI label format (per line, space-separated):
        type truncated occluded alpha
        bbox_2d(4) dimensions(3: h,w,l) location(3: x,y,z) rotation_y

    The 3D box is in camera coordinates. We convert to LiDAR frame:
        x_lidar = z_cam, y_lidar = -x_cam, z_lidar = -y_cam + h/2

    Args:
        label_path: Path to a label_2/*.txt file.

    Returns:
        List of annotation dicts with center_xyz, size_wlh, heading, class_name.
    """
    annotations: list[dict] = []

    with open(label_path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 15:
                continue

            obj_type = parts[0]
            if obj_type == "DontCare":
                continue

            class_name = KITTI_CLASSES.get(obj_type)
            if class_name is None:
                continue

            h = float(parts[8])
            w = float(parts[9])
            l = float(parts[10])  # noqa: E741
            x_cam = float(parts[11])
            y_cam = float(parts[12])
            z_cam = float(parts[13])
            rotation_y = float(parts[14])

            # Camera to LiDAR coordinate transform (KITTI convention)
            x_lidar = z_cam
            y_lidar = -x_cam
            z_lidar = -y_cam + h / 2.0

            # Heading: rotation_y is around camera Y (down), which maps
            # to rotation around LiDAR Z (up) with sign flip
            heading = -rotation_y

            annotations.append(
                {
                    "center_xyz": [x_lidar, y_lidar, z_lidar],
                    "size_wlh": [w, l, h],
                    "heading": heading,
                    "class_name": class_name,
                }
            )

    return annotations


def parse_kitti_scene(
    velodyne_path: Path,
    label_path: Path,
) -> dict:
    """Parse a single KITTI scene (point cloud + labels).

    Args:
        velodyne_path: Path to .bin file (N x 4: x, y, z, reflectance).
        label_path: Path to corresponding label_2 .txt file.

    Returns:
        Unified annotation dict for this scene.
    """
    annotations = parse_kitti_label(label_path)
    scene_id = velodyne_path.stem

    return {
        "scene_id": scene_id,
        "point_cloud_path": str(velodyne_path),
        "num_annotations": len(annotations),
        "annotations": annotations,
    }


def parse_kitti_dataset(
    kitti_root: Path,
    output_dir: Path | None = None,
    split: str = "training",
) -> list[dict]:
    """Parse the full KITTI 3D Object Detection dataset.

    Args:
        kitti_root: Root of KITTI dataset (contains training/ directory).
        output_dir: If provided, write per-scene JSON annotations here.
        split: Dataset split ("training" or "testing").

    Returns:
        List of unified annotation dicts, one per scene.
    """
    velodyne_dir = kitti_root / split / "velodyne"
    label_dir = kitti_root / split / "label_2"

    if not velodyne_dir.exists():
        print(f"  [error] Velodyne directory not found: {velodyne_dir}")
        return []

    bin_files = sorted(velodyne_dir.glob("*.bin"))
    print(f"  Found {len(bin_files)} point clouds in {split}")

    results: list[dict] = []
    for bin_file in bin_files:
        label_file = label_dir / f"{bin_file.stem}.txt"
        if not label_file.exists():
            continue

        annotation = parse_kitti_scene(bin_file, label_file)
        results.append(annotation)

        if output_dir:
            out_path = output_dir / f"{bin_file.stem}.json"
            _write_annotation(out_path, annotation)

    print(f"  Parsed {len(results)} scenes with annotations")
    return results


def parse_scannet_bbox(bbox_path: Path) -> list[dict]:
    """Parse a ScanNet _bbox.npy file into annotation boxes.

    ScanNet bbox format: Nx7 array where each row is:
        [cx, cy, cz, dx, dy, dz, class_id]
    Boxes are axis-aligned (heading = 0).

    Args:
        bbox_path: Path to scene_bbox.npy file.

    Returns:
        List of annotation dicts.
    """
    if not bbox_path.exists():
        return []

    bboxes = np.load(bbox_path)
    if bboxes.ndim != 2 or bboxes.shape[1] < 7:
        return []

    annotations: list[dict] = []
    for row in bboxes:
        cx, cy, cz = float(row[0]), float(row[1]), float(row[2])
        dx, dy, dz = float(row[3]), float(row[4]), float(row[5])
        class_id = int(row[6])

        if 0 <= class_id < len(SCANNET_CLASSES):
            class_name = SCANNET_CLASSES[class_id]
        else:
            class_name = f"unknown_{class_id}"

        annotations.append(
            {
                "center_xyz": [cx, cy, cz],
                "size_wlh": [dx, dy, dz],
                "heading": 0.0,
                "class_name": class_name,
            }
        )

    return annotations


def parse_scannet_scene(scene_dir: Path) -> dict:
    """Parse a single ScanNet scene directory.

    Expected files:
        scene_dir/sceneXXXX_YY_vh_clean_2.ply  (point cloud)
        scene_dir/sceneXXXX_YY_bbox.npy         (bounding boxes)

    Args:
        scene_dir: Path to a scene directory (e.g., scans/scene0000_00/).

    Returns:
        Unified annotation dict for this scene.
    """
    scene_id = scene_dir.name

    ply_files = list(scene_dir.glob("*_vh_clean_2.ply"))
    point_cloud_path = str(ply_files[0]) if ply_files else ""

    bbox_files = list(scene_dir.glob("*_bbox.npy"))
    annotations: list[dict] = []
    if bbox_files:
        annotations = parse_scannet_bbox(bbox_files[0])

    return {
        "scene_id": scene_id,
        "point_cloud_path": point_cloud_path,
        "num_annotations": len(annotations),
        "annotations": annotations,
    }


def parse_scannet_dataset(
    scannet_root: Path,
    output_dir: Path | None = None,
) -> list[dict]:
    """Parse the full ScanNet dataset.

    Args:
        scannet_root: Root of ScanNet dataset (contains scans/ directory).
        output_dir: If provided, write per-scene JSON annotations here.

    Returns:
        List of unified annotation dicts, one per scene.
    """
    scans_dir = scannet_root / "scans"
    if not scans_dir.exists():
        print(f"  [error] Scans directory not found: {scans_dir}")
        return []

    scene_dirs = sorted(
        d for d in scans_dir.iterdir() if d.is_dir() and d.name.startswith("scene")
    )
    print(f"  Found {len(scene_dirs)} scenes")

    results: list[dict] = []
    for scene_dir in scene_dirs:
        annotation = parse_scannet_scene(scene_dir)
        if annotation["num_annotations"] > 0 or annotation["point_cloud_path"]:
            results.append(annotation)

            if output_dir:
                out_path = output_dir / f"{scene_dir.name}.json"
                _write_annotation(out_path, annotation)

    print(f"  Parsed {len(results)} scenes with data")
    return results


def _compute_bbox_from_points(
    points: NDArray[np.float64],
) -> tuple[list[float], list[float]]:
    """Compute axis-aligned bounding box center and size from points."""
    min_pt = points.min(axis=0)
    max_pt = points.max(axis=0)
    center = ((min_pt + max_pt) / 2.0).tolist()
    size = (max_pt - min_pt).tolist()
    return center, size


def parse_s3dis_room(room_dir: Path) -> list[dict]:
    """Parse a single S3DIS room's annotation files into bounding boxes.

    S3DIS annotations are per-class point clouds in room/Annotations/*.txt.
    Each txt file contains XYZ RGB points belonging to one object instance.
    We derive axis-aligned bounding boxes from each instance's point extent.

    Args:
        room_dir: Path to a room directory (e.g., Area_1/office_1/).

    Returns:
        List of annotation dicts (one per object instance).
    """
    annotations_dir = room_dir / "Annotations"
    if not annotations_dir.exists():
        return []

    annotations: list[dict] = []
    for ann_file in sorted(annotations_dir.glob("*.txt")):
        # Filename format: classname_instancenum.txt (e.g., chair_1.txt)
        name_parts = ann_file.stem.rsplit("_", 1)
        if len(name_parts) < 2:
            continue

        class_name = name_parts[0].lower()
        if class_name not in S3DIS_CLASSES:
            continue

        try:
            points = np.loadtxt(ann_file, dtype=np.float64)
        except (ValueError, OSError):
            continue

        if points.ndim != 2 or points.shape[0] < 2:
            continue

        # Use only XYZ columns
        xyz = points[:, :3]
        center, size = _compute_bbox_from_points(xyz)

        # Pad to 3D if needed
        while len(center) < 3:
            center.append(0.0)
        while len(size) < 3:
            size.append(0.0)

        annotations.append(
            {
                "center_xyz": center,
                "size_wlh": size,
                "heading": 0.0,
                "class_name": class_name,
            }
        )

    return annotations


def parse_s3dis_area(area_dir: Path) -> list[dict]:
    """Parse all rooms in an S3DIS area.

    Args:
        area_dir: Path to an area directory (e.g., Area_1/).

    Returns:
        List of unified annotation dicts, one per room.
    """
    results: list[dict] = []

    room_dirs = sorted(
        d for d in area_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )

    for room_dir in room_dirs:
        room_cloud = room_dir / f"{room_dir.name}.txt"
        point_cloud_path = str(room_cloud) if room_cloud.exists() else ""

        annotations = parse_s3dis_room(room_dir)
        if annotations or point_cloud_path:
            results.append(
                {
                    "scene_id": f"{area_dir.name}/{room_dir.name}",
                    "point_cloud_path": point_cloud_path,
                    "num_annotations": len(annotations),
                    "annotations": annotations,
                }
            )

    return results


def parse_s3dis_dataset(
    s3dis_root: Path,
    output_dir: Path | None = None,
) -> list[dict]:
    """Parse the full S3DIS dataset.

    Args:
        s3dis_root: Root of S3DIS dataset (contains Area_1/ through Area_6/).
        output_dir: If provided, write per-room JSON annotations here.

    Returns:
        List of unified annotation dicts, one per room.
    """
    area_dirs = sorted(
        d for d in s3dis_root.iterdir() if d.is_dir() and d.name.startswith("Area_")
    )

    if not area_dirs:
        print("  [error] No Area_* directories found")
        return []

    print(f"  Found {len(area_dirs)} areas")
    results: list[dict] = []

    for area_dir in area_dirs:
        area_results = parse_s3dis_area(area_dir)
        results.extend(area_results)
        print(f"    {area_dir.name}: {len(area_results)} rooms")

        if output_dir:
            for annotation in area_results:
                scene_id = annotation["scene_id"].replace("/", "_")
                out_path = output_dir / f"{scene_id}.json"
                _write_annotation(out_path, annotation)

    print(f"  Parsed {len(results)} rooms total")
    return results


def parse_all(
    data_root: Path,
    output_root: Path | None = None,
) -> dict[str, list[dict]]:
    """Parse all available datasets into unified annotation format.

    Args:
        data_root: Root data directory (contains kitti/, scannet/, s3dis/).
        output_root: If provided, write per-scene JSON annotations here.

    Returns:
        Dict mapping dataset name to list of annotation dicts.
    """
    results: dict[str, list[dict]] = {}

    kitti_root = data_root / "kitti"
    if kitti_root.exists():
        print("\n--- Parsing KITTI ---")
        out_dir = output_root / "kitti" if output_root else None
        results["kitti"] = parse_kitti_dataset(kitti_root, out_dir)

    scannet_root = data_root / "scannet"
    if scannet_root.exists():
        print("\n--- Parsing ScanNet ---")
        out_dir = output_root / "scannet" if output_root else None
        results["scannet"] = parse_scannet_dataset(scannet_root, out_dir)

    s3dis_root = data_root / "s3dis"
    if s3dis_root.exists():
        print("\n--- Parsing S3DIS ---")
        out_dir = output_root / "s3dis" if output_root else None
        results["s3dis"] = parse_s3dis_dataset(s3dis_root, out_dir)

    return results
