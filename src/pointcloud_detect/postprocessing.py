"""Post-processing: NMS, AABB computation, coordinate normalisation."""

import numpy as np

from pointcloud_detect.config import PostprocessingConfig
from pointcloud_detect.models import Detection


def compute_aabb(detection: Detection) -> Detection:
    """Compute axis-aligned bounding box from oriented bounding box."""
    cx, cy, cz = detection.center
    sx, sy, sz = detection.size
    heading = detection.heading

    cos_h = np.cos(heading)
    sin_h = np.sin(heading)

    half_x = sx / 2.0
    half_y = sy / 2.0
    half_z = sz / 2.0

    corners_x = np.array(
        [
            cos_h * half_x - sin_h * half_y,
            cos_h * half_x + sin_h * half_y,
            -cos_h * half_x - sin_h * half_y,
            -cos_h * half_x + sin_h * half_y,
        ]
    )
    corners_y = np.array(
        [
            sin_h * half_x + cos_h * half_y,
            sin_h * half_x - cos_h * half_y,
            -sin_h * half_x + cos_h * half_y,
            -sin_h * half_x - cos_h * half_y,
        ]
    )

    dx = float(np.max(np.abs(corners_x)))
    dy = float(np.max(np.abs(corners_y)))

    detection.aabb_min = np.array([cx - dx, cy - dy, cz - half_z])
    detection.aabb_max = np.array([cx + dx, cy + dy, cz + half_z])
    return detection


def iou_3d_aabb(det_a: Detection, det_b: Detection) -> float:
    """Compute IoU between two detections using their AABBs."""
    # aabb_min and aabb_max are independent Optional fields on Detection and nothing
    # enforces that they are set together, so both have to be checked. Guarding on
    # aabb_min alone left `inter_max - inter_min` and the volume terms below free to
    # subtract None and raise TypeError on any Detection carrying one bound but not
    # the other -- reachable via a directly-constructed or deserialised Detection,
    # since only compute_aabb() happens to set the pair together.
    if (
        det_a.aabb_min is None
        or det_a.aabb_max is None
        or det_b.aabb_min is None
        or det_b.aabb_max is None
    ):
        return 0.0

    inter_min = np.maximum(det_a.aabb_min, det_b.aabb_min)
    inter_max = np.minimum(det_a.aabb_max, det_b.aabb_max)
    inter_size = np.maximum(inter_max - inter_min, 0.0)
    inter_vol = float(np.prod(inter_size))

    vol_a = float(np.prod(det_a.aabb_max - det_a.aabb_min))
    vol_b = float(np.prod(det_b.aabb_max - det_b.aabb_min))
    union_vol = vol_a + vol_b - inter_vol

    if union_vol <= 0:
        return 0.0
    return inter_vol / union_vol


def nms_3d(detections: list[Detection], iou_threshold: float) -> list[Detection]:
    """Apply 3D Non-Maximum Suppression across detections.

    Uses both IoU overlap and center distance to suppress duplicates.
    Same-class detections are suppressed if IoU > threshold OR if their
    centers are within half the max dimension of the higher-confidence box.
    """
    if not detections:
        return []

    sorted_dets = sorted(detections, key=lambda d: d.confidence, reverse=True)
    keep: list[Detection] = []

    for det in sorted_dets:
        suppressed = False
        for kept in keep:
            if det.label != kept.label:
                continue
            # IoU-based suppression
            if iou_3d_aabb(det, kept) > iou_threshold:
                suppressed = True
                break
            # Distance-based suppression: suppress if centers are close
            dist = float(np.linalg.norm(det.center - kept.center))
            merge_radius = float(np.max(kept.size))
            if dist < merge_radius:
                suppressed = True
                break
        if not suppressed:
            keep.append(det)

    return keep


def filter_by_confidence(
    detections: list[Detection], threshold: float
) -> list[Detection]:
    """Remove detections below confidence threshold."""
    return [d for d in detections if d.confidence >= threshold]


def filter_by_size(
    detections: list[Detection],
    min_size_m: float | None = None,
    max_size_m: float | None = None,
) -> list[Detection]:
    """Filter detections by bounding box size (max dimension)."""
    result: list[Detection] = []
    for det in detections:
        max_dim = float(np.max(det.size))
        if min_size_m is not None and max_dim < min_size_m:
            continue
        if max_size_m is not None and max_dim > max_size_m:
            continue
        result.append(det)
    return result


def filter_by_excluded_labels(
    detections: list[Detection], excluded_labels: list[str]
) -> list[Detection]:
    """Remove detections whose label appears in ``excluded_labels``.

    Matching is case-insensitive, so "Curtain" excludes "curtain".
    """
    if not excluded_labels:
        return detections
    excluded_lower = {label.lower() for label in excluded_labels}
    return [d for d in detections if d.label.lower() not in excluded_lower]


def normalize_detections(
    detections: list[Detection],
    scene_min: np.ndarray,
    scene_max: np.ndarray,
) -> list[Detection]:
    """Normalise detection coordinates to [0, 1] based on scene extents."""
    extent = scene_max - scene_min
    extent = np.where(extent == 0, 1.0, extent)

    for det in detections:
        det.center = (det.center - scene_min) / extent
        det.size = det.size / extent
        if det.aabb_min is not None:
            det.aabb_min = (det.aabb_min - scene_min) / extent
        if det.aabb_max is not None:
            det.aabb_max = (det.aabb_max - scene_min) / extent

    return detections


def postprocess(
    detections: list[Detection],
    config: PostprocessingConfig,
    include_aabb: bool = True,
    normalize: bool = False,
    scene_min: np.ndarray | None = None,
    scene_max: np.ndarray | None = None,
) -> list[Detection]:
    """Run full post-processing pipeline on merged detections."""
    if include_aabb:
        detections = [compute_aabb(d) for d in detections]

    detections = nms_3d(detections, config.nms_iou_threshold)

    if not config.raw_mode:
        detections = filter_by_confidence(detections, config.confidence_threshold)

    detections = filter_by_size(detections, config.min_size_m, config.max_size_m)
    detections = filter_by_excluded_labels(detections, config.excluded_labels)

    if normalize and scene_min is not None and scene_max is not None:
        detections = normalize_detections(detections, scene_min, scene_max)

    return detections
