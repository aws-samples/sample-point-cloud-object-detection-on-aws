#!/usr/bin/env python3
"""Convert a Rohbau3D scene (NumPy arrays) to a colored PLY for the detection pipeline.

Rohbau3D ships each scene as separate arrays:
  coord.npy    (N, 3) float  -- XYZ in metres, scene-local, Z-up
  color.npy    (N, 3) uint8 or float  -- RGB (0-255 or 0-1)
  class.npy    (N,)  int      -- optional semantic label (not needed for inference)
  instance.npy (N,)  int      -- optional instance id (not needed for inference)

The pipeline's PLY reader (io_readers.read_ply via trimesh) consumes XYZ + vertex
colors, which is all detection needs. This writes a binary PLY with per-vertex RGB.

Usage:
  python scripts/convert-rohbau3d.py --scene-dir data/rohbau3d/<scene> \
      --output data/rohbau3d/<scene>.ply

  # convert every scene under a root (each subdir with a coord.npy):
  python scripts/convert-rohbau3d.py --root data/rohbau3d --out-dir data/rohbau3d_ply
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def _load_scene(scene_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    coord_path = scene_dir / "coord.npy"
    color_path = scene_dir / "color.npy"
    if not coord_path.exists():
        raise FileNotFoundError(f"No coord.npy in {scene_dir}")

    xyz = np.load(coord_path).astype(np.float64)
    if xyz.ndim != 2 or xyz.shape[1] != 3:
        raise ValueError(f"coord.npy in {scene_dir} is not (N, 3): {xyz.shape}")

    if color_path.exists():
        rgb = np.load(color_path)
        # Normalize to uint8 0-255 regardless of source encoding.
        if np.issubdtype(rgb.dtype, np.floating):
            # floats are typically 0-1; scale if so, else assume already 0-255
            rgb = (rgb * 255.0) if rgb.max() <= 1.0 else rgb
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        if rgb.shape[0] != xyz.shape[0]:
            raise ValueError(
                f"color.npy rows ({rgb.shape[0]}) != coord rows ({xyz.shape[0]})"
            )
    else:
        # No color -> mid-grey so FCAF3D still gets a valid (constant) color channel.
        # VoteNet (XYZ-only) is unaffected; FCAF3D will be weaker without real color.
        print(f"  WARNING: no color.npy in {scene_dir}; writing grey placeholder")
        rgb = np.full((xyz.shape[0], 3), 128, dtype=np.uint8)

    return xyz, rgb


def _write_ply(xyz: np.ndarray, rgb: np.ndarray, out_path: Path) -> None:
    """Write a binary little-endian PLY with per-vertex XYZ (float32) + RGB (uint8).

    Written directly with numpy so this has no third-party dependency (no trimesh):
    the pipeline's PLY reader loads vertices + vertex colors, which is all inference
    needs.
    """
    n = xyz.shape[0]
    xyz32 = xyz.astype("<f4")
    rgb8 = rgb.astype(np.uint8)

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "property uchar red\n"
        "property uchar green\n"
        "property uchar blue\n"
        "end_header\n"
    ).encode("ascii")

    # Interleave into a structured record: 3 float32 + 3 uint8 per vertex.
    dtype = np.dtype(
        [
            ("x", "<f4"),
            ("y", "<f4"),
            ("z", "<f4"),
            ("red", "u1"),
            ("green", "u1"),
            ("blue", "u1"),
        ]
    )
    rec = np.empty(n, dtype=dtype)
    rec["x"], rec["y"], rec["z"] = xyz32[:, 0], xyz32[:, 1], xyz32[:, 2]
    rec["red"], rec["green"], rec["blue"] = rgb8[:, 0], rgb8[:, 1], rgb8[:, 2]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(rec.tobytes())
    print(f"  wrote {out_path} ({n:,} points)")


def convert_one(scene_dir: Path, out_path: Path) -> None:
    print(f"converting {scene_dir.name} ...")
    xyz, rgb = _load_scene(scene_dir)
    _write_ply(xyz, rgb, out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scene-dir", type=Path, help="Single scene directory (has coord.npy)"
    )
    ap.add_argument("--output", type=Path, help="Output PLY path (with --scene-dir)")
    ap.add_argument("--root", type=Path, help="Root containing multiple scene subdirs")
    ap.add_argument("--out-dir", type=Path, help="Output directory (with --root)")
    args = ap.parse_args()

    if args.scene_dir:
        out = args.output or args.scene_dir.with_suffix(".ply")
        convert_one(args.scene_dir, out)
        return

    if args.root:
        out_dir = args.out_dir or (args.root.parent / f"{args.root.name}_ply")
        scenes = sorted(p.parent for p in args.root.rglob("coord.npy"))
        if not scenes:
            raise SystemExit(f"No scenes (coord.npy) found under {args.root}")
        print(f"found {len(scenes)} scene(s)")
        for scene_dir in scenes:
            convert_one(scene_dir, out_dir / f"{scene_dir.name}.ply")
        return

    ap.error("provide either --scene-dir or --root")


if __name__ == "__main__":
    main()
