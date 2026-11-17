"""Generate LOD octree tiles for progressive 3D viewer loading.

Tile binary format (interleaved Float32):
  - "xyz"     → [x, y, z] per point (stride 3) — default, backward compatible
  - "xyzrgb"  → [x, y, z, r, g, b] per point (stride 6), RGB in 0..1
  - "xyzrgbi" → [x, y, z, r, g, b, i] per point (stride 7), intensity in 0..1
"""

import logging
from typing import Literal

import numpy as np

from pointcloud_detect.models import PointCloud
from pointcloud_detect.worker.s3_io import upload_binary_to_s3, upload_json_to_s3

logger = logging.getLogger(__name__)

MAX_OVERVIEW_POINTS = 1_000_000
MAX_DETAIL_TILE_POINTS = 500_000

PointFormat = Literal["xyz", "xyzrgb", "xyzrgbi"]

# Stride (number of float32 values per point) for each format
FORMAT_STRIDE: dict[PointFormat, int] = {"xyz": 3, "xyzrgb": 6, "xyzrgbi": 7}


def _determine_point_format(cloud: PointCloud) -> PointFormat:
    """Determine the richest point format available from the cloud's channels."""
    if cloud.rgb is not None and cloud.intensity is not None:
        return "xyzrgbi"
    if cloud.rgb is not None:
        return "xyzrgb"
    return "xyz"


def _pack_points(
    xyz: np.ndarray,
    rgb: np.ndarray | None,
    intensity: np.ndarray | None,
    point_format: PointFormat,
    indices: np.ndarray | None = None,
) -> bytes:
    """Pack point data into interleaved Float32 binary.

    Args:
        xyz: (N, 3) float64 point positions
        rgb: (N, 3) uint8 colors or None
        intensity: (N,) float32 intensity or None
        point_format: target encoding format
        indices: optional subset indices to select from arrays

    Returns:
        Packed bytes (N * stride * 4 bytes)
    """
    if indices is not None:
        xyz = xyz[indices]
        if rgb is not None:
            rgb = rgb[indices]
        if intensity is not None:
            intensity = intensity[indices]

    n = len(xyz)
    stride = FORMAT_STRIDE[point_format]
    buf = np.zeros((n, stride), dtype=np.float32)

    # XYZ always present
    buf[:, 0:3] = xyz.astype(np.float32)

    # RGB (normalized to 0..1)
    if point_format in ("xyzrgb", "xyzrgbi") and rgb is not None:
        buf[:, 3:6] = rgb.astype(np.float32) / 255.0

    # Intensity (normalized to 0..1)
    if point_format == "xyzrgbi" and intensity is not None:
        int_vals = intensity.astype(np.float32)
        int_max = int_vals.max()
        if int_max > 0:
            int_vals = int_vals / int_max
        buf[:, 6] = int_vals

    return buf.tobytes()


def generate_lod_tiles(
    cloud: PointCloud,
    job_id: str,
    s3_client,
    bucket: str,
    segmentation_labels: np.ndarray | None = None,
    segmentation_class_names: list[str] | None = None,
) -> int:
    """Generate multi-resolution LOD tiles and upload to S3.

    Automatically selects the richest point format based on available
    channels (xyz, xyzrgb, or xyzrgbi). The format is recorded in the
    tile index so the frontend knows how to parse the binary.

    If segmentation_labels is provided, a parallel Uint16 label file is
    emitted per tile ({tile_id}_seg.bin), aligned index-for-index with
    the tile's points.

    Returns the total number of tiles generated.
    """
    n_points = cloud.num_points
    point_format = _determine_point_format(cloud)
    # Bound to a narrowed local rather than kept as a bare bool. The two are
    # equivalent at runtime, but `if has_segmentation:` loses the None-ness of
    # segmentation_labels for a reader and for the type checker, leaving the
    # labels-are-present invariant implicit at every use site below.
    seg_labels: np.ndarray | None = (
        segmentation_labels
        if segmentation_labels is not None and len(segmentation_labels) == n_points
        else None
    )
    has_segmentation = seg_labels is not None
    logger.info(
        "Generating LOD tiles for %d points (format: %s, segmentation: %s)",
        n_points,
        point_format,
        has_segmentation,
    )

    tiles_metadata = []

    # Level 0: Overview tile (subsampled to MAX_OVERVIEW_POINTS)
    if n_points <= MAX_OVERVIEW_POINTS:
        overview_indices = None
        overview_count = n_points
    else:
        rng = np.random.default_rng(42)
        overview_indices = rng.choice(n_points, MAX_OVERVIEW_POINTS, replace=False)
        overview_count = MAX_OVERVIEW_POINTS

    overview_bytes = _pack_points(
        cloud.xyz, cloud.rgb, cloud.intensity, point_format, overview_indices
    )
    overview_key = f"tiles/{job_id}/overview.bin"
    upload_binary_to_s3(s3_client, bucket, overview_key, overview_bytes)

    # Segmentation for overview tile
    if seg_labels is not None:
        seg_indices = (
            overview_indices if overview_indices is not None else np.arange(n_points)
        )
        overview_seg = seg_labels[seg_indices].astype(np.uint16)
        overview_seg_key = f"tiles/{job_id}/overview_seg.bin"
        upload_binary_to_s3(s3_client, bucket, overview_seg_key, overview_seg.tobytes())

    tiles_metadata.append(
        {
            "tile_id": "overview",
            "level": 0,
            "bounds_min": cloud.bounds_min.tolist(),
            "bounds_max": cloud.bounds_max.tolist(),
            "point_count": overview_count,
            "s3_key": overview_key,
        }
    )

    # Level 1+: Detail tiles via octree subdivision (only if large)
    if n_points > MAX_DETAIL_TILE_POINTS:
        detail_tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=MAX_DETAIL_TILE_POINTS,
        )

        for i, tile in enumerate(detail_tiles):
            tile_id = f"detail_{i:04d}"
            tile_key = f"tiles/{job_id}/{tile_id}.bin"
            tile_bytes = _pack_points(
                tile["xyz"], tile["rgb"], tile["intensity"], point_format
            )
            upload_binary_to_s3(s3_client, bucket, tile_key, tile_bytes)

            # Per-tile segmentation labels
            if seg_labels is not None:
                tile_seg = seg_labels[tile["indices"]].astype(np.uint16)
                seg_key = f"tiles/{job_id}/{tile_id}_seg.bin"
                upload_binary_to_s3(s3_client, bucket, seg_key, tile_seg.tobytes())

            tiles_metadata.append(
                {
                    "tile_id": tile_id,
                    "level": 1,
                    "bounds_min": tile["bounds_min"].tolist(),
                    "bounds_max": tile["bounds_max"].tolist(),
                    "point_count": len(tile["xyz"]),
                    "s3_key": tile_key,
                }
            )

    # Build segmentation labels metadata for the index
    seg_labels_meta = None
    if has_segmentation and segmentation_class_names:
        seg_labels_meta = [
            {"id": i, "name": name} for i, name in enumerate(segmentation_class_names)
        ]

    # Upload tile index (includes point_format + segmentation info for frontend)
    index = {
        "job_id": job_id,
        "total_points": n_points,
        "bounds_min": cloud.bounds_min.tolist(),
        "bounds_max": cloud.bounds_max.tolist(),
        "point_format": point_format,
        "has_segmentation": has_segmentation,
        "segmentation_labels": seg_labels_meta,
        "levels": 2 if n_points > MAX_DETAIL_TILE_POINTS else 1,
        "tiles": tiles_metadata,
    }
    index_key = f"tiles/{job_id}/index.json"
    upload_json_to_s3(s3_client, bucket, index_key, index)

    logger.info("Generated %d tiles for job %s", len(tiles_metadata), job_id)
    return len(tiles_metadata)


def _subdivide_octree(
    cloud: PointCloud,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    max_points: int,
) -> list[dict]:
    """Recursively subdivide point cloud into spatial tiles, preserving all channels."""
    tiles: list[dict] = []
    indices = np.arange(cloud.num_points)
    _recursive_split(cloud, indices, bounds_min, bounds_max, max_points, tiles)
    return tiles


def _recursive_split(
    cloud: PointCloud,
    indices: np.ndarray,
    bounds_min: np.ndarray,
    bounds_max: np.ndarray,
    max_points: int,
    tiles: list[dict],
) -> None:
    """Binary split along longest axis until tiles are within budget."""
    if len(indices) <= max_points:
        tiles.append(
            {
                "xyz": cloud.xyz[indices],
                "rgb": cloud.rgb[indices] if cloud.rgb is not None else None,
                "intensity": cloud.intensity[indices]
                if cloud.intensity is not None
                else None,
                "indices": indices,
                "bounds_min": bounds_min,
                "bounds_max": bounds_max,
            }
        )
        return

    extent = bounds_max - bounds_min
    split_axis = int(np.argmax(extent))
    mid = (bounds_min[split_axis] + bounds_max[split_axis]) / 2.0

    positions = cloud.xyz[indices, split_axis]
    left_mask = positions <= mid
    right_mask = ~left_mask

    left_indices = indices[left_mask]
    right_indices = indices[right_mask]

    left_max = bounds_max.copy()
    left_max[split_axis] = mid
    right_min = bounds_min.copy()
    right_min[split_axis] = mid

    if len(left_indices) > 0:
        _recursive_split(cloud, left_indices, bounds_min, left_max, max_points, tiles)
    if len(right_indices) > 0:
        _recursive_split(cloud, right_indices, right_min, bounds_max, max_points, tiles)
