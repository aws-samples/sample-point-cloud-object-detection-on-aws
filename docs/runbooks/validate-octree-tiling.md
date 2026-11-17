# Runbook: Validate Density-Aware Octree Tiling

Validates the new axis-qualified octree tiler (`src/pointcloud_detect/tiling.py`) on
**real** indoor and outdoor scenes. The unit tests already prove the algorithm's
invariants on synthetic data; this runbook confirms it behaves sensibly on real point
clouds and does not regress detections versus the previous binary-split tiler.

**Who runs this:** operator on the GPU EC2 box (has the datasets + mmdet3d). The agent
cannot run this — it needs real data and inference.

**Branch under test:** `feature/density-octree-tiling`

**Time:** ~30–45 min once data is present.

---

## What we are checking

| Goal | How | Pass criterion |
|------|-----|----------------|
| Octree splits both regimes sensibly | Tile count + shape on indoor vs outdoor | Indoor produces 8-way splits; outdoor never splits the short (Z) axis |
| Density-aware placement helps | Tiles align to clusters, not mid-object | No obvious object cut in half across a tile seam (spot-check in viewer) |
| No detection regression | Compare detections vs `main` (old tiler) | Post-NMS detection count within ~±5%; no whole objects lost at seams |
| Point coverage is complete | Every input point in ≥1 tile | Coverage check script prints 100% |
| Performance is acceptable | Wall time + tile count | Comparable to or better than old tiler |

---

## 0. Setup

```bash
# On the EC2 box
cd "${REPO_ROOT:?set REPO_ROOT to your checkout path}"
git fetch origin
git checkout feature/density-octree-tiling
git pull

# Confirm the tiling unit tests pass here too (fast sanity)
uv run pytest tests/test_pipeline.py::TestTiling tests/test_pipeline.py::TestDensityGrid -v
```

Expected: `11 passed`.

---

## 1. Indoor scene (expect 8-way octree behavior)

Use a dense, roughly cubic indoor scan (ScanNet scene, S3DIS area, or a Rohbau3D room).

```bash
INDOOR=data/scannet/scene0011_00_vh_clean_2.ply   # substitute your file

uv run pointcloud-detect detect "$INDOOR" \
  --mode indoor --use-color --coord-frame z-up \
  --output ./val/indoor
```

Check the run summary JSON for `tile_count`. For a multi-room / large indoor scan you
should see multiple tiles; for a single small room, 1 tile is correct.

**Inspect tile shapes** (confirms axis-qualified octree actually split multiple axes):

```bash
uv run python - <<'PY'
import numpy as np
from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.io_readers import read_point_cloud
from pointcloud_detect.preprocessing import preprocess
from pointcloud_detect.tiling import adaptive_tile

cfg = PipelineConfig()
cloud = preprocess(read_point_cloud("data/scannet/scene0011_00_vh_clean_2.ply"), cfg.preprocessing)
tiles = adaptive_tile(cloud, cfg.tiling)
print(f"tiles: {len(tiles)}")

gmin, gmax = cloud.bounds_min, cloud.bounds_max
def interior_axes(t):
    return sum(
        1 for a in range(3)
        if t.extent_min[a] > gmin[a] + 1e-6 and t.extent_max[a] < gmax[a] - 1e-6
    )
counts = [interior_axes(t) for t in tiles]
print(f"tiles interior on 3 axes (true 8-way octant): {counts.count(3)}")
print(f"tiles interior on 2 axes: {counts.count(2)}")
print(f"max points in a tile: {max(len(t.xyz) for t in tiles)}  (budget {cfg.tiling.max_points_per_tile})")
PY
```

**Pass:** for a scan large on all three axes, at least one tile is interior on all three
axes (a genuine 8-way split).

**On the point budget:** `max_points_per_tile` bounds a node's *own* points, never the
size of the emitted tile, and treating it as a hard cap will make this step look
like a failure when the tiler is working as designed. Each tile carries its core
points **plus** the guard band, so with `overlap_margin_m=2.0` against 1 m minimum
tiles a point can legitimately belong to several neighbouring tiles.

Measured on a 260k-point clustered scene: total points across all tiles reach
**4.98x** the cloud, and peak memory **8.92x** — of which 6.64x is the retained
tiles themselves and 2.28x is transient working memory. At `overlap_margin_m=0.0`
the same scene retains 1.00x and peaks at 3.16x. Record the ratio you observe and
compare it against `docs/diagrams/tiling-pipeline.md`, which carries the full
decomposition; do not pass/fail against a single multiple of the budget.

Those figures come from `scripts/measure_tiling_memory.py`, so you can reproduce
them rather than taking them on trust:

```bash
PYTHONPATH=src python scripts/measure_tiling_memory.py --label mine
```

It also records an order-independent signature over every tile's point indices and
bounds. `--compare BEFORE AFTER` reports whether two runs produced an identical
tiling, which is how a performance change to the tiler gets shown to be
behaviour-preserving instead of argued to be. It exits non-zero if they differ.

If the retained figure is higher than you can afford, the lever is
`overlap_margin_m` — lowering it reduces duplication directly, at the cost of
cross-boundary detection for objects wider than the remaining band.

---

## 2. Outdoor scene (expect NO Z split)

Use a wide, thin outdoor scan (nuScenes LIDAR sweep, KITTI velodyne, or a DALES tile).

```bash
OUTDOOR=data/nuscenes/samples/LIDAR_TOP/<sample>.pcd.bin   # substitute

uv run pointcloud-detect detect "$OUTDOOR" \
  --mode outdoor --use-intensity \
  --output ./val/outdoor
```

**Confirm the Z axis is never split** (this is the key outdoor guarantee):

```bash
uv run python - <<'PY'
import numpy as np
from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.io_readers import read_point_cloud
from pointcloud_detect.preprocessing import preprocess
from pointcloud_detect.tiling import adaptive_tile

cfg = PipelineConfig()
# NOTE: substitute a real sample name for <sample> before running.
cloud = preprocess(read_point_cloud("data/nuscenes/samples/LIDAR_TOP/<sample>.pcd.bin"), cfg.preprocessing)
tiles = adaptive_tile(cloud, cfg.tiling)
zmin, zmax = cloud.bounds_min[2], cloud.bounds_max[2]
z_intact = all(np.isclose(t.extent_min[2], zmin) and np.isclose(t.extent_max[2], zmax) for t in tiles)
print(f"tiles: {len(tiles)}")
print(f"Z extent: {zmax - zmin:.1f} m  (min_tile_size*2 = {cfg.tiling.min_tile_size_m*2} m)")
print(f"every tile spans full Z (no wasted Z octants): {z_intact}")
PY
```

**Pass:** `every tile spans full Z: True` for a scene whose Z extent is under
`2 x min_tile_size_m`. If Z is genuinely tall (rare outdoors), a Z split is legitimate —
note it rather than fail.

---

## 3. Point coverage (no points dropped)

```bash
uv run python - <<'PY'
import numpy as np
from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.io_readers import read_point_cloud
from pointcloud_detect.preprocessing import preprocess
from pointcloud_detect.tiling import adaptive_tile

for path in ["data/scannet/scene0011_00_vh_clean_2.ply",
             # NOTE: substitute a real sample name for <sample> before running.
             "data/nuscenes/samples/LIDAR_TOP/<sample>.pcd.bin"]:
    cfg = PipelineConfig()
    cloud = preprocess(read_point_cloud(path), cfg.preprocessing)
    tiles = adaptive_tile(cloud, cfg.tiling)
    covered = np.unique(np.concatenate([t.point_indices for t in tiles]))
    pct = 100.0 * len(covered) / cloud.num_points
    print(f"{path}: {pct:.2f}% points covered ({len(covered)}/{cloud.num_points})")
PY
```

**Pass:** `100.00%` for both. Coverage < 100% is a bug (points fell through a seam).

---

## 4. Detection regression check (new tiler vs old)

The important one: confirm the new tiling does not lose or duplicate objects at seams
relative to the previous tiler on `main`.

```bash
# New tiler (this branch) — already run above into ./val/indoor and ./val/outdoor

# Old tiler (main) for comparison
git stash push -m "octree-runbook"   # if any local changes
git checkout main
uv run pointcloud-detect detect "$INDOOR"  --mode indoor  --use-color --output ./val/indoor_main
uv run pointcloud-detect detect "$OUTDOOR" --mode outdoor --use-intensity --output ./val/outdoor_main
git checkout feature/density-octree-tiling
# Restore the work stashed above, or it is silently left behind in the stash.
git stash list | grep -q "octree-runbook" && git stash pop

# Compare detection counts per class
uv run python - <<'PY'
import json, collections, pathlib
def counts(p):
    d = json.loads(pathlib.Path(p).read_text())
    c = collections.Counter(x["label"] for x in d["detections"])
    return c, len(d["detections"])
for name, new_p, old_p in [
    ("indoor",  "val/indoor/*_detections.json",  "val/indoor_main/*_detections.json"),
    ("outdoor", "val/outdoor/*_detections.json", "val/outdoor_main/*_detections.json"),
]:
    import glob
    new_c, new_n = counts(glob.glob(new_p)[0])
    old_c, old_n = counts(glob.glob(old_p)[0])
    print(f"\n{name}: new total {new_n} vs old total {old_n}")
    for label in sorted(set(new_c) | set(old_c)):
        print(f"  {label:20s} new={new_c[label]:4d} old={old_c[label]:4d}")
PY
```

**Pass:** totals within ~±5% and no class that had detections on `main` dropping to zero
on the new tiler. Large swings warrant a look in the viewer (step 5).

---

## 5. Visual spot-check (objects not cut at seams)

Open each result in the viewer and eyeball the tile boundaries:

```bash
uv run pointcloud-detect visualize "$INDOOR"  val/indoor/*_detections.json  --output ./val/indoor
# open val/indoor/*_3d.html
```

**Look for:** boxes that stop abruptly at a tile edge, or a single real object covered by
two half-boxes that NMS failed to merge. Density-median placement should make this rare
(cuts fall between clusters). A couple of seam duplicates are acceptable — that's what the
guard band + 3D NMS are for.

---

## Rollback

If the new tiler regresses detections materially:

```bash
# Revert the tiler AND its tests together. Several tests assert behaviour only
# the new tiler has (8-way splits, the min_tile_size_m invariant, non-finite
# rejection, the density-grid cell cap), so reverting tiling.py alone leaves the
# suite red and hides whatever you are trying to diagnose.
git checkout main -- src/pointcloud_detect/tiling.py \
                     src/pointcloud_detect/config.py \
                     tests/test_pipeline.py
# re-run step 4 to confirm the old behavior returns, then open an issue with the
# before/after detection counts attached.
```

## What this runbook does NOT cover

- **Memory budget (NFR-1.1 / NFR-1.2).** Not covered by *this* section — see part 2
  below, which validates the budget and the streaming ingest path directly. (An earlier
  revision of this file said the work was unimplemented, immediately above the section
  that validates it.)


---

# Runbook (part 2): Validate Memory-Budget Enforcement (NFR-1.1 / NFR-1.2)

Validates that streaming ingest keeps peak memory within `memory_budget_gb` on large
LAS/LAZ + E57 scenes. This is the benchmark that turns the "50 GB+ within 16 GB" claim
from designed into proven. Agent cannot run it (no large data, no memory probe here).

**Branch:** `feature/memory-budget-enforcement`

## Setup

```bash
git checkout feature/memory-budget-enforcement && git pull
uv run pytest tests/test_memory_budget.py -v   # expect 15 passed
```

## Confirm routing decision (which path a file takes)

```bash
uv run python - <<'PY'
from pathlib import Path
from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.pipeline import _plan_ingest
for f in ["data/site_50gb.laz", "data/floor.las", "data/room.ply"]:
    if Path(f).exists():
        stream, bmin, bmax = _plan_ingest(Path(f), PipelineConfig())
        print(f"{f}: streaming={stream}")
PY
```

**Expect:** the 50 GB LAZ → `streaming=True`; small LAS → `False`; PLY → `False`
(not streamable, always whole-load).

## Measure peak RSS on a large scene (the actual NFR test)

`/usr/bin/time -v` reports "Maximum resident set size" (in KB).

```bash
/usr/bin/time -v uv run pointcloud-detect detect data/site_50gb.laz \
  --mode outdoor --use-intensity --output ./val/mem 2>&1 | tee val/mem/time.log
grep "Maximum resident set size" val/mem/time.log
```

**Pass (NFR-1.1):** Maximum resident set size stays at or below `memory_budget_gb`
(default 16 GB = ~16.7M KB), with headroom for the model.

**If it exceeds the budget, do NOT reach for `WORKING_SET_FACTOR` first.** That knob feeds
the per-tile cap, which does not bind at any realistic budget: it needs to reach roughly
3,334 before the derived cap even drops below the default `max_points_per_tile` of 100,000,
so raising it from 4 to 400 changes nothing at all. In rough order of effect:

1. **Lower `tiling.max_points_per_tile`.** This is the knob that actually bounds a tile,
   and it takes effect immediately.
2. **Lower `tiling.overlap_margin_m`.** Retained tile bytes scale with the guard band —
   measured 4.98x the cloud at the default 2.0 m against 1.00x at 0. It trades away
   cross-boundary detection for objects wider than the remaining band.
3. **Lower `memory.ROUTING_SAFETY_FRACTION`** to make the pipeline stream sooner. Note this
   moves *only* the whole-vs-stream decision. It used to share one constant with the
   per-tile cap, so changing it silently altered which files streamed — the two are now
   `TILE_SAFETY_FRACTION` and `ROUTING_SAFETY_FRACTION` and are independent.
4. **Raise `WORKING_SET_FACTOR`** last, and only if a profile shows inference itself is the
   peak. It is an uncalibrated placeholder; this benchmark is what should set it, and
   `docs/diagrams/memory-budget-enforcement.md` gives the formula for deriving it from a
   run. The 3,334 crossover quoted above is pinned by
   `test_working_set_factor_is_inert_at_its_shipped_value`, so if this ordering advice
   ever goes stale that test fails rather than the advice quietly misleading you.

**Pass (NFR-1.2):** the run completes without OOM/kill on a scene far larger than RAM.

## Sanity: streaming vs whole-load produce comparable detections

On a scene small enough to run BOTH ways, force each path and compare detection counts
(streaming should not lose objects vs whole-load, modulo seam effects):

```bash
# whole-load: set a huge budget so _plan_ingest chooses whole-cloud
uv run pointcloud-detect detect data/floor.las --mode outdoor \
  --config <(echo 'tiling: {memory_budget_gb: 100000}') --output ./val/whole
# streaming: tiny budget forces the streaming path
uv run pointcloud-detect detect data/floor.las --mode outdoor \
  --config <(echo 'tiling: {memory_budget_gb: 0.05}') --output ./val/stream
# compare detection totals (reuse the compare snippet from part 1, step 4)
```

**Pass:** totals within ~±5%. Larger gaps → inspect in the viewer for seam artifacts.

## Expected limitations to confirm (not failures)

- Streaming a scene with `segmentation.enabled: true` emits a `RuntimeWarning` and returns
  **no** segmentation — by design. Confirm the warning fires and detections are still
  complete.
- PLY never streams regardless of size — confirm a large PLY uses whole-load (and may OOM
  if it genuinely exceeds RAM; PLY is out of scope for the scale claim).
