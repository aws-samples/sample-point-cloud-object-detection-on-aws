"""Inference backends for detection and segmentation."""

from pointcloud_detect.backends.mmdet3d import (
    MMDet3DDetectionBackend,
    MMDet3DSegmentationBackend,
)

__all__ = [
    "MMDet3DDetectionBackend",
    "MMDet3DSegmentationBackend",
]
