# Domain Entities — Unit 2: Backend Services

## Entity: Job

Represents a detection processing request.

| Attribute | Type | Description |
|-----------|------|-------------|
| job_id | UUID string | Unique identifier |
| s3_input_key | string | S3 key for uploaded point cloud file |
| filename | string | Original filename (for display) |
| file_size_bytes | int | File size |
| config | JobConfig | Detection parameters |
| status | enum | `queued`, `processing`, `complete`, `failed` |
| created_at | datetime | Submission timestamp |
| started_at | datetime | nullable | When worker picked up the job |
| completed_at | datetime | nullable | When processing finished |
| detection_count | int | nullable | Number of detections found |
| tile_count | int | nullable | Number of tiles processed |
| processing_time_s | float | nullable | Total processing time |
| error_message | string | nullable | Error details if failed |
| s3_output_key | string | nullable | S3 key for detection results JSON |
| s3_segmentation_key | string | nullable | S3 key for segmentation JSON |

---

## Entity: JobConfig

Parameters for a detection run.

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| model_mode | enum | `indoor` | `indoor`, `outdoor`, `kitti`, `custom` |
| confidence_threshold | float | 0.5 | Minimum confidence to include |
| use_color | bool | true | Include RGB channels |
| use_intensity | bool | false | Include intensity channel |
| use_normals | bool | false | Compute surface normals |
| coord_frame | string | `z-up` | Coordinate frame (z-up, livox, leica, etc.) |
| downsample_voxel_m | float | 0.02 | Voxel size for downsampling |
| max_points | int | nullable | null | Max points to load |
| custom_weights_version | string | nullable | null | Model registry version if custom |
| raw_mode | bool | false | Skip confidence filtering |

---

## Entity: UploadSession

Tracks a multipart upload in progress.

| Attribute | Type | Description |
|-----------|------|-------------|
| upload_id | string | S3 multipart upload ID |
| filename | string | Original filename |
| file_size_bytes | int | Total expected size |
| content_type | string | MIME type |
| s3_key | string | Target S3 key |
| part_size_bytes | int | Size of each part (100MB default) |
| total_parts | int | Expected number of parts |
| created_at | datetime | When upload was initiated |
| status | enum | `in_progress`, `completed`, `aborted` |

---

## Entity: TileIndex

Metadata for LOD point cloud tiles (for the 3D viewer).

| Attribute | Type | Description |
|-----------|------|-------------|
| job_id | string | Associated job |
| total_points | int | Total points in scene |
| bounds_min | float[3] | Scene min XYZ |
| bounds_max | float[3] | Scene max XYZ |
| levels | int | Number of LOD levels |
| tiles | list[TileMetadata] | Per-tile metadata |

---

## Entity: TileMetadata

Single tile in the LOD octree.

| Attribute | Type | Description |
|-----------|------|-------------|
| tile_id | string | Unique tile identifier |
| level | int | LOD level (0 = coarsest overview) |
| bounds_min | float[3] | Tile spatial extent min |
| bounds_max | float[3] | Tile spatial extent max |
| point_count | int | Points in this tile |
| s3_key | string | S3 key for tile data file |

---

## Entity: SQSJobMessage

The message published to SQS when a job is created.

| Attribute | Type | Description |
|-----------|------|-------------|
| job_id | string | Job identifier |
| s3_input_key | string | S3 key for input file |
| config | JobConfig | Detection parameters |
| created_at | string | ISO timestamp |

---

## Entity: ModelVersion (shared with Unit 4)

| Attribute | Type | Description |
|-----------|------|-------------|
| version | string | Semantic version (e.g., `v1.0.0`) |
| base_model | string | Base model config name |
| s3_weights_key | string | S3 key for .pth file |
| eval_metrics | dict | mAP@0.25, mAP@0.50 |
| created_at | datetime | Registration time |
