"""Detection and segmentation inference backends.

This module defines the interface and provides stub implementations.
Real backends (MMDetection3D, OpenPCDet) will be implemented as
subclasses of DetectionBackend and SegmentationBackend.
"""

import logging
from abc import ABC, abstractmethod

import numpy as np

from pointcloud_detect.config import ModelConfig
from pointcloud_detect.models import Detection, Tile


class DetectionBackend(ABC):
    """Abstract base for 3D object detection backends."""

    @abstractmethod
    def load_model(self, config: ModelConfig) -> None:
        """Load model weights and prepare for inference."""

    @abstractmethod
    def detect(self, tile: Tile) -> list[Detection]:
        """Run detection on a single tile, returning detections in tile-local coords."""


class SegmentationBackend(ABC):
    """Abstract base for per-point segmentation backends."""

    @abstractmethod
    def load_model(self, config: ModelConfig) -> None:
        """Load model weights and prepare for inference."""

    @abstractmethod
    def segment(self, tile: Tile) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Run segmentation on a tile.

        Returns per-point label indices, the class name list, and per-point
        confidences -- three values, matching what pipeline.py unpacks.
        """


class StubDetectionBackend(DetectionBackend):
    """Stub detection backend for pipeline testing.

    Generates synthetic detections based on point cloud statistics.
    Replace with MMDetection3D or OpenPCDet implementation.
    """

    def __init__(self) -> None:
        self._class_names: list[str] = []

    def load_model(self, config: ModelConfig) -> None:
        if config.mode == "indoor":
            self._class_names = [
                "cabinet",
                "bed",
                "chair",
                "sofa",
                "table",
                "door",
                "window",
                "bookshelf",
                "picture",
                "counter",
                "desk",
                "curtain",
                "refrigerator",
                "shower curtain",
                "toilet",
                "sink",
                "bathtub",
                "other furniture",
            ]
        elif config.mode == "outdoor":
            self._class_names = [
                "car",
                "truck",
                "bus",
                "trailer",
                "construction vehicle",
                "pedestrian",
                "motorcycle",
                "bicycle",
                "traffic cone",
                "barrier",
            ]
        else:
            self._class_names = ["object"]

    def detect(self, tile: Tile) -> list[Detection]:
        """Generate a single synthetic detection at the tile center."""
        if tile.xyz.shape[0] < 10:
            return []

        center = tile.xyz.mean(axis=0)
        extent = tile.xyz.max(axis=0) - tile.xyz.min(axis=0)
        size = extent * 0.1

        rng = np.random.default_rng(seed=int(center[0] * 1000) % 2**31)
        label = self._class_names[rng.integers(len(self._class_names))]

        return [
            Detection(
                label=label,
                confidence=float(rng.uniform(0.5, 0.99)),
                center=center,
                size=size,
                heading=float(rng.uniform(0, 2 * np.pi)),
            )
        ]


class StubSegmentationBackend(SegmentationBackend):
    """Stub segmentation backend for pipeline testing.

    Assigns random labels to points.
    Replace with MMDetection3D or Pointcept implementation.
    """

    def __init__(self) -> None:
        self._class_names: list[str] = []

    def load_model(self, config: ModelConfig) -> None:
        if config.mode == "indoor":
            self._class_names = [
                "wall",
                "floor",
                "ceiling",
                "door",
                "window",
                "chair",
                "table",
                "sofa",
                "bed",
                "desk",
                "cabinet",
                "bookshelf",
                "curtain",
                "counter",
                "refrigerator",
                "shower curtain",
                "toilet",
                "sink",
                "bathtub",
                "other furniture",
            ]
        elif config.mode == "outdoor":
            self._class_names = [
                "road",
                "sidewalk",
                "building",
                "vegetation",
                "terrain",
                "vehicle",
                "pedestrian",
                "pole",
                "traffic sign",
                "other",
            ]
        else:
            self._class_names = ["unknown"]

    def segment(self, tile: Tile) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Assign random labels to all points in the tile."""
        rng = np.random.default_rng(seed=42)
        labels = rng.integers(0, len(self._class_names), size=tile.xyz.shape[0]).astype(
            np.int32
        )
        confidences = np.ones(len(labels), dtype=np.float32) * 0.5
        return labels, self._class_names, confidences


def create_detection_backend(
    config: ModelConfig, device: str = "auto"
) -> DetectionBackend:
    """Factory for detection backends.

    Uses MMDetection3D if available. On CPU, monkey-patches mmcv ops
    (furthest_point_sample, ball_query, group_points) with pure-PyTorch
    fallbacks so all models work — including VoteNet and FCAF3D.
    """
    try:
        import mmdet3d  # noqa: F401  # pyright: ignore[reportMissingImports]
        import torch  # pyright: ignore[reportMissingImports]

        # Apply CPU fallback patches for mmcv CUDA ops if needed
        if not torch.cuda.is_available():
            import pointcloud_detect.backends.mmcv_cpu_ops  # noqa: F401

        from pointcloud_detect.backends.mmdet3d import MMDet3DDetectionBackend

        backend: DetectionBackend = MMDet3DDetectionBackend(device=device)
        backend.load_model(config)
        return backend

    except ImportError:
        logging.getLogger(__name__).warning(
            "mmdet3d not installed — using stub detection backend."
        )

    backend = StubDetectionBackend()
    backend.load_model(config)
    return backend


def create_segmentation_backend(
    config: ModelConfig, device: str = "auto"
) -> SegmentationBackend:
    """Factory for segmentation backends.

    Uses MMDetection3D if available. CPU fallback patches are applied
    for mmcv ops so PointNet2 segmentation works on CPU too.
    """
    try:
        import mmdet3d  # noqa: F401  # pyright: ignore[reportMissingImports]
        import torch  # pyright: ignore[reportMissingImports]

        if not torch.cuda.is_available():
            import pointcloud_detect.backends.mmcv_cpu_ops  # noqa: F401

        from pointcloud_detect.backends.mmdet3d import MMDet3DSegmentationBackend

        backend: SegmentationBackend = MMDet3DSegmentationBackend(device=device)
        backend.load_model(config)
        return backend

    except ImportError:
        logging.getLogger(__name__).warning(
            "mmdet3d not installed — using stub segmentation backend."
        )

    backend = StubSegmentationBackend()
    backend.load_model(config)
    return backend
