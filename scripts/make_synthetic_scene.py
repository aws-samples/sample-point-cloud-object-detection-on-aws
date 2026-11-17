"""Generate a large synthetic LAS/LAZ point cloud for scale/memory benchmarking.

This produces an outdoor-style scene (wide X/Y, thin Z) whose points are drawn
mostly into Gaussian clusters over a sparse fill, at a target point count,
written in chunks so the generator itself never holds the whole cloud in memory.
It exercises the streaming ingest + memory-budget path (NFR-1.1 / NFR-1.2); it is
NOT a source of real objects, so it validates scale behaviour and peak memory,
not detection quality. For detection-quality/GPU testing, use a real captured
50 GB cloud instead — this generator is only the scale/memory first step.

Density profile matters. The adaptive tiler splits at the occupancy median and
emits over-budget tiles where a region cannot be cut below ``min_tile_size_m``,
so the number of over-budget tiles is driven by how much denser the clusters are
than the sparse fill (the density contrast). Weak clustering barely differs from
uniform and under-tests the tiler; tight clustering creates genuine dense/sparse
structure. Measured contrast (p99 / median occupancy) by ``--cluster-sigma`` over
the 500 m extent, 60 clusters:

    sigma=40 m  -> ~1.3-4x  (clusters overlap heavily; almost uniform; no empty cells)
    sigma=10 m  -> ~25x     (realistic dense/sparse structure) [default]
    sigma=3 m   -> ~100x    (very tight clusters, large sparse volume)

Defaults produce the sigma=10 m realistic profile. To reproduce the originally
recorded 300 M benchmark (which used the weaker overlapping-cluster scene), pass
``--clusters 60 --cluster-sigma 40``.

Usage:
    # ~50 GB LAZ is roughly 1.5 billion points at LAZ compression; size varies.
    # Start smaller to sanity-check, then scale up:
    uv run python scripts/make_synthetic_scene.py --points 50_000_000  --out local_dev_data/synth_50M.laz
    uv run python scripts/make_synthetic_scene.py --points 1_500_000_000 --out local_dev_data/synth_site.laz

Note: point count drives size, not bytes. Watch `ls -lh` on the output and `df -h`
on the volume — a billion+ points needs real disk. LAZ compresses ~5-10x vs LAS.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import laspy
import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, required=True, help="total point count")
    ap.add_argument("--out", type=Path, required=True, help="output .las or .laz path")
    ap.add_argument(
        "--extent-xy", type=float, default=500.0, help="scene width/depth in metres"
    )
    ap.add_argument(
        "--extent-z",
        type=float,
        default=4.0,
        help="scene height in metres (thin, outdoor-like)",
    )
    ap.add_argument(
        "--clusters", type=int, default=60, help="number of Gaussian cluster centres"
    )
    ap.add_argument(
        "--cluster-sigma",
        type=float,
        default=10.0,
        help="cluster spread in metres; smaller = tighter clusters = higher density contrast "
        "(10 m gives a realistic dense/sparse profile; pass 40 to reproduce the original 300M scene)",
    )
    ap.add_argument(
        "--dense-fraction",
        type=float,
        default=0.8,
        help="fraction of points drawn into clusters vs uniform sparse fill",
    )
    ap.add_argument(
        "--chunk",
        type=int,
        default=10_000_000,
        help="points generated/written per chunk",
    )
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)

    # LAS 1.4 + point format 6, NOT the older 1.2 / format 3. LAS 1.2 stores the
    # point count in a 32-bit field capped at 4,294,967,295 (2**32 - 1); a 50 GB
    # cloud at this compression is ~7.6 billion points, well over that ceiling, and
    # laspy raises LaspyException mid-write when it is crossed. LAS 1.4 uses a 64-bit
    # point count and has no practical limit here. The streaming reader is version
    # agnostic (it goes through laspy.open / chunk_iterator / point_count), so 1.4
    # reads back on the ingest path exactly like 1.2.
    #
    # LAS scaling: 1mm resolution, offset at origin.
    # laspy declares scales/offsets as np.ndarray (laspy/header.py), so build arrays
    # rather than lists: a list works at runtime only because laspy indexes it, and
    # it fails pyright, which is a blocking CI job.
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = np.array([0.001, 0.001, 0.001], dtype=np.float64)
    header.offsets = np.zeros(3, dtype=np.float64)

    # Cluster centres are fixed for the whole scene, not regenerated per chunk:
    # otherwise every chunk would scatter its clusters to fresh locations, smearing
    # them across the extent and flattening the density contrast the tiler is meant
    # to be stressed by. The dense/sparse split and contrast are set by
    # --dense-fraction and --cluster-sigma (see module docstring for measured
    # contrast vs sigma); tighter clusters produce more over-budget tiles.
    centers = rng.uniform(0, args.extent_xy, size=(args.clusters, 2))

    written = 0
    with laspy.open(str(args.out), mode="w", header=header) as writer:
        while written < args.points:
            n = min(args.chunk, args.points - written)
            n_dense = int(n * args.dense_fraction)
            n_sparse = n - n_dense

            pick = rng.integers(0, len(centers), size=n_dense)
            dense_xy = centers[pick] + rng.normal(
                0, args.cluster_sigma, size=(n_dense, 2)
            )
            dense_xy = np.clip(dense_xy, 0, args.extent_xy)
            sparse_xy = rng.uniform(0, args.extent_xy, size=(n_sparse, 2))

            xy = np.vstack([dense_xy, sparse_xy])
            z = rng.uniform(0, args.extent_z, size=(n, 1))
            xyz = np.hstack([xy, z]).astype(np.float64)

            pr = laspy.ScaleAwarePointRecord.zeros(n, header=header)
            pr.x = xyz[:, 0]
            pr.y = xyz[:, 1]
            pr.z = xyz[:, 2]
            writer.write_points(pr)

            written += n
            print(f"  wrote {written:,} / {args.points:,} points", flush=True)

    size_gb = args.out.stat().st_size / 1e9
    print(f"Done: {args.out} ({size_gb:.2f} GB, {args.points:,} points)")


if __name__ == "__main__":
    main()
