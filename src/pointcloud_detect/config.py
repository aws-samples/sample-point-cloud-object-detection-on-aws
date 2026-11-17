"""Pipeline configuration models."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PreprocessingConfig(BaseModel):
    """Point cloud preprocessing parameters."""

    downsample: bool = True
    voxel_size_m: float = 0.02
    use_color: bool = True
    use_intensity: bool = False
    use_normals: bool = False
    point_cloud_range: list[float] | None = None
    coord_frame: str = "z-up"
    max_points: int | None = None


class DetectionModelConfig(BaseModel):
    """Detection model configuration."""

    backend: str = "mmdetection3d"
    config: str = "votenet_scannet"
    weights: str = "pretrained"


class SegmentationModelConfig(BaseModel):
    """Segmentation model configuration.

    Off by default: the only segmentation weights are the pretrained PointNet++
    checkpoints, which are non-commercially trained and download only with
    ALLOW_NONCOMMERCIAL_WEIGHTS=1 (backends/checkpoints.py). The worker turns it on
    when that opt-in is set.
    """

    enabled: bool = False
    backend: str = "mmdetection3d"
    config: str = "pointnet2_s3dis"
    weights: str = "pretrained"


class ModelConfig(BaseModel):
    """Model selection configuration."""

    mode: Literal["indoor", "outdoor", "kitti", "custom"] = "indoor"
    detection: DetectionModelConfig = Field(default_factory=DetectionModelConfig)
    segmentation: SegmentationModelConfig = Field(
        default_factory=SegmentationModelConfig
    )


class TilingConfig(BaseModel):
    """Adaptive tiling parameters."""

    # Constraints matter here beyond hygiene: min_tile_size_m = 0 makes every
    # axis (including zero-extent ones) qualify for splitting, and a resolution
    # of 0 divides by zero when sizing the density grid.
    max_points_per_tile: int = Field(default=100_000, gt=0)
    overlap_margin_m: float = Field(default=2.0, ge=0.0)
    min_tile_size_m: float = Field(default=5.0, gt=0.0)
    density_grid_resolution_m: float = Field(default=10.0, gt=0.0)
    memory_budget_gb: float = Field(default=16.0, gt=0.0)
    # Optional ceiling on emitted tiles. Each tile is one inference call and tile
    # counts move sharply with scene shape (81 -> 596 measured on a skewed
    # scene), so this exists as a guard against runaway inference cost.
    #
    # Defaults to None (no ceiling) deliberately. Any fixed default low enough to
    # catch a pathological scene would also reject the NFR-1.2 target: 50 GB
    # needs >= ~41,000 tiles and 500 GB >= ~416,000 at the default
    # max_points_per_tile. Picking a real default requires the on-hardware
    # benchmark, so operators opt in per workload until then.
    max_tiles: int | None = Field(default=None, gt=0)


class InferenceConfig(BaseModel):
    """Inference device and precision settings."""

    device: str = "auto"
    batch_size: int = 1
    precision: Literal["fp32", "fp16"] = "fp32"


class PostprocessingConfig(BaseModel):
    """Post-processing parameters."""

    # Reject unknown keys instead of ignoring them. pydantic's default would
    # silently drop a misspelt or renamed key, and a filter the operator believes
    # is active would simply not run — detections they expected to be excluded
    # would appear in the output with no warning. This is what makes the
    # excluded_labels rename safe: a config still using the old key fails
    # immediately and names it, rather than quietly filtering nothing.
    model_config = ConfigDict(extra="forbid")

    nms_iou_threshold: float = 0.3
    confidence_threshold: float = 0.5
    raw_mode: bool = False
    min_size_m: float | None = None
    max_size_m: float | None = None
    excluded_labels: list[str] = []


class OutputConfig(BaseModel):
    """Output configuration."""

    directory: Path = Path("./output")
    naming: str = "{input_stem}_detections"
    include_segmentation: bool = True
    include_aabb: bool = True
    normalize_coordinates: bool = False
    format: Literal["json"] = "json"


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"
    progress: bool = True


class PipelineConfig(BaseModel):
    """Top-level pipeline configuration."""

    name: str = "pointcloud-detection"
    version: str = "1.0"
    preprocessing: PreprocessingConfig = Field(default_factory=PreprocessingConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    tiling: TilingConfig = Field(default_factory=TilingConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    postprocessing: PostprocessingConfig = Field(default_factory=PostprocessingConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
