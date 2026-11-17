#!/usr/bin/env python3
"""Convert an S3DIS room .txt (X Y Z R G B per line) to a binary PLY. numpy-only.

S3DIS aligned rooms are ASCII: `x y z r g b` with RGB in 0-255. This loads the
room and writes a binary little-endian PLY (XYZ float32 + RGB uint8) that the
pipeline's PLY reader consumes for detection.

Usage:
  python scripts/convert-s3dis-room.py --input data/s3dis_rooms/conferenceRoom_1.txt \
      --output data/s3dis_ply/conferenceRoom_1.ply
"""

import argparse
from pathlib import Path

import numpy as np


def _write_ply(xyz: np.ndarray, rgb: np.ndarray, out: Path) -> None:
    n = xyz.shape[0]
    hdr = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    ).encode("ascii")
    dt = np.dtype(
        [
            ("x", "<f4"),
            ("y", "<f4"),
            ("z", "<f4"),
            ("red", "u1"),
            ("green", "u1"),
            ("blue", "u1"),
        ]
    )
    rec = np.empty(n, dt)
    x = xyz.astype("<f4")
    rec["x"], rec["y"], rec["z"] = x[:, 0], x[:, 1], x[:, 2]
    rec["red"], rec["green"], rec["blue"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(hdr)
        f.write(rec.tobytes())
    print(f"wrote {out} ({n:,} points)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True, help="S3DIS room .txt")
    ap.add_argument("--output", type=Path, required=True, help="Output PLY")
    a = ap.parse_args()

    # S3DIS rows are `x y z r g b`. A few files contain occasional malformed lines;
    # np.loadtxt with usecols is strict, so fall back to a tolerant manual parse.
    try:
        data = np.loadtxt(a.input, usecols=(0, 1, 2, 3, 4, 5))
    except ValueError:
        rows = []
        with open(a.input, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        rows.append([float(p) for p in parts[:6]])
                    except ValueError:
                        continue
        data = np.asarray(rows, dtype=np.float64)

    xyz = data[:, :3]
    rgb = np.clip(data[:, 3:6], 0, 255).astype(np.uint8)
    _write_ply(xyz, rgb, a.output)


if __name__ == "__main__":
    main()
