"""Data models for pipeline inputs and outputs."""

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray


@dataclass
class PointCloud:
    """Unified point cloud representation after format normalisation."""

    xyz: NDArray[np.float64]
    rgb: NDArray[np.uint8] | None = None
    intensity: NDArray[np.float32] | None = None
    normals: NDArray[np.float32] | None = None
    source_file: str = ""

    @property
    def num_points(self) -> int:
        return self.xyz.shape[0]

    @property
    def bounds_min(self) -> NDArray[np.float64]:
        return self.xyz.min(axis=0)

    @property
    def bounds_max(self) -> NDArray[np.float64]:
        return self.xyz.max(axis=0)


@dataclass
class Tile:
    """A spatial tile of the scene for inference."""

    xyz: NDArray[np.float64]
    rgb: NDArray[np.uint8] | None = None
    intensity: NDArray[np.float32] | None = None
    normals: NDArray[np.float32] | None = None
    origin: NDArray[np.float64] = field(default_factory=lambda: np.zeros(3))
    extent_min: NDArray[np.float64] = field(default_factory=lambda: np.zeros(3))
    extent_max: NDArray[np.float64] = field(default_factory=lambda: np.zeros(3))
    point_indices: NDArray[np.int64] | None = None


@dataclass
class Detection:
    """A single detected object."""

    label: str
    confidence: float
    center: NDArray[np.float64]
    size: NDArray[np.float64]
    heading: float
    aabb_min: NDArray[np.float64] | None = None
    aabb_max: NDArray[np.float64] | None = None


@dataclass
class SegmentationResult:
    """Per-point segmentation labels for the scene."""

    labels: NDArray[np.int32]
    class_names: list[str]
    xyz: NDArray[np.float64] | None = None
    confidences: NDArray[np.float32] | None = None


@dataclass
class PipelineResult:
    """Complete pipeline output for a single scene."""

    source_file: str
    detections: list[Detection]
    segmentation: SegmentationResult | None = None
    scene_bounds_min: NDArray[np.float64] = field(default_factory=lambda: np.zeros(3))
    scene_bounds_max: NDArray[np.float64] = field(default_factory=lambda: np.zeros(3))
    tile_count: int = 0
    processing_time_seconds: float = 0.0
    model_name: str = ""
    model_version: str = ""
    device: str = ""
