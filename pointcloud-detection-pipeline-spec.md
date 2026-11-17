# Point Cloud Object Detection Pipeline Spec Doc

This is a requirements and specification document for a point cloud object detection pipeline that can:

1. Ingest E57, LAS/LAZ, and PLY files containing LiDAR point clouds of large scenes
2. Tile large scenes adaptively and run 3D object detection on each tile
3. Detect objects using pre-trained models (indoor + outdoor classes) with a path to fine-tuning
4. Produce per-point semantic segmentation alongside bounding-box detections
5. Output oriented and axis-aligned 3D bounding boxes, class labels, and confidence scores in JSON
6. Run standalone as a Python CLI and integrate into AWS VAMS as a managed pipeline

The pipeline is developed in three phases:
- **Phase 1**: Standalone Python CLI (no Docker) that proves detection and segmentation on local point clouds
- **Phase 2**: Containerised (Docker) integration into AWS VAMS as a managed pipeline, triggered by asset ingest
- **Phase 3**: Fine-tuning workflow for training on custom annotated LiDAR datasets


## Problem Statement

On construction and infrastructure projects, large LiDAR point clouds are captured for as-built verification, progress monitoring, and clash detection. These datasets contain thousands of objects (structural elements, MEP, furniture, vehicles, temporary works) that currently require manual identification and tagging.

Manual object identification in point clouds is:
- Time-consuming: a single large scan can take days to annotate by hand
- Inconsistent: different operators tag objects differently
- Not scalable: dataset volumes are growing faster than manual capacity
- Not repeatable: no audit trail of what was identified and when

Automated 3D object detection enables:
- Rapid scene understanding from raw LiDAR captures
- Consistent, repeatable object inventories with confidence scores
- A foundation for automated progress tracking (detecting what's been installed)
- Input to downstream clash detection and digital twin workflows
- Scalable processing of the growing volume of site capture data


## Requirements

Functionality:

- Parse E57, LAS/LAZ, and PLY files and extract point cloud data (XYZ, intensity, RGB, normals)
- Accept preprocessing parameters: downsample voxel size, use colour (RGB), use intensity, use normals
- Optionally downsample the point cloud before inference (voxel grid filter with configurable resolution)
- Optionally include or exclude colour/intensity/normal channels based on model requirements and user preference
- Support large-scale datasets (50–500GB+) via adaptive tiling with density-aware tile sizing
- Run 3D object detection producing 7-DOF oriented bounding boxes (center, size, heading) per detected object
- Also produce axis-aligned bounding boxes (AABB) for simpler downstream spatial queries
- Assign class labels and confidence scores to each detection
- Perform per-point semantic segmentation within the full scene
- Support both indoor (ScanNet 18-class) and outdoor (nuScenes 10-class) pre-trained detection model configurations
- Support structural segmentation via ScanNet (20-class: wall, floor, ceiling, door, window + furniture) and S3DIS (13-class: wall, floor, ceiling, column, beam, door, window + furniture)
- Provide configurable confidence threshold with a default (e.g., 0.5) and a raw/unfiltered mode
- Filter detections by minimum and maximum size (based on bounding box dimensions)
- Exclude specific labels via a configurable exclusion list (e.g., ignore "curtain", "other furniture")
- Merge detections across tile boundaries using Non-Maximum Suppression (NMS)
- Output detections as a structured JSON file per input scene
- Support CPU-only inference (slow) and GPU-accelerated inference (CUDA)
- Provide a clear path for fine-tuning models on custom annotated LiDAR datasets
- Handle datasets that exceed available RAM through streaming/chunked point cloud reading

Design:

- Two-phase architecture: standalone Python CLI first, then VAMS-integrated AWS pipeline
- Adaptive tiling: smaller tiles in dense regions, larger tiles in sparse regions, with configurable overlap
- Model-agnostic inference interface: swap detection backends (MMDetection3D, OpenPCDet) without changing the pipeline
- Configurable via YAML/JSON manifest specifying model config, tile parameters, class mapping, and thresholds
- Idempotent: re-running with the same inputs and config produces identical outputs (deterministic inference)
- Clear separation between I/O (format reading/writing), tiling, inference, post-processing, and output
- Extensible to support future model architectures and additional output formats without core refactoring


## Technical Specifications

Phase 1 - Standalone Python CLI (no containerisation):

- Runs directly on a developer workstation or server; no Docker required
- Input: E57 files (ASTM E2807), LAS/LAZ files (ASPRS LAS 1.2–1.4), and PLY files containing unstructured/structured point clouds
- Point cloud I/O via pye57 (E57), laspy (LAS/LAZ), Open3D or trimesh (PLY) — all permissive licenses (MIT/BSD/Apache)
- Format normalisation: all inputs converted to numpy arrays (XYZ + optional intensity/RGB/normals) before preprocessing
- Preprocessing parameters (all configurable via CLI flags or YAML config):
  - Downsample: optional voxel grid downsampling (configurable voxel size, e.g., 0.02m) to reduce point count before inference
  - Use colour: toggle whether RGB channels are passed to the model (some models are geometry-only)
  - Use intensity: toggle whether intensity channel is included as a model feature
  - Use normals: toggle whether surface normals are computed/included as model features
  - Point cloud range: optional spatial crop (min/max XYZ) to limit inference to a region of interest
- Adaptive tiling engine:
  - Compute scene bounding box and point density distribution
  - Subdivide using octree or KD-tree partitioning until each tile fits within configured max-points threshold (default: 100K points)
  - Apply configurable overlap margin (default: 2m) at tile boundaries
  - Track tile origins for coordinate back-projection of detections to scene space
- Detection backend: MMDetection3D (Apache 2.0) as primary framework
  - Indoor: FCAF3D or TR3D with ScanNet pre-trained weights (18 classes — discrete objects: door, window, furniture)
  - Outdoor: CenterPoint or DSVT with nuScenes pre-trained weights (10 classes)
  - Model selection via config (user specifies indoor/outdoor/custom)
  - Detection targets discrete objects that can be bounded (doors, windows, furniture, vehicles)
- Segmentation backend: MMDetection3D semantic segmentation head or Pointcept PTv3 (MIT)
  - Per-point class labels for the full scene
  - Runs alongside or after detection depending on architecture
  - Segmentation targets both discrete objects AND planar structural elements (walls, floors, ceilings, columns, beams)
  - Model configs:
    - ScanNet segmentation (20-class): includes wall, floor, ceiling, door, window + furniture
    - S3DIS segmentation (13-class): includes wall, floor, ceiling, column, beam, door, window — more relevant for structural/construction scenes
  - Config selects which segmentation model to use based on scene type
- Post-processing:
  - Transform tile-local detections back to scene-global coordinates
  - 3D NMS across overlapping tile boundaries (IoU threshold configurable, default: 0.3)
  - Compute AABB from OBB for each detection
  - Apply confidence threshold filter (configurable, default: 0.5)
- Output coordinate normalisation (optional):
  - Default: output bounding boxes in world-scale coordinates matching the input point cloud
  - Normalised mode: scale detection coordinates to [0, 1] range based on scene extents (bounding box min/max)
  - Useful when downstream consumers expect unit-cube coordinates or when comparing detections across scenes of different scale
  - Normalisation metadata (scene extents used for scaling) included in output JSON so coordinates can be converted back to world-scale
- Inference device: auto-detect CUDA availability; fall back to CPU with warning about performance
- Memory management: stream points from large files in configurable chunk sizes; only load one tile into GPU memory at a time
- Output: one JSON file per input scene containing all detections and per-point labels

Phase 2 - VAMS AWS Pipeline Integration (containerised):

- Containerisation: Docker image with MMDetection3D + CUDA runtime, built and pushed to ECR
- Execution environment: match existing VAMS pipeline pattern (ECS/Fargate with GPU instances for inference, Lambda-triggered orchestration via Step Functions)
- Trigger: S3 event on E57/LAS/LAZ/PLY upload to designated ingest bucket, or explicit API call
- Pipeline steps: Validate Input → Tile Scene → Run Detection → Run Segmentation → Merge Results → Write Output → Update VAMS Asset Metadata → Notify
- GPU compute: ECS tasks on p3/g4dn instances with NVIDIA runtime for inference; CPU fallback on m5 instances
- Integration with VAMS asset hierarchy: detection results registered as child assets of the source point cloud
- Support batch processing: queue multiple scenes and process sequentially or in parallel (Step Functions Map state)
- Fine-tuning workflow: separate training pipeline that accepts annotated data, produces model weights, registers them as VAMS assets
- Error handling: dead-letter queue for failed detections; notification via SNS/EventBridge
- Model versioning: track which model version produced each detection result for reproducibility

Phase 3 - Fine-Tuning on Custom Data:

- Accept annotated training data: point clouds + 7-DOF bounding box annotations + class labels
- Annotation format: JSON with per-scene list of boxes (center_xyz, size_wlh, heading, class_name)
- Training pipeline: MMDetection3D training loop with transfer learning from pre-trained weights
- Evaluation: compute mAP@0.25 and mAP@0.50 on held-out validation split
- Model registry: versioned model artifacts stored in S3 / VAMS with training metadata
- A/B comparison: run old vs new model on same scenes, output comparison report


## Tech Stack

Phase 1 (Python CLI):

| Component | Library | License | Purpose |
|-----------|---------|---------|---------|
| E57 I/O | pye57 | MIT | Read ASTM E2807 E57 files |
| LAS/LAZ I/O | laspy | BSD-2-Clause | Read LAS/LAZ with chunked streaming |
| PLY I/O | Open3D or trimesh | MIT | Read PLY format point clouds |
| Point cloud ops | numpy | BSD | Array operations, coordinate transforms |
| Spatial indexing | scipy.spatial | BSD | KD-tree for adaptive tiling |
| Detection framework | MMDetection3D | Apache 2.0 | 3D object detection inference + training |
| Segmentation | MMDetection3D / Pointcept | Apache 2.0 / MIT | Per-point semantic labels |
| Deep learning | PyTorch | BSD | Model inference and training |
| CUDA acceleration | PyTorch CUDA | BSD | GPU inference |
| CLI | typer | MIT | Command-line interface |
| Config | pydantic | MIT | Typed configuration models |
| Output | orjson / json | MIT / stdlib | Fast JSON serialisation |
| Testing | pytest + anyio | MIT | Unit and integration tests |
| Formatting | ruff | MIT | Linting and formatting |
| Type checking | pyright | MIT | Static type analysis |
| Package management | uv | MIT | Dependency management and execution |

Phase 2 (AWS/VAMS):

| Component | Service | Purpose |
|-----------|---------|---------|
| Orchestration | AWS Step Functions | Pipeline state machine |
| Compute (GPU) | AWS ECS/Fargate (g4dn) | GPU-accelerated inference tasks |
| Compute (CPU) | AWS ECS/Fargate (m5) | Tiling, post-processing, fallback inference |
| Trigger | S3 Events + EventBridge | Ingest detection and pipeline initiation |
| Storage | S3 | Input point clouds, output JSON, model weights |
| Metadata | VAMS DynamoDB | Asset metadata, detection results, model versions |
| Notifications | SNS | Alerts for pipeline failures |
| Monitoring | CloudWatch | Pipeline execution logs, inference latency metrics |
| Container registry | ECR | Docker images with MMDetection3D + CUDA runtime |
| Model registry | S3 + VAMS | Versioned model weights and training metadata |


## Data Flow

```
E57 / LAS / LAZ / PLY Input (S3 or local)
    |
    v
[1. Parse & Normalise]
    - Detect input format (E57, LAS, LAZ, PLY)
    - Stream point data in chunks (XYZ, intensity, RGB)
    - Convert to unified numpy representation
    - Compute scene bounding box and point statistics
    |
    v
[2. Adaptive Tiling]
    - Analyse point density distribution across scene
    - Subdivide into tiles (octree/KD-tree) respecting max-points-per-tile
    - Apply overlap margins at tile boundaries
    - Record tile origins and extents for coordinate mapping
    |
    v
[3. Detection Inference]
    - Load model (indoor/outdoor/custom) based on config
    - For each tile:
        - Prepare point tensor (voxelise if required by model)
        - Run forward pass (GPU if available, CPU fallback)
        - Collect 7-DOF OBB predictions + scores + labels
    - Transform tile-local predictions to scene-global coordinates
    |
    v
[4. Segmentation Inference]
    - For each tile:
        - Run segmentation head (shared backbone or separate model)
        - Assign per-point class labels
    - Resolve conflicts in overlap regions (majority vote or max confidence)
    |
    v
[5. Post-Processing & Merge]
    - 3D NMS across all tiles (remove duplicate detections in overlaps)
    - Compute AABB from each OBB
    - Apply confidence threshold (or pass through all if --raw)
    - Aggregate per-point labels into scene-level segmentation
    |
    v
[6. Output]
    - Write detection JSON (bounding boxes, labels, confidence)
    - Write segmentation output (per-point labels as LAZ classification or JSON index)
    - Write processing metadata (model version, tile params, runtime stats)
    |
    v
[7. Report / Register]
    - Processing summary (object counts per class, runtime, tile count)
    - Asset registration (VAMS phase only)
    - Notification on completion or failure
```


## Output JSON Schema

```json
{
  "metadata": {
    "source_file": "site_scan_001.e57",
    "model": "mmdet3d_fcaf3d_scannet",
    "model_version": "1.0.0",
    "confidence_threshold": 0.5,
    "tile_count": 142,
    "processing_time_seconds": 387.2,
    "device": "cuda:0",
    "scene_bounds": {
      "min": [0.0, 0.0, -2.0],
      "max": [150.0, 200.0, 15.0]
    }
  },
  "detections": [
    {
      "id": "det_0001",
      "label": "chair",
      "confidence": 0.92,
      "bbox_obb": {
        "center": [12.5, 34.2, 0.45],
        "size": [0.6, 0.6, 0.9],
        "heading": 1.57
      },
      "bbox_aabb": {
        "min": [12.1, 33.8, 0.0],
        "max": [12.9, 34.6, 0.9]
      }
    }
  ],
  "segmentation": {
    "format": "point_index_map",
    "classes": ["wall", "floor", "chair", "table", "..."],
    "file": "site_scan_001_segmentation.json"
  }
}
```


## Adaptive Tiling Strategy

The core challenge: large-scale point clouds (50–500GB+) cannot fit in GPU memory for single-pass inference. Models expect bounded input (typically 50K–100K points per forward pass).

The adaptive tiling approach:

1. **Density estimation**: compute point density across the scene bounding box using a coarse 3D grid (e.g., 10m cells)
2. **Recursive subdivision**: use octree decomposition, splitting cells that exceed the max-points threshold
3. **Overlap margins**: extend each tile boundary by a configurable margin (default 2m) to avoid cutting objects at tile edges
4. **Merge strategy**: 3D NMS with IoU threshold removes duplicate detections from overlap regions
5. **Memory budget**: process tiles sequentially (or in small batches if GPU memory allows), releasing memory between tiles _(implemented: `memory_budget_gb` drives a per-tile cap plus routing between whole-load and a two-pass streaming ingest that never holds the whole cloud. Bounded by construction and by unit test, but not yet measured on a large scene — NFR-1.1/1.2 are implemented, not verified. See `docs/diagrams/memory-budget-enforcement.md`)_

Configuration parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_points_per_tile` | 100,000 | Maximum points loaded per inference pass |
| `overlap_margin_m` | 2.0 | Overlap between adjacent tiles in metres |
| `min_tile_size_m` | 5.0 | Minimum tile dimension (prevents over-subdivision) |
| `nms_iou_threshold` | 0.3 | IoU threshold for cross-tile NMS |
| `density_grid_resolution_m` | 10.0 | Coarse grid cell size for density estimation |


## Downstream Analysis Dependencies

The detection and segmentation outputs must support the following downstream operations. The pipeline must produce data in formats and at quality levels required by each.

### Clash Detection & Spatial Alignment
- Requires: accurate 3D bounding boxes in the same CRS as the design model
- Consumed attributes: OBB coordinates, class labels (to match against design elements)
- Integration: bounding boxes compared against BIM element positions to flag spatial conflicts

### Digital Twin / Asset Registry
- Requires: object inventory with class labels, confidence, and spatial location
- Consumed attributes: detection JSON (full schema), segmentation labels
- Integration: detected objects registered in asset management systems for tracking

### Progress Monitoring
- Requires: repeatable detection across time-series scans of the same area
- Consumed attributes: class labels, bounding box positions, model version (for consistency)
- Integration: compare detections between scan dates to identify newly installed elements

### Automated Tagging (3D Feature Extraction)
- Requires: per-point semantic labels to isolate object regions for detailed feature extraction
- Consumed attributes: segmentation point indices, class labels
- Integration: segmented regions passed to downstream feature extraction pipelines


## Configuration Schema

```yaml
pipeline:
  name: "pointcloud-detection"
  version: "1.0"

input:
  formats: ["e57", "las", "laz", "ply"]  # accepted input formats
  max_file_size_gb: null                  # null = no limit

preprocessing:
  downsample: true                        # enable voxel grid downsampling
  voxel_size_m: 0.02                      # voxel size in metres (smaller = more points retained)
  use_color: true                         # pass RGB channels to model
  use_intensity: false                    # pass intensity channel to model
  use_normals: false                      # compute and pass surface normals to model
  point_cloud_range: null                 # optional spatial crop [x_min, y_min, z_min, x_max, y_max, z_max]

model:
  mode: "indoor"                          # "indoor" | "outdoor" | "custom"
  detection:
    backend: "mmdetection3d"
    config: "fcaf3d_scannet"              # model config name
    weights: "pretrained"                 # "pretrained" | path to .pth file
  segmentation:
    enabled: true
    backend: "mmdetection3d"
    config: "pointnet2_s3dis"             # "pointnet2_scannet" (20-class) | "pointnet2_s3dis" (13-class, structural)
    weights: "pretrained"

tiling:
  max_points_per_tile: 100000
  overlap_margin_m: 2.0
  min_tile_size_m: 5.0
  density_grid_resolution_m: 10.0
  memory_budget_gb: 16

inference:
  device: "auto"                          # "auto" | "cuda:0" | "cpu"
  batch_size: 1                           # tiles per batch (increase if GPU memory allows)
  precision: "fp32"                       # "fp32" | "fp16" (GPU only)

postprocessing:
  nms_iou_threshold: 0.3
  confidence_threshold: 0.5
  raw_mode: false                         # if true, bypass confidence filter
  min_size_m: null                        # minimum detection size (max bbox dimension), null = no limit
  max_size_m: null                        # maximum detection size (max bbox dimension), null = no limit
  excluded_labels: []                     # labels to exclude, e.g. ["curtain", "other furniture"]

output:
  directory: "./output"
  naming: "{input_stem}_detections"
  include_segmentation: true
  include_aabb: true
  normalize_coordinates: false            # if true, scale bbox coords to [0,1] based on scene extents
  format: "json"                          # output format for detections

logging:
  level: "INFO"
  format: "json"                          # structured logging
  progress: true                          # show progress bar
```


## Pre-trained Model Classes

### Detection Models (bounding-box output for discrete objects)

Indoor Detection (ScanNet 18-class):

cabinet, bed, chair, sofa, table, **door**, **window**, bookshelf, picture, counter, desk, curtain, refrigerator, shower curtain, toilet, sink, bathtub, other furniture

Outdoor Detection (nuScenes 10-class):

car, truck, bus, trailer, construction vehicle, pedestrian, motorcycle, bicycle, traffic cone, barrier

### Segmentation Models (per-point labels for all elements including structural)

Indoor Segmentation — ScanNet (20-class):

**wall**, **floor**, **ceiling**, **door**, **window**, chair, table, sofa, bed, desk, cabinet, bookshelf, curtain, counter, refrigerator, shower curtain, toilet, sink, bathtub, other furniture

Indoor Segmentation — S3DIS (13-class, structural focus):

**wall**, **floor**, **ceiling**, **column**, **beam**, **door**, **window**, table, chair, sofa, bookcase, board, clutter

### Structural Element Strategy

Planar structural elements (walls, floors, ceilings) are handled by segmentation (per-point labels), not detection (bounding boxes). These elements are surfaces, not discrete bounded objects — a bounding box around a wall is not meaningful.

Discrete structural openings (doors, windows) are handled by both:
- **Detection**: produces a bounding box with label and confidence (useful for counting, spatial queries)
- **Segmentation**: labels all points belonging to the element (useful for isolation and measurement)

| Element | Detection (bbox) | Segmentation (per-point) | Available In |
|---------|:-:|:-:|---|
| Wall | - | Yes | ScanNet-seg, S3DIS |
| Floor | - | Yes | ScanNet-seg, S3DIS |
| Ceiling | - | Yes | ScanNet-seg, S3DIS |
| Column | - | Yes | S3DIS |
| Beam | - | Yes | S3DIS |
| Door | Yes | Yes | ScanNet-det + seg, S3DIS |
| Window | Yes | Yes | ScanNet-det + seg, S3DIS |

### Phase 3 Fine-Tuning: Custom Structural Classes

The pre-trained models cover common indoor structural elements. Phase 3 fine-tuning enables adding site-specific classes not present in public datasets:

Candidate custom classes (to be refined with site teams):
- Entryway / doorframe (distinct from door leaf)
- Partition wall vs structural wall
- Lintel / soffit
- Service penetration
- Scaffolding / temporary works
- Formwork
- Rebar / reinforcement
- MEP runs (pipes, ducts, cable trays)


## Success Criteria

| # | Criterion | Phase | Evidence |
|---|-----------|-------|----------|
| 1 | Reads E57, LAS/LAZ, PLY and produces valid detections | 1 | Integration tests with sample files per format |
| 2 | Processes 500GB+ dataset without OOM | 1 | Streaming + tiling keeps peak memory within configured budget |
| 3 | Detects objects using pre-trained ScanNet model (mAP@0.25 > 50%) | 1 | Evaluation on ScanNet val split |
| 4 | Detects objects using pre-trained nuScenes model (mAP > 60%) | 1 | Evaluation on nuScenes val split |
| 5 | Produces valid JSON output matching schema | 1 | Schema validation tests |
| 6 | Runs on CPU (slower) and GPU (faster) | 1 | CI tests on CPU; benchmarks on GPU |
| 7 | Confidence threshold and raw mode work correctly | 1 | Unit tests for filtering logic |
| 8 | Adaptive tiling handles varying point densities | 1 | Test with sparse outdoor + dense indoor regions |
| 9 | Cross-tile NMS removes duplicate detections | 1 | Synthetic overlap test cases |
| 10 | Fine-tuned model outperforms base model on custom data | 3 | mAP improvement on annotated validation set |
| 11 | Integrates into VAMS with asset registration | 2 | Detection results visible in VAMS asset tree |
| 12 | Per-point segmentation labels output correctly | 1 | Spot-check against ground truth annotations |
| 13 | Structural elements (wall, floor, ceiling) segmented via ScanNet/S3DIS | 1 | Evaluation on respective val splits |
| 14 | S3DIS model identifies columns and beams | 1 | Spot-check on S3DIS val scenes with structural elements |


## Functional Requirements

| ID | Requirement | Phase | Priority |
|----|-------------|-------|----------|
| FR-1.1 | Parse E57 files and extract XYZ + intensity + RGB | 1 | Must |
| FR-1.2 | Parse LAS/LAZ files with chunked/streamed reading | 1 | Must |
| FR-1.3 | Parse PLY files and extract point attributes | 1 | Must |
| FR-1.4 | Auto-detect input format from file extension and magic bytes | 1 | Must |
| FR-1.5 | Convert all formats to unified numpy array representation | 1 | Must |
| FR-1.6 | Optionally downsample point cloud via voxel grid filter (configurable voxel size) | 1 | Must |
| FR-1.7 | Toggle inclusion of colour (RGB) channels based on config | 1 | Must |
| FR-1.8 | Toggle inclusion of intensity channel based on config | 1 | Must |
| FR-1.9 | Toggle computation/inclusion of surface normals based on config | 1 | Should |
| FR-1.10 | Optionally crop to a spatial region of interest (point cloud range) | 1 | Should |
| FR-2.1 | Compute scene bounding box and point density statistics | 1 | Must |
| FR-2.2 | Adaptively tile scene based on point density (octree/KD-tree) | 1 | Must |
| FR-2.3 | Apply configurable overlap margin between adjacent tiles | 1 | Must |
| FR-2.4 | Respect minimum tile size to prevent over-subdivision | 1 | Should |
| FR-3.1 | Run 3D object detection on each tile | 1 | Must |
| FR-3.2 | Support indoor detection model config (FCAF3D/TR3D + ScanNet weights) | 1 | Must |
| FR-3.3 | Support outdoor detection model config (CenterPoint/DSVT + nuScenes weights) | 1 | Must |
| FR-3.4 | Produce 7-DOF oriented bounding boxes per detection | 1 | Must |
| FR-3.5 | Produce axis-aligned bounding boxes per detection | 1 | Must |
| FR-3.6 | Assign class label and confidence score per detection | 1 | Must |
| FR-3.7 | Support GPU inference with CUDA | 1 | Must |
| FR-3.8 | Support CPU-only inference as fallback | 1 | Must |
| FR-4.1 | Run per-point semantic segmentation | 1 | Must |
| FR-4.2 | Support ScanNet segmentation model (20-class: includes wall, floor, ceiling) | 1 | Must |
| FR-4.3 | Support S3DIS segmentation model (13-class: includes column, beam) | 1 | Must |
| FR-4.4 | Resolve segmentation conflicts in tile overlap regions | 1 | Should |
| FR-5.1 | Transform tile-local coordinates to scene-global coordinates | 1 | Must |
| FR-5.2 | Apply 3D NMS across tile boundaries | 1 | Must |
| FR-5.3 | Apply configurable confidence threshold | 1 | Must |
| FR-5.4 | Support --raw mode to bypass threshold filtering | 1 | Must |
| FR-5.5 | Filter detections by minimum size (max bbox dimension) | 1 | Must |
| FR-5.6 | Filter detections by maximum size (max bbox dimension) | 1 | Must |
| FR-5.7 | Exclude detections matching the label exclusion list (case-insensitive) | 1 | Must |
| FR-6.1 | Output detection JSON matching defined schema | 1 | Must |
| FR-6.2 | Output per-point segmentation labels | 1 | Must |
| FR-6.3 | Output processing metadata (model, runtime, tile stats) | 1 | Must |
| FR-6.4 | Optionally normalise output coordinates to [0,1] based on scene extents | 1 | Must |
| FR-6.5 | Include scene extents in output metadata when normalised (for back-conversion) | 1 | Must |
| FR-7.1 | Accept custom annotated training data for fine-tuning | 3 | Must |
| FR-7.2 | Train with transfer learning from pre-trained weights | 3 | Must |
| FR-7.3 | Evaluate fine-tuned model and report mAP metrics | 3 | Must |
| FR-8.1 | Register detection results as VAMS child assets | 2 | Must |
| FR-8.2 | Trigger pipeline on S3 upload event | 2 | Must |
| FR-8.3 | Track model version per detection result | 2 | Must |


## Non-Functional Requirements

| ID | Requirement | Phase | Priority |
|----|-------------|-------|----------|
| NFR-1.1 | Peak memory usage stays within configurable budget (default: 16GB) | 1 | Must |
| NFR-1.2 | Process 500GB+ datasets without crash or OOM | 1 | Must |
| NFR-2.1 | GPU inference processes at least 10 tiles/second on g4dn.xlarge | 2 | Should |
| NFR-2.2 | CPU inference completes within 10x GPU time (acceptable for small datasets) | 1 | Should |
| NFR-3.1 | Deterministic output given same input + config + model | 1 | Must |
| NFR-3.2 | No partial output files on failure (atomic writes) | 1 | Must |
| NFR-3.3 | Graceful error handling for malformed input files | 1 | Must |
| NFR-4.1 | All dependencies permissively licensed (MIT, BSD, Apache 2.0) | 1 | Must |
| NFR-4.2 | No GPL-licensed runtime dependencies | 1 | Must |
| NFR-5.1 | CLI provides progress indication for long-running operations | 1 | Should |
| NFR-5.2 | Structured logging (JSON) for pipeline observability | 1 | Should |


## Phasing & Milestones

### Phase 1: Standalone Python CLI

**Goal**: Working CLI (no Docker) that ingests point cloud files, tiles large scenes, runs 3D object detection and segmentation, and outputs structured JSON with bounding boxes, labels, and confidence scores.

Deliverables:
1. Python package installable via `uv` (runs directly on host, no container)
2. CLI command: `uv run pcdet detect --config detect.yaml input.e57` (also accepts .las/.laz/.ply)
3. Adaptive tiling engine with density-aware subdivision
4. Detection inference with pre-trained ScanNet (indoor) and nuScenes (outdoor) models
5. Per-point semantic segmentation output
6. Cross-tile NMS and result merging
7. JSON output matching defined schema
8. Unit tests for tiling, NMS, and output formatting
9. Integration tests with sample point cloud files (one per supported format)

**Exit criteria**: Successfully processes a multi-room ScanNet scene and a nuScenes outdoor scene, producing valid detection JSON with mAP@0.25 > 50% on the respective validation splits. Structural segmentation (wall, floor, ceiling) produces valid per-point labels on ScanNet and S3DIS val scenes. Runs on both GPU and CPU. Handles a 50GB+ test file without OOM.

### Phase 2: VAMS AWS Pipeline Integration

**Goal**: Containerise Phase 1 logic and deploy as an AWS pipeline within VAMS, triggered by data ingest, with GPU-accelerated inference and asset registration. This is when Docker is introduced.

Deliverables:
1. Dockerfile + Dockerised pipeline container with CUDA runtime (ECR)
2. Step Functions state machine definition (tile → detect → segment → merge → register)
3. VAMS asset registration logic (detection results as child assets)
4. EventBridge rules for S3 upload triggers
5. Model versioning and tracking in VAMS metadata
6. CloudWatch dashboards (inference latency, tile throughput, error rates)
7. Batch processing support (multiple scenes via Step Functions Map)
8. Integration tests against VAMS staging environment

**Exit criteria**: Point cloud uploaded to VAMS triggers detection pipeline; results registered as child asset with model version metadata; pipeline completes within SLA for a representative dataset.

### Phase 3: Fine-Tuning on Custom Data

**Goal**: Enable training custom detection models on annotated site LiDAR data, with evaluation and model registry integration.

Deliverables:
1. Training pipeline accepting annotated point clouds + bounding box labels
2. Transfer learning from pre-trained weights (ScanNet/nuScenes base)
3. Evaluation script computing mAP@0.25 and mAP@0.50 on validation split
4. Model registry integration (versioned weights stored in S3/VAMS)
5. A/B comparison tool (old model vs new model on same scenes)
6. Documentation: annotation format specification, training workflow guide

**Exit criteria**: Fine-tuned model achieves measurable mAP improvement over base model on held-out site data. Model registered and deployable through Phase 2 pipeline without code changes.


## Open Questions

1. **Indoor vs outdoor model selection**: Should the pipeline auto-detect scene type (indoor/outdoor) based on point cloud characteristics, or always require explicit config? — TBD, evaluate feasibility during Phase 1
2. **Annotation tooling**: Which tool will be used to annotate site LiDAR data for fine-tuning? (e.g., SUSTechPOINTS, Label3D, Segments.ai, custom) — TBD, evaluate during Phase 3 planning
3. **Class taxonomy**: What are the priority custom structural classes for construction sites beyond the pre-trained set? (e.g., entryway/doorframe, partition vs structural wall, lintel, soffit, scaffolding, formwork, rebar, MEP runs) — TBD, consult with site teams
4. **CRS alignment**: Should detection be run before or after coordinate transformation? If after, the coord-xform pipeline output feeds directly in. — TBD, evaluate based on pipeline ordering in VAMS
5. **Segmentation output format**: Should per-point labels be written as LAS/LAZ classification field, separate JSON index file, or both? — TBD, driven by downstream consumer needs
6. **Model update cadence**: How often will fine-tuned models be retrained as new annotated data arrives? — TBD, driven by data availability and Phase 3 maturity
7. **Multi-model ensemble**: Should the pipeline support running indoor + outdoor models on the same scene and merging results? — TBD, evaluate if mixed scenes are common
8. **Confidence calibration**: Should raw model confidence scores be calibrated before output, or left as-is? — TBD, evaluate after initial deployment shows score distributions
9. **ScanNet vs S3DIS default**: Should S3DIS be the default segmentation model for construction scenes given its structural focus (columns, beams), or should ScanNet be default for its broader class coverage? — TBD, evaluate on representative site scans during Phase 1
