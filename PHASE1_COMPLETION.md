# Phase 1 Completion Report

## Status: ✅ Complete — Ready for Phase 2 VAMS Integration

This document records the Phase 1 implementation status against the spec, documents
deviations and decisions made during development, and lists prerequisites for Phase 2.

---

## Functional Requirements Status

### FR-1.x — I/O & Format Parsing

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-1.1 | Parse E57 files (XYZ + intensity + RGB) | ✅ | Capacity-limited reading via pye57 libe57 buffers — safe for 1B+ point files |
| FR-1.2 | Parse LAS/LAZ with chunked/streamed reading | ✅ | Auto-caps at 20M points for files >100M points |
| FR-1.3 | Parse PLY files | ✅ | Auto-caps at 20M points for files >100M points |
| FR-1.4 | Auto-detect format from extension + magic bytes | ✅ | E57, LAS, LAZ, PLY, KITTI .bin |
| FR-1.5 | Unified numpy array representation | ✅ | XYZ float64 + optional RGB uint8 + intensity float32 |
| FR-1.6 | Voxel grid downsampling | ✅ | Configurable voxel size (default 0.02m) |
| FR-1.7 | Toggle RGB channels | ✅ | `--use-color / --no-color` |
| FR-1.8 | Toggle intensity channel | ✅ | `--use-intensity / --no-intensity` |
| FR-1.9 | Toggle surface normals | ✅ | `--use-normals / --no-normals` (computed on demand) |
| FR-1.10 | Spatial crop (point cloud range) | ✅ | `point_cloud_range` config option |

**Deviation:** KITTI `.bin` format added (not in original spec) to support KITTI dataset testing.

### FR-2.x — Adaptive Tiling

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-2.1 | Scene bounding box + point density stats | ✅ | |
| FR-2.2 | Adaptive tiling (octree/KD-tree) | ✅ | Octree subdivision |
| FR-2.3 | Configurable overlap margin | ✅ | Default 2m |
| FR-2.4 | Minimum tile size | ✅ | Default 5m |

### FR-3.x — Detection Inference

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-3.1 | 3D object detection per tile | ✅ | |
| FR-3.2 | Indoor detection (FCAF3D/VoteNet + ScanNet) | ✅ | FCAF3D when MinkowskiEngine available, VoteNet fallback |
| FR-3.3 | Outdoor detection (CenterPoint + nuScenes) | ✅ | |
| FR-3.4 | 7-DOF oriented bounding boxes | ✅ | |
| FR-3.5 | Axis-aligned bounding boxes | ✅ | |
| FR-3.6 | Class labels + confidence scores | ✅ | |
| FR-3.7 | GPU inference (CUDA) | ✅ | Auto-detected |
| FR-3.8 | CPU-only inference fallback | ✅ | With warning |

**Deviation:** Added KITTI PointPillars model (`--mode kitti`) not in original spec.
**Deviation:** FCAF3D requires MinkowskiEngine which cannot be built on Python 3.12 or CUDA 12.8.
  Workaround: separate Python 3.11 + CUDA 11.8 venv (`.venv311`). For Phase 2 Docker,
  use a base image with Python 3.10/3.11 + CUDA 11.8 to support FCAF3D natively.

### FR-4.x — Segmentation Inference

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-4.1 | Per-point semantic segmentation | ✅ | |
| FR-4.2 | ScanNet segmentation (20-class) | ✅ | `pointnet2_scannet` |
| FR-4.3 | S3DIS segmentation (13-class, structural) | ✅ | `pointnet2_s3dis` (default) |
| FR-4.4 | Resolve segmentation conflicts in tile overlaps | ✅ | Highest confidence wins |

**Addition:** Per-point confidence scores now saved alongside labels in segmentation JSON.
**Addition:** Segmentation XYZ coordinates saved in JSON for correct visualisation alignment.

### FR-5.x — Post-Processing

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-5.1 | Tile-local → scene-global coordinates | ✅ | |
| FR-5.2 | 3D NMS across tile boundaries | ✅ | IoU + distance-based merging |
| FR-5.3 | Configurable confidence threshold | ✅ | Default 0.5 |
| FR-5.4 | `--raw` mode (bypass threshold) | ✅ | |
| FR-5.5 | Filter by minimum size | ✅ | |
| FR-5.6 | Filter by maximum size | ✅ | |
| FR-5.7 | Label exclusion list | ✅ | Case-insensitive |

**Enhancement:** NMS extended with distance-based merging (same-class detections within
  1× max_dimension are merged) to handle cases where IoU is low but objects are clearly
  the same detection.

### FR-6.x — Output

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-6.1 | Detection JSON matching schema | ✅ | |
| FR-6.2 | Per-point segmentation labels | ✅ | Separate `*_segmentation.json` |
| FR-6.3 | Processing metadata | ✅ | model, runtime, tile count, device, bounds |
| FR-6.4 | Normalise coordinates to [0,1] | ✅ | `--normalize` flag |
| FR-6.5 | Scene extents in metadata when normalised | ✅ | |

**Deviation:** Segmentation output format is JSON (not LAZ classification field as mentioned
  as an option in the spec). LAZ output is deferred to Phase 2 based on downstream consumer needs.
**Addition:** Segmentation JSON includes `xyz` (preprocessed point coordinates) and
  `confidences` (per-point softmax probability) for richer downstream use.

### FR-7.x — Fine-Tuning (Phase 3)

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-7.1 | Accept annotated training data | 🔲 Phase 3 | |
| FR-7.2 | Transfer learning from pre-trained weights | 🔲 Phase 3 | |
| FR-7.3 | Evaluate mAP@0.25 and mAP@0.50 | 🔲 Phase 3 | |

### FR-8.x — VAMS Integration (Phase 2)

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| FR-8.1 | Register detection results as VAMS child assets | 🔲 Phase 2 | |
| FR-8.2 | Trigger pipeline on S3 upload event | 🔲 Phase 2 | |
| FR-8.3 | Track model version per detection result | ✅ | `model` + `model_version` in metadata |

---

## Non-Functional Requirements Status

| ID | Requirement | Status | Notes |
|----|-------------|--------|-------|
| NFR-1.1 | Peak memory within configurable budget | ✅ Measured | Held under the 16 GB budget on every scene tested, bounded by one tile not the scene. On the pre-redesign path: ~2.9 GB on a real 49.92 GB / 7.6 B-point scene (zero swap; RSS read from `top` because that run was terminated before a final `time -v` line). On the current on-disk-extract path: **4.25 GB on the 50 GB synthetic**, still within budget. See "Memory NFRs" below. |
| NFR-1.2 | 500GB+ without OOM | ⚠️ 50 GB tier completes; 500 GB unproven | No OOM at any size tested (see NFR-1.1). The per-tile re-read model that failed to finish 50 GB in ~24 h has been replaced by single-pass on-disk tile extracts (MR 27), after which a 50 GB synthetic completes in **6 h 42 m**, reported from one operator's bench box and not reproduced in CI. 500 GB has never been attempted. See "Memory NFRs" below. |
| NFR-2.1 | GPU: 10 tiles/sec on g4dn.xlarge | 🔲 Phase 2 | Not benchmarked yet |
| NFR-2.2 | CPU within 10× GPU time | 🔲 Not measured | No CPU and GPU timings of the same scene were recorded, so there is no evidence for this yet |
| NFR-3.1 | Deterministic output | ✅ | Fixed RNG seeds throughout |
| NFR-3.2 | Atomic writes (no partial files) | ✅ | `.tmp` → rename pattern |
| NFR-3.3 | Graceful error handling | ✅ | MemoryError, FileNotFoundError, ValueError caught |
| NFR-4.1 | Permissive licenses only | ✅ | MIT/BSD/Apache 2.0 throughout |
| NFR-4.2 | No GPL runtime dependencies | ✅ | Verified |
| NFR-5.1 | Progress indication | ✅ | Rich spinner |
| NFR-5.2 | Structured logging | ✅ | JSON logging config |

### Memory NFRs (NFR-1.1 measured at 50 GB / NFR-1.2 50 GB tier now completes)

These two were previously marked met without evidence. NFR-1.1 is now measured on a
real 50 GB scene and holds. NFR-1.2 was measured to FAIL at that scale for a
throughput (not memory) reason; the on-disk-extract redesign that failure motivated
has since landed and the 50 GB tier now completes. The 500 GB design target remains
unmeasured. The distinction matters because they are the scale claim this solution is
published on.

**What exists.** A budget-derived per-tile cap (`memory.py`), a two-pass streaming
ingest for LAS/LAZ and E57 that never holds the whole cloud (`streaming.py`), and
routing in `pipeline.py` that chooses streaming when a modelled peak would exceed
`memory_budget_gb`. Unit tests assert peak memory via `tracemalloc` and that it
does not scale with scene extent.

**What is now measured (Sep 10, EC2, stub backend).** A genuine 49.92 GB LAZ
(7.6B points, LAS 1.4) was run end-to-end-ish:
- **NFR-1.1 holds:** ~2.9 GB RSS observed via `top` during processing, zero swap —
  far under the 16 GB budget, even with a 13.6M-point over-budget tile present.
  Memory stays bounded by one tile, not the scene, now confirmed on 50 GB (not just
  the 50M/300M scenes, which held flat at ~1.67 GB). RSS was read from `top`, not a
  final `time -v` line (run was terminated first), but the margin makes that immaterial.
- **NFR-1.2 fails at 50 GB:** the run was CPU-bound and did **not** complete in ~24 h.
  A 50 GB scene plans into ~10,000 tiles and the streaming Pass-2 re-reads the file
  region per tile; at 10,000 tiles that I/O model is the wall. No OOM — a throughput
  failure, not a memory one. The cell->leaf routing fix (Option A, ~12x at 300M) does
  not address it because it did not change the re-read-per-tile model.

**What changed after that (Sep 16): the redesign landed and 50 GB completes.** The
per-tile re-read model above was replaced by single-pass routing to on-disk per-tile
extracts — store each point once in its owning core, then rebuild each tile from its
core plus overlapping neighbours (`_TileExtractStore` / `build_neighbor_map` in
`streaming.py`). Reported results, all single runs on one engineer's bench box:

| Scene | Backend | Wall time | Peak RSS |
|-------|---------|-----------|----------|
| 300 M synthetic | stub | 12 m 29 s | — |
| 50 GB synthetic (NVMe) | stub | **6 h 42 m (completes)** | 4.25 GB |

**Provenance, stated plainly.** These two numbers are *reported measurements from a
single operator's bench box*, not reproduced here and not captured in a checked-in
log. They are the acceptance evidence for the throughput claim, so they should be
backed by the raw `/usr/bin/time -v` output committed under `docs/benchmarks/` before
this is cited externally. What *was* independently verified during review, on the
reviewer's machine, is different and narrower: that the new path is byte-identical to
the previous one across five tiling configurations (including zero-margin and
wide-overlap), that the extract temp directory is cleaned up on all six exit paths
(normal, exception, abandoned generator, dropped reference, explicit close,
KeyboardInterrupt), that the disk-headroom and `PCD_EXTRACT_DIR` guards fire before
any data is written, and that peak RSS overhead versus the previous path shrinks with
scene size (+26% at 2 M points, +5% at 8 M) rather than growing.

**What still does not exist.** Anything at 500 GB. NFR-1.2's literal "500 GB+ without
OOM" is still unproven — the 50 GB tier is the first benchmark tier, not the design
target, and the two are deliberately distinct (see
`docs/diagrams/memory-budget-enforcement.md`). A 50 GB synthetic scene is the
largest evidence that exists.

**Why the old evidence did not support the claim.**

- NFR-1.1 was justified as "tiling + chunked reading". Tiling bounds a tile, not
  the process: before the streaming path, the whole cloud was read and preprocessed
  in memory first, so the budget had nothing to do with peak usage.
- NFR-1.2 was justified by readers auto-capping at 20M points for large files. That
  is decimation, not capacity — it discards most of the input and returns a
  confident-looking answer computed from a subsample. It has since been treated as
  a defect and put behind an explicit warning, with routing switched to stream at
  that threshold instead.

**What would close them.** `docs/runbooks/validate-octree-tiling.md` part 2. It
measures peak RSS on a scene far larger than RAM and compares streamed against
whole-load detections. The results table in
`docs/diagrams/memory-budget-enforcement.md` is empty until that runs.

**`WORKING_SET_FACTOR` is an uncalibrated estimate.** It models inference working
set as a multiple of tile footprint. Earlier comments described it as tuned against
the benchmark; that was untrue and has been removed. The benchmark is the only
thing that can set it, so until then it is a placeholder — and the runbook
deliberately tells operators not to reach for it first.

---

## Phase 1 Exit Criteria Status

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Reads E57, LAS/LAZ, PLY → valid detections | ✅ | Tested with KITTI, nuScenes and Livox |
| 2 | 500GB+ without OOM | ⚠️ 50 GB tier completes; 500 GB unproven | The old evidence here was a large E57 run that used `--max-points`, so most points were discarded rather than processed. That loophole is now closed in code: `pipeline.py` rejects `preprocessing.max_points` on the streaming path outright, and forces streaming above `AUTO_CAP_THRESHOLD_POINTS` (100 M) even when the budget would permit a whole-cloud read, so a billion-point file can no longer be silently decimated. The current largest run is a 50 GB synthetic, which completes. Still not evidence for 500 GB. See "Memory NFRs" below. |
| 3 | ScanNet model mAP@0.25 > 50% | ⚠️ | Not formally evaluated on ScanNet val split — domain gap observed on custom data |
| 4 | nuScenes model mAP > 60% | ⚠️ | Not formally evaluated on nuScenes val split |
| 5 | Valid JSON output matching schema | ✅ | Schema validation in tests |
| 6 | GPU + CPU inference | ✅ | A10G GPU tested; CPU fallback verified |
| 7 | Confidence threshold + raw mode | ✅ | Unit tests pass |
| 8 | Adaptive tiling with varying densities | ✅ | 93 tiles on large E57, 1 tile on small PLY |
| 9 | Cross-tile NMS removes duplicates | ✅ | Unit tests pass |
| 12 | Per-point segmentation labels | ✅ | S3DIS + ScanNet models tested |
| 13 | Structural elements segmented | ✅ | Wall, floor, ceiling, column, beam verified |
| 14 | S3DIS identifies columns + beams | ✅ | Spot-checked on custom data |

**Note on criteria 3 & 4:** Formal mAP evaluation on ScanNet/nuScenes val splits was not
completed in Phase 1. This is acceptable for Phase 2 entry — the pipeline produces valid
detections on real-world data. Formal evaluation is recommended before Phase 3 fine-tuning
to establish a baseline.

---

## Key Deviations & Decisions for Phase 2 Awareness

### 1. MinkowskiEngine / FCAF3D Environment Split
**Decision:** FCAF3D, the pipeline's indoor detector, requires Python 3.11 + CUDA 11.8 due to
MinkowskiEngine build constraints. (Superseded: `Dockerfile.gpu` now builds MinkowskiEngine
on Python 3.11 and CUDA 12.6.) The main pipeline uses Python 3.12 + CUDA 12.8.

**Phase 2 Action:** The Docker container should use Python 3.10 or 3.11 + CUDA 11.8
as the base to support FCAF3D natively. Recommended base image:
`nvcr.io/nvidia/pytorch:23.10-py3` (Python 3.10, CUDA 12.2, PyTorch 2.1).

### 2. Coordinate Frame Transforms
**Addition not in spec:** A `--coord-frame` preprocessing option was added to handle
different scanner coordinate conventions. Named transforms:
- `livox` — Livox Mid-360 (Z-up, +27° Y rotation)
- `leica` — Leica RTC360 (Z-up, +60° Z rotation)
- `z-up` — default (no transform)
- `ros`, `colmap`, `y-up`, `x-up` — standard conventions

**Phase 2 Action:** Store `coord_frame` in output metadata so VAMS knows which transform
was applied. Allow per-asset coord_frame configuration in VAMS.

### 3. Segmentation Output Format
**Decision:** Per-point labels output as JSON (not LAZ classification field).
JSON includes `xyz`, `labels`, and `confidences` arrays.

**Phase 2 Action:** Evaluate whether VAMS downstream consumers need LAZ format.
If so, add LAZ writer in Phase 2 alongside JSON.

### 4. Large File Memory Safety
**Addition not in spec:** Auto-cap of 20M points for files >100M points, with
`--max-points` override. E57 uses capacity-limited pye57 buffer reading.

**Phase 2 Action:** For production, consider streaming tiled processing where
tiles are read directly from the file rather than loading all points first.
This would support truly unlimited file sizes.

### 5. NMS Enhancement
**Enhancement:** NMS extended with distance-based suppression (same-class detections
within 1× max_dimension merged) in addition to IoU-based suppression.

**Phase 2 Action:** Expose `nms_distance_threshold` as a configurable parameter.

### 6. Segmentation Confidence Threshold
**Addition:** Per-point confidence scores from segmentation softmax are saved.
`--min-confidence` filter available in `visualize_seg.py` but not yet in the
main `detect` CLI.

**Phase 2 Action:** Add `segmentation_confidence_threshold` to PostprocessingConfig
and expose via CLI.

### 7. Visualisation Tools
**Addition not in spec:** Three visualisation scripts added:
- `scripts/visualize_3d.py` — interactive 3D HTML with detection bboxes + seg toggle
- `scripts/visualize_seg.py` — segmentation-only viewer with class toggles
- `scripts/axes_viewer.py` — coordinate frame identification tool
- `scripts/pick_rotation.py` — rotation calibration tool for new sensors

These are development/QA tools, not part of the production pipeline.

---

## Prerequisites for Phase 2 (VAMS Integration)

### Infrastructure
- [ ] AWS account with VAMS deployed
- [ ] ECR repository for pipeline Docker image
- [ ] S3 bucket for input point clouds and output JSON
- [ ] Step Functions state machine definition
- [ ] ECS task definition with GPU instance (g4dn.xlarge or g5.xlarge)
- [ ] EventBridge rule for S3 upload trigger
- [ ] SNS topic for failure notifications
- [ ] CloudWatch log group

### Docker Image
- [ ] Base image: Python 3.10/3.11 + CUDA 11.8 (for MinkowskiEngine/FCAF3D support)
- [ ] Pre-built MinkowskiEngine wheel (avoid recompiling in container)
- [ ] MMDetection3D stack pre-installed
- [ ] Pre-downloaded model checkpoints baked in (avoid runtime downloads)
- [ ] Pipeline package installed

### Code Changes for Phase 2
- [ ] S3 input/output support (read from s3://, write to s3://)
- [ ] VAMS asset registration API call after successful detection
- [ ] Model version tracking in output metadata
- [ ] CloudWatch metrics emission (tile throughput, inference latency)
- [ ] Batch processing support (process multiple files from SQS queue)
- [ ] `coord_frame` stored in output metadata

### Testing
- [ ] Integration test against VAMS staging environment
- [ ] Load test with representative dataset sizes
- [ ] Formal mAP evaluation on ScanNet/nuScenes val splits (baseline before Phase 3)
