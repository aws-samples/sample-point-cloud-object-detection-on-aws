#!/usr/bin/env python3
"""Measure adaptive_tile memory behaviour and capture a tile-equivalence signature.

Produces the two things the tiling docs quote:

1. Materialisation and peak-memory ratios, as multiples of the input cloud's
   footprint. ``docs/runbooks/validate-octree-tiling.md`` asks operators to record
   what they observe and compare it against the decomposition in
   ``docs/diagrams/tiling-pipeline.md``; this is the harness those numbers came
   from, so a comparison is against a reproducible baseline rather than a number
   in prose.

2. An order-independent signature over each tile's sorted point indices and
   rounded bounds. Two runs with the same signature produced exactly the same
   tiling. That is what makes a performance refactor provable rather than
   argued: capture a signature before, capture one after, diff them.

Usage:
    python scripts/measure_tiling_memory.py --label before
    python scripts/measure_tiling_memory.py --label after
    python scripts/measure_tiling_memory.py --compare before after

Peak memory comes from ``tracemalloc``, so it counts Python allocations only. It
is a like-for-like comparison between runs, not an RSS measurement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tracemalloc
from pathlib import Path

import numpy as np

from pointcloud_detect.config import TilingConfig
from pointcloud_detect.models import PointCloud
from pointcloud_detect.tiling import adaptive_tile

DEFAULT_OUT_DIR = Path("local_dev_data/tiling-measurements")


def clustered_scene(seed: int = 11) -> np.ndarray:
    """Dense clusters in a large sparse volume — the shape that stresses the tiler.

    Uniform scenes split evenly and hide the guard-band duplication that dense
    clusters expose, so the headline ratios come from this one.
    """
    rng = np.random.default_rng(seed)
    blocks = []
    for _ in range(40):
        centre = rng.uniform(10, 190, size=3)
        blocks.append(centre + rng.normal(0, 1.5, size=(6_000, 3)))
    blocks.append(rng.uniform(0, 200, size=(20_000, 3)))  # sparse background
    return np.vstack(blocks).astype(np.float64)


def uniform_scene(seed: int = 12) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.uniform(0, 200, size=(260_000, 3)).astype(np.float64)


def _tile_signature(tiles: list) -> str:
    """Order-independent hash of the tiling's content.

    Sorting the per-tile digests makes this independent of emission order, so a
    refactor that changes traversal order but not the resulting tiles still
    matches.
    """
    digests = sorted(
        hashlib.sha256(
            np.sort(t.point_indices).tobytes()
            + np.round(t.extent_min, 6).tobytes()
            + np.round(t.extent_max, 6).tobytes()
        ).hexdigest()
        for t in tiles
    )
    return hashlib.sha256("".join(digests).encode()).hexdigest()


def run_case(name: str, xyz: np.ndarray, cfg: TilingConfig) -> dict:
    cloud = PointCloud(xyz=xyz)
    cloud_bytes = xyz.nbytes

    tracemalloc.start()
    tracemalloc.reset_peak()
    tiles = adaptive_tile(cloud, cfg)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    total_pts = sum(len(t.xyz) for t in tiles)
    retained = sum(t.xyz.nbytes for t in tiles)
    # Counts EMITTED points, which include the guard band, against a budget that
    # bounds a node's own points. So this is expected to be non-zero whenever
    # overlap_margin_m > 0 and is not a fault — see the note on the point budget in
    # docs/runbooks/validate-octree-tiling.md. Genuine unsplittable overruns are
    # reported by adaptive_tile's RuntimeWarning instead.
    over_budget_with_band = sum(
        1
        for t in tiles
        if t.point_indices is not None
        and len(t.point_indices) > cfg.max_points_per_tile
    )

    print(f"--- {name} ---")
    print(f"  cloud points     : {len(xyz):,} ({cloud_bytes / 1e6:.1f} MB)")
    print(f"  tiles            : {len(tiles):,}")
    print(f"  tiles over core budget (incl. guard band): {over_budget_with_band}")
    print(f"  points in tiles  : {total_pts:,} ({total_pts / len(xyz):.2f}x cloud)")
    print(
        f"  retained         : {retained / 1e6:.1f} MB ({retained / cloud_bytes:.2f}x)"
    )
    print(
        f"  peak (tracemalloc): {peak / 1e6:.1f} MB ({peak / cloud_bytes:.2f}x cloud)"
    )

    return {
        "name": name,
        "points": int(len(xyz)),
        "tiles": len(tiles),
        "tiles_over_core_budget_incl_guard_band": over_budget_with_band,
        "materialisation_x": round(total_pts / len(xyz), 4),
        "retained_x": round(retained / cloud_bytes, 4),
        "peak_x": round(peak / cloud_bytes, 4),
        "peak_bytes": int(peak),
        "signature": _tile_signature(tiles),
    }


def _cfg(overlap_margin_m: float) -> TilingConfig:
    """Shared tiling config for the cases below, varying only the overlap margin.

    Built by a factory rather than `TilingConfig(**base, ...)`: a dict literal mixing
    an int point budget with a float tile size infers dict[str, float], which then
    fails to satisfy the int-typed fields it is unpacked into.
    """
    return TilingConfig(
        max_points_per_tile=5_000,
        min_tile_size_m=1.0,
        overlap_margin_m=overlap_margin_m,
    )


def all_cases() -> list[dict]:
    return [
        run_case("clustered, margin=2.0", clustered_scene(), _cfg(2.0)),
        run_case("clustered, margin=0.0", clustered_scene(), _cfg(0.0)),
        run_case("uniform, margin=2.0", uniform_scene(), _cfg(2.0)),
    ]


def _load(out_dir: Path, label: str) -> list[dict]:
    path = out_dir / f"{label}.json"
    if not path.exists():
        sys.exit(f"No measurement found at {path}. Run with --label {label} first.")
    return json.loads(path.read_text())


def compare(out_dir: Path, before: str, after: str) -> int:
    """Report whether two runs produced identical tilings, and how memory moved."""
    a, b = _load(out_dir, before), _load(out_dir, after)
    by_name = {r["name"]: r for r in b}
    drift = 0

    for old in a:
        new = by_name.get(old["name"])
        if new is None:
            print(f"MISSING in {after}: {old['name']}")
            drift += 1
            continue
        same = old["signature"] == new["signature"]
        verdict = "identical" if same else "*** TILING CHANGED ***"
        if not same:
            drift += 1
        speed = (
            f"{old['peak_x']:.2f}x -> {new['peak_x']:.2f}x peak"
            if old["peak_x"] != new["peak_x"]
            else f"{new['peak_x']:.2f}x peak (unchanged)"
        )
        print(f"{old['name']}: {verdict}; {speed}")

    if drift:
        print(f"\n{drift} case(s) differ — the change is NOT behaviour-preserving.")
    else:
        print("\nAll signatures match: the tiling is byte-for-byte unchanged.")
    return 1 if drift else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="record a measurement under this label")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="compare two recorded labels",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"where measurements are stored (default: {DEFAULT_OUT_DIR})",
    )
    args = parser.parse_args()

    if args.compare:
        return compare(args.out_dir, *args.compare)

    if not args.label:
        parser.error("give either --label to record or --compare to diff")

    results = all_cases()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{args.label}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
