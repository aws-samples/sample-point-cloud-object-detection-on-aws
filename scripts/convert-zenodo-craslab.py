#!/usr/bin/env python3
"""Convert the CC BY 4.0 CRAS-lab office dataset into training scenes for the trainer.

This is the data half of the commercially clean model (README, "Commercially clean
model"): a detector trained from scratch on it inherits no pretrained checkpoint, so
its terms are those of this dataset.

Source dataset:
  Abreu, N., Souza, R., Pinto, A., Matos, A., & Pires, M. (2023).
  "Indoor point cloud dataset for BIM related applications" [Data set]. Zenodo.
  https://doi.org/10.5281/zenodo.7948116 -- Creative Commons Attribution 4.0
  (CC BY 4.0). Described in: Data 8(6):101, https://doi.org/10.3390/data8060101

craslabannotated.zip holds one ASCII cloud of 21 registered scans (~585M points,
~25 GB unpacked), one point per line:
    X  Y  Z  R  G  B  Intensity  Label
`Label` is a per-point SEMANTIC class id (0..33). Detectors (VoteNet / FCAF3D) need
per-INSTANCE 3D boxes, so this script:

  1. streams the cloud, from the zip or an unpacked file, optionally
     voxel-downsampling on the fly to bound memory,
  2. clusters same-class points into instances (pure-numpy voxel
     connected-components, no scikit-learn/scipy),
  3. fits an axis-aligned box per instance (heading 0: semantic labels carry no
     orientation),
  4. cuts the building into square XY blocks, room-sized by default, and writes each
     block that holds a box as one training scene: <scene>_<ix>_<iy>.bin (float32
     N x 6, XYZ + raw 0-255 RGB, the format training/mmdet3d_trainer.py reads) and
     <scene>_<ix>_<iy>.json in the unified annotation format. Blocks matter: the
     trainer holds out at least one scene for validation, so one whole-building
     scene would leave nothing to train on, and its PointSample would see a
     building rather than a room.
  5. optionally writes the whole downsampled cloud as a binary PLY, for viewing or
     for running detection on it afterwards.

"Structure" classes (ceiling/floor/wall) and unassigned points are not detection
targets and would produce room-sized boxes; they are skipped unless
--include-structure is passed. Their points stay in the scenes as context.

Usage:
  python scripts/convert-zenodo-craslab.py \\
      --input data/craslab/craslabannotated.zip \\
      --out-dir data/craslab_scenes \\
      --downsample-voxel 0.03 \\
      --out-ply data/craslab/craslab.ply

Then train from scratch on --out-dir (configs/train_clean_indoor_craslab.yaml).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import numpy as np

# Zenodo record 7948116 label id -> class name (33 classes; 0 = unassigned).
CRASLAB_CLASSES: dict[int, str] = {
    0: "unassigned",
    1: "ceiling",
    2: "floor",
    3: "wall",
    4: "door",
    5: "window",
    6: "desk",
    7: "chair",
    8: "cabinet",
    9: "mobile cabinet",
    10: "shelf",
    11: "vents",
    12: "water tank",
    13: "bin",
    14: "box",
    15: "board",
    16: "computer",
    17: "screen",
    18: "printer",
    19: "vest",
    20: "switch",
    21: "paper dispenser",
    22: "alcohol dispenser",
    23: "cable",
    24: "phone",
    25: "robot",
    26: "water kettle",
    27: "stairs",
    28: "ladder",
    29: "oil heater",
    30: "divider",
    31: "hanger",
    32: "fan",
    33: "water dispenser",
}

# Not detection targets: room structure + unassigned/noise.
STRUCTURE_LABELS = {0, 1, 2, 3}

PROVENANCE = {
    "source": "Zenodo 10.5281/zenodo.7948116 (CRAS labs @ FEUP)",
    "license": "CC BY 4.0",
    "attribution": (
        "Abreu, N., Souza, R., Pinto, A., Matos, A., & Pires, M. (2023). "
        "Indoor point cloud dataset for BIM related applications. Zenodo. "
        "https://doi.org/10.5281/zenodo.7948116"
    ),
}


def _open_lines(path: Path) -> Iterator[str]:
    """Lines of the cloud, from a .zip (its single text member) or a plain file."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise ValueError(
                    f"{path} holds {len(members)} files; expected the one ASCII cloud"
                )
            with zf.open(members[0]) as raw:
                yield from io.TextIOWrapper(raw, encoding="ascii", errors="replace")
    else:
        with open(path, encoding="ascii", errors="replace") as f:
            yield from f


def parse_cloud(
    path: Path, downsample_voxel: float = 0.0, chunk_lines: int = 5_000_000
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (xyz float32 [N,3], rgb uint8 [N,3], labels int32 [N]).

    The cloud does not fit in memory through np.loadtxt, so it is streamed in line
    chunks. The `//` column header, the bare point-count line and any malformed row
    are skipped. With downsample_voxel > 0 one point is kept per occupied voxel of
    that size (metres), deduplicated across chunks; 0 keeps every point, which is
    only safe on small clouds.
    """
    xyz_parts: list[np.ndarray] = []
    rgb_parts: list[np.ndarray] = []
    lab_parts: list[np.ndarray] = []
    seen_keys: set[int] = set()  # packed voxel keys, for dedup across chunks
    total = 0

    # Voxel coords packed into one int64 so dedup is vectorized. The offset keeps
    # coords non-negative; 21 bits per axis spans ~2M voxels, ample for a building.
    offset = 1 << 20
    mul = 1 << 21

    def pack_keys(xyz_f: np.ndarray) -> np.ndarray:
        v = np.floor(xyz_f / downsample_voxel).astype(np.int64) + offset
        return (v[:, 0] * mul + v[:, 1]) * mul + v[:, 2]

    def flush(lines: list[str]) -> None:
        rows = [
            ln
            for ln in lines
            if ln.strip() and not ln.lstrip().startswith("//") and len(ln.split()) >= 8
        ]
        if not rows:
            return
        arr = np.loadtxt(
            io.StringIO("".join(rows)), usecols=(0, 1, 2, 3, 4, 5, 7), ndmin=2
        )
        xyz = arr[:, :3].astype(np.float32)
        rgb = np.clip(arr[:, 3:6], 0, 255).astype(np.uint8)
        lab = np.rint(arr[:, 6]).astype(np.int32)
        if downsample_voxel > 0:
            keys = pack_keys(xyz)
            _, first_idx = np.unique(keys, return_index=True)
            cand = keys[first_idx]
            fresh = np.fromiter(
                (k not in seen_keys for k in cand.tolist()), bool, len(cand)
            )
            keep = first_idx[fresh]
            seen_keys.update(cand[fresh].tolist())
            xyz, rgb, lab = xyz[keep], rgb[keep], lab[keep]
        xyz_parts.append(xyz)
        rgb_parts.append(rgb)
        lab_parts.append(lab)

    buf: list[str] = []
    for line in _open_lines(path):
        total += 1
        buf.append(line)
        if len(buf) >= chunk_lines:
            flush(buf)
            buf = []
            kept = sum(len(p) for p in xyz_parts)
            print(f"  ...read {total:,} lines, kept {kept:,}", flush=True)
    flush(buf)

    if not xyz_parts:
        raise ValueError(f"parsed 0 data rows from {path}")
    xyz = np.concatenate(xyz_parts)
    rgb = np.concatenate(rgb_parts)
    labels = np.concatenate(lab_parts)
    print(
        f"  read {total:,} lines -> kept {len(xyz):,} points "
        f"(downsample_voxel={downsample_voxel})",
        flush=True,
    )
    return xyz, rgb, labels


def cluster_voxels(pts: np.ndarray, voxel: float, min_points: int) -> list[np.ndarray]:
    """Cluster points into instances via voxel connected-components (26-neighbour).

    Points are binned to a voxel grid; occupied voxels are joined to their 26
    neighbours with a union-find, then point clusters are read back. Returns index
    arrays into `pts`, one per cluster of at least `min_points` points.
    """
    if len(pts) == 0:
        return []

    mins = pts.min(axis=0)
    vox = np.floor((pts - mins) / voxel).astype(np.int64)
    uniq, inv = np.unique(vox, axis=0, return_inverse=True)
    inv = inv.ravel()
    n_vox = len(uniq)
    vox_to_id = {tuple(int(c) for c in v): i for i, v in enumerate(uniq)}
    parent = np.arange(n_vox, dtype=np.int64)

    def find(a: int) -> int:
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != root:  # path compression
            parent[a], a = root, parent[a]
        return int(root)

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    neighbours = [
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
    ]
    for i, v in enumerate(uniq):
        x, y, z = int(v[0]), int(v[1]), int(v[2])
        for dx, dy, dz in neighbours:
            j = vox_to_id.get((x + dx, y + dy, z + dz))
            if j is not None and j > i:
                union(i, j)

    roots = np.array([find(i) for i in range(n_vox)], dtype=np.int64)
    point_root = roots[inv]
    clusters: list[np.ndarray] = []
    for r in np.unique(point_root):
        idx = np.nonzero(point_root == r)[0]
        if len(idx) >= min_points:
            clusters.append(idx)
    return clusters


def fit_box(pts: np.ndarray) -> tuple[list[float], list[float]]:
    """Axis-aligned box around `pts`: (center_xyz, size_wlh), sizes at least 1 mm."""
    lo = pts.min(axis=0)
    hi = pts.max(axis=0)
    center = ((lo + hi) / 2.0).tolist()
    size = [max(float(s), 1e-3) for s in (hi - lo).tolist()]
    return center, size


def instance_boxes(
    xyz: np.ndarray,
    labels: np.ndarray,
    voxel: float,
    min_points: int,
    include_structure: bool = False,
) -> list[dict]:
    """One axis-aligned box per same-class connected cluster, in annotation form."""
    skip = set() if include_structure else STRUCTURE_LABELS
    boxes: list[dict] = []
    for lab in sorted(set(labels.tolist())):
        if lab in skip:
            continue
        name = CRASLAB_CLASSES.get(lab, f"class_{lab}")
        pts = xyz[labels == lab]
        for idx in cluster_voxels(pts, voxel=voxel, min_points=min_points):
            center, size = fit_box(pts[idx])
            boxes.append(
                {
                    "center_xyz": [round(c, 4) for c in center],
                    "size_wlh": [round(s, 4) for s in size],
                    "heading": 0.0,
                    "class_name": name,
                }
            )
    return boxes


def split_into_blocks(
    xyz: np.ndarray,
    boxes: list[dict],
    block_size: float,
    min_block_points: int,
) -> list[tuple[tuple[int, int], np.ndarray, list[dict]]]:
    """Cut the cloud into square XY blocks of `block_size` metres.

    Returns (block index, point indices, boxes) for every block with at least
    `min_block_points` points and at least one box. A box belongs to the block that
    holds its center, so each box is trained on exactly once; an object straddling
    a block edge keeps its full box but only the points on its block's side.
    """
    origin = xyz[:, :2].min(axis=0)
    cell = np.floor((xyz[:, :2] - origin) / block_size).astype(np.int64)
    boxes_by_cell: dict[tuple[int, int], list[dict]] = {}
    for box in boxes:
        cx, cy = box["center_xyz"][0], box["center_xyz"][1]
        key = (
            int(np.floor((cx - origin[0]) / block_size)),
            int(np.floor((cy - origin[1]) / block_size)),
        )
        boxes_by_cell.setdefault(key, []).append(box)

    order = np.lexsort((cell[:, 1], cell[:, 0]))
    sorted_cells = cell[order]
    starts = np.flatnonzero(np.any(np.diff(sorted_cells, axis=0) != 0, axis=1)) + 1
    blocks = []
    for group in np.split(order, starts):
        if len(group) == 0:
            continue
        key = (int(cell[group[0], 0]), int(cell[group[0], 1]))
        cell_boxes = boxes_by_cell.get(key, [])
        if len(group) >= min_block_points and cell_boxes:
            blocks.append((key, np.sort(group), cell_boxes))
    return blocks


def write_bin(xyz: np.ndarray, rgb: np.ndarray, out: Path) -> None:
    """float32 N x 6 (XYZ + raw 0-255 RGB), the layout the trainer reads."""
    out.parent.mkdir(parents=True, exist_ok=True)
    np.column_stack([xyz, rgb]).astype(np.float32).tofile(out)


def write_ply(xyz: np.ndarray, rgb: np.ndarray, out: Path) -> None:
    """Binary PLY with XYZ (float32) and RGB (uint8)."""
    n = xyz.shape[0]
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    ).encode("ascii")
    rec = np.empty(
        n,
        np.dtype(
            [
                ("x", "<f4"),
                ("y", "<f4"),
                ("z", "<f4"),
                ("red", "u1"),
                ("green", "u1"),
                ("blue", "u1"),
            ]
        ),
    )
    x = xyz.astype("<f4")
    rec["x"], rec["y"], rec["z"] = x[:, 0], x[:, 1], x[:, 2]
    rec["red"], rec["green"], rec["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(header)
        f.write(rec.tobytes())
    print(f"wrote PLY {out} ({n:,} points)")


def write_scenes(
    xyz: np.ndarray,
    rgb: np.ndarray,
    blocks: list[tuple[tuple[int, int], np.ndarray, list[dict]]],
    out_dir: Path,
    scene_id: str,
    derivation: str,
) -> list[Path]:
    """One <scene>_<ix>_<iy>.bin + .json per block. Returns the JSON paths.

    point_cloud_path is the .bin's name, which the trainer resolves against the
    annotation directory (its data_path).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for (ix, iy), idx, boxes in blocks:
        name = f"{scene_id}_{ix:03d}_{iy:03d}"
        write_bin(xyz[idx], rgb[idx], out_dir / f"{name}.bin")
        ann = {
            "scene_id": name,
            "point_cloud_path": f"{name}.bin",
            "num_annotations": len(boxes),
            "annotations": boxes,
            "provenance": {**PROVENANCE, "derivation": derivation},
        }
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(ann, indent=2))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--input",
        type=Path,
        required=True,
        help="craslabannotated.zip, or the ASCII cloud unpacked from it",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="directory for the training scenes (.bin + .json per block)",
    )
    ap.add_argument(
        "--out-ply",
        type=Path,
        help="also write the whole downsampled cloud as a binary PLY",
    )
    ap.add_argument("--scene-id", default="craslab", help="prefix for scene ids")
    ap.add_argument(
        "--voxel", type=float, default=0.05, help="instance clustering voxel size (m)"
    )
    ap.add_argument(
        "--min-points", type=int, default=50, help="minimum points per instance"
    )
    ap.add_argument(
        "--block-size",
        type=float,
        default=6.0,
        help="side of the square XY blocks each scene covers (m)",
    )
    ap.add_argument(
        "--min-block-points",
        type=int,
        default=5000,
        help="skip blocks with fewer points than this",
    )
    ap.add_argument(
        "--downsample-voxel",
        type=float,
        default=0.0,
        help="keep one point per voxel of this size (m) while reading; "
        "0 keeps all, which only fits in memory for small clouds. The "
        "full CRAS-lab cloud needs 0.02-0.05.",
    )
    ap.add_argument(
        "--chunk-lines",
        type=int,
        default=5_000_000,
        help="lines parsed per streamed chunk (memory/speed tradeoff)",
    )
    ap.add_argument(
        "--include-structure",
        action="store_true",
        help="also box ceiling/floor/wall/unassigned (default: skip)",
    )
    a = ap.parse_args(argv)

    print(f"parsing {a.input} ...", flush=True)
    xyz, rgb, labels = parse_cloud(a.input, a.downsample_voxel, a.chunk_lines)
    if a.out_ply:
        write_ply(xyz, rgb, a.out_ply)

    boxes = instance_boxes(xyz, labels, a.voxel, a.min_points, a.include_structure)
    blocks = split_into_blocks(xyz, boxes, a.block_size, a.min_block_points)
    if not blocks:
        print("no block holds both enough points and a box", file=sys.stderr)
        return 1
    derivation = (
        "per-point semantic labels clustered into instances "
        f"(cluster_voxel={a.voxel}m, min_points={a.min_points}, "
        f"stream_downsample_voxel={a.downsample_voxel}m), split into "
        f"{a.block_size}m XY blocks; heading=0 (semantic labels carry no orientation)"
    )
    written = write_scenes(xyz, rgb, blocks, a.out_dir, a.scene_id, derivation)

    counts: dict[str, int] = {}
    for _, _, block_boxes in blocks:
        for box in block_boxes:
            counts[box["class_name"]] = counts.get(box["class_name"], 0) + 1
    print(f"wrote {len(written)} scenes to {a.out_dir} ({sum(counts.values())} boxes)")
    for name in sorted(counts):
        print(f"  {name:20s} {counts[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
