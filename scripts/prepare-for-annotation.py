"""Prepare point cloud files for annotation in SUSTechPOINTS.

SUSTechPOINTS expects data in:
    tools/SUSTechPOINTS/data/<scene_name>/lidar/<frame>.pcd

This script converts PLY, E57, LAS, and LAZ files to the expected format.
Supports multiple files in one command.

Usage:
    python scripts/prepare-for-annotation.py <input.ply>
    python scripts/prepare-for-annotation.py <input.ply> --scene-name my_scene
    python scripts/prepare-for-annotation.py file1.ply file2.e57 file3.las
    python scripts/prepare-for-annotation.py data/scans/*.ply --coord-frame livox

Example:
    python scripts/prepare-for-annotation.py my_livox_scan.ply
    python scripts/prepare-for-annotation.py scan1.ply scan2.e57 --max-points 500000
"""

import argparse
import struct
import sys
from pathlib import Path

import numpy as np


def load_point_cloud(input_path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Load a point cloud file (PLY, E57, LAS, LAZ). Returns (xyz, colors)."""
    suffix = input_path.suffix.lower()

    if suffix == ".ply":
        try:
            import trimesh
        except ImportError:
            print("Error: trimesh required. Install with: uv pip install trimesh")
            sys.exit(1)
        # trimesh.load is typed as returning the Geometry base class, which declares
        # neither vertices nor colors -- those are on the concrete PointCloud/Trimesh
        # subclasses, and which one comes back depends on the file. getattr keeps the
        # duck-typed access while giving the type checker something it can follow.
        cloud = trimesh.load(str(input_path), process=False)
        verts = getattr(cloud, "vertices", None)
        if verts is None:
            raise ValueError(f"PLY file does not contain vertices: {input_path}")
        cloud_colors = getattr(cloud, "colors", None)
        xyz = np.asarray(verts, dtype=np.float32)
        colors = (
            np.asarray(cloud_colors[:, :3], dtype=np.uint8)
            if cloud_colors is not None
            else None
        )
        return xyz, colors

    elif suffix == ".e57":
        try:
            import pye57

            # libe57 is the SWIG extension module pye57 exposes at runtime but omits
            # from its type stubs -- an upstream stub gap, not a missing dependency.
            from pye57 import libe57  # pyright: ignore[reportAttributeAccessIssue]
        except ImportError:
            print("Error: pye57 required. Install with: uv pip install pye57")
            sys.exit(1)
        e57 = pye57.E57(str(input_path))
        header = e57.get_header(0)
        point_fields = header.point_fields
        has_rgb = all(
            f in point_fields for f in ["colorRed", "colorGreen", "colorBlue"]
        )

        fields_to_read = ["cartesianX", "cartesianY", "cartesianZ"]
        if has_rgb:
            fields_to_read += ["colorRed", "colorGreen", "colorBlue"]

        capacity = min(header.point_count, 2_000_000)  # Cap at 2M for reading
        data = {}
        buffers = libe57.VectorSourceDestBuffer()
        for field in fields_to_read:
            np_array, buffer = e57.make_buffer(field, capacity)
            data[field] = np_array
            buffers.append(buffer)
        count = header.points.reader(buffers).read()

        xyz = np.column_stack(
            [
                data["cartesianX"][:count].copy(),
                data["cartesianY"][:count].copy(),
                data["cartesianZ"][:count].copy(),
            ]
        ).astype(np.float32)

        colors = None
        if has_rgb:
            colors = np.column_stack(
                [
                    data["colorRed"][:count].copy(),
                    data["colorGreen"][:count].copy(),
                    data["colorBlue"][:count].copy(),
                ]
            ).astype(np.uint8)

        return xyz, colors

    elif suffix in (".las", ".laz"):
        try:
            import laspy
        except ImportError:
            print("Error: laspy required. Install with: uv pip install 'laspy[lazrs]'")
            sys.exit(1)
        las = laspy.read(str(input_path))
        xyz = np.column_stack(
            [np.array(las.x), np.array(las.y), np.array(las.z)]
        ).astype(np.float32)
        colors = None
        if hasattr(las, "red") and hasattr(las, "green") and hasattr(las, "blue"):
            colors = np.column_stack(
                [
                    (np.array(las.red) >> 8).astype(np.uint8),
                    (np.array(las.green) >> 8).astype(np.uint8),
                    (np.array(las.blue) >> 8).astype(np.uint8),
                ]
            )
        return xyz, colors

    else:
        print(
            f"Error: Unsupported format '{suffix}'. Supported: .ply, .e57, .las, .laz"
        )
        sys.exit(1)


def write_pcd(
    xyz: np.ndarray,
    colors: np.ndarray | None,
    output_path: Path,
    coord_frame: str = "z-up",
    max_points: int = 300000,
) -> None:
    """Write point cloud to binary PCD format for SUSTechPOINTS.

    For large point clouds (>max_points), subsamples randomly while preserving
    spatial coverage. The full cloud is still used for training — this subsample
    is only for the annotation viewer.
    """

    # Apply coordinate transform if needed
    if coord_frame != "z-up":
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from pointcloud_detect.preprocessing import transform_coordinates

        xyz = transform_coordinates(xyz.astype(np.float64), coord_frame).astype(
            np.float32
        )

    n_total = len(xyz)

    # Subsample if too large for WebGL viewer
    if n_total > max_points:
        rng = np.random.default_rng(42)
        idx = rng.choice(n_total, max_points, replace=False)
        xyz = xyz[idx]
        colors = colors[idx] if colors is not None else None
        print(
            f"  Subsampled: {n_total:,} → {max_points:,} points (viewer only; full cloud used for training)"
        )

    n_points = len(xyz)

    # Write binary PCD with RGB
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        header = (
            "# .PCD v0.7 - Point Cloud Data file format\n"
            "VERSION 0.7\n"
            "FIELDS x y z rgb\n"
            "SIZE 4 4 4 4\n"
            "TYPE F F F U\n"
            "COUNT 1 1 1 1\n"
            f"WIDTH {n_points}\n"
            "HEIGHT 1\n"
            "VIEWPOINT 0 0 0 1 0 0 0\n"
            f"POINTS {n_points}\n"
            "DATA binary\n"
        )
        f.write(header.encode("ascii"))

        for i in range(n_points):
            f.write(struct.pack("<fff", xyz[i, 0], xyz[i, 1], xyz[i, 2]))
            if colors is not None:
                rgb_int = (
                    (int(colors[i, 0]) << 16)
                    | (int(colors[i, 1]) << 8)
                    | int(colors[i, 2])
                )
                f.write(struct.pack("<I", rgb_int))
            else:
                f.write(struct.pack("<I", 0xAAAAAA))

    print(f"  Wrote {n_points:,} points to: {output_path}")
    if n_total > max_points:
        print(
            "  NOTE: Bounding box annotations are spatial — they apply to the full cloud"
        )


# Default colors for common object types
LABEL_COLORS = {
    "door": "#00cc66",
    "window": "#44ccff",
    "wall": "#aaaaaa",
    "column": "#ffcc00",
    "beam": "#ff8800",
    "floor": "#996633",
    "ceiling": "#ccccee",
    "furniture": "#cc3333",
    "chair": "#3388ff",
    "table": "#ff6600",
    "other": "#888888",
}

# Default sizes (meters) for common object types
LABEL_SIZES = {
    "door": [1.0, 0.2, 2.1],
    "window": [1.2, 0.2, 1.2],
    "wall": [3.0, 0.3, 2.8],
    "column": [0.4, 0.4, 3.0],
    "beam": [3.0, 0.4, 0.4],
    "floor": [5.0, 5.0, 0.3],
    "ceiling": [5.0, 5.0, 0.3],
    "furniture": [1.0, 1.0, 1.0],
    "chair": [0.6, 0.6, 0.9],
    "table": [1.2, 0.8, 0.75],
    "other": [1.0, 1.0, 1.0],
}


def _update_obj_cfg(labels: list[str]) -> None:
    """Update SUSTechPOINTS obj_cfg.js to include custom labels in the dropdown."""
    obj_cfg_path = Path("tools/SUSTechPOINTS/public/js/obj_cfg.js")
    if not obj_cfg_path.exists():
        print(
            "  Warning: obj_cfg.js not found — labels won't appear in dropdown until SUSTechPOINTS is installed"
        )
        return

    content = obj_cfg_path.read_text()

    # Build entries for each label
    entries = []
    for label in labels:
        color = LABEL_COLORS.get(label, "#ff00ff")
        size = LABEL_SIZES.get(label, [1.0, 1.0, 1.0])
        # Capitalize for JS (SUSTechPOINTS uses PascalCase keys)
        key = label.replace(" ", "")
        entries.append(
            f"        {key}:  {{color: '{color}',  size:[{size[0]}, {size[1]}, {size[2]}]}},"
        )

    custom_block = "\n".join(entries)

    # Insert after 'obj_type_map = {' if not already present
    marker = "obj_type_map = {"
    if labels[0].replace(" ", "") + ":" not in content:
        content = content.replace(marker, marker + "\n" + custom_block)
        obj_cfg_path.write_text(content)
        print(f"  Updated obj_cfg.js with {len(labels)} custom labels")
    else:
        print("  obj_cfg.js already has custom labels")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare point cloud files for SUSTechPOINTS annotation"
    )
    parser.add_argument(
        "inputs", nargs="+", help="Input point cloud files (.ply, .e57, .las, .laz)"
    )
    parser.add_argument(
        "--scene-name",
        default=None,
        help="Scene name (default: filename stem). Only used for single file.",
    )
    parser.add_argument(
        "--coord-frame",
        default="z-up",
        help="Coordinate frame (z-up, livox, colmap, etc.)",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=300000,
        help="Max points for viewer (default 300K)",
    )
    parser.add_argument(
        "--labels",
        nargs="+",
        default=[
            "door",
            "window",
            "wall",
            "column",
            "beam",
            "floor",
            "ceiling",
            "furniture",
            "other",
        ],
        help="Object labels for annotation dropdown (default: door window wall column beam floor ceiling furniture other)",
    )
    args = parser.parse_args()

    for input_str in args.inputs:
        input_path = Path(input_str)
        if not input_path.exists():
            print(f"Error: {input_path} not found, skipping")
            continue

        # Determine scene name
        if args.scene_name and len(args.inputs) == 1:
            scene_name = args.scene_name
        else:
            scene_name = input_path.stem

        print(f"\nPreparing '{input_path.name}' as scene '{scene_name}'")
        print(f"  Format: {input_path.suffix.lower()}")
        print(f"  Max points: {args.max_points:,}")

        # Load point cloud
        xyz, colors = load_point_cloud(input_path)
        print(
            f"  Loaded: {len(xyz):,} points, color={'yes' if colors is not None else 'no'}"
        )

        # Write PCD
        sustechpoints_dir = Path("tools/SUSTechPOINTS/data") / scene_name / "lidar"
        output_path = sustechpoints_dir / "000000.pcd"
        write_pcd(xyz, colors, output_path, args.coord_frame, args.max_points)

        # Create label config with user-specified labels
        cfg_dir = Path("tools/SUSTechPOINTS/data") / scene_name
        label_cfg = cfg_dir / "label.cfg"
        label_cfg.write_text("\n".join(args.labels) + "\n")

        # Create label output directory (prevents save errors)
        label_dir = cfg_dir / "label"
        label_dir.mkdir(parents=True, exist_ok=True)

    # Update SUSTechPOINTS obj_cfg.js with custom labels (so dropdown shows them)
    _update_obj_cfg(args.labels)

    print(f"\nDone! {len(args.inputs)} scene(s) prepared.")
    print(f"  Labels: {', '.join(args.labels)}")
    print("  Start server:  cd tools/SUSTechPOINTS && python main.py --port 8081")
    print("  Open browser:  http://<ec2-ip>:8081")


if __name__ == "__main__":
    main()
