"""Training configuration models."""

from pydantic import BaseModel, Field


class AugmentationConfig(BaseModel):
    """Data augmentation settings."""

    random_flip: bool = True
    random_rotate: bool = True
    random_scale: tuple[float, float] = (0.9, 1.1)
    random_jitter: float = 0.02


class TrainingConfig(BaseModel):
    """Fine-tuning configuration."""

    # Architecture to train (a DETECTION_CONFIGS key). FCAF3D needs MinkowskiEngine,
    # which only the GPU image has; votenet_scannet trains anywhere mmdet3d runs.
    base_model: str = "fcaf3d_scannet"
    # Where training starts from:
    #   "scratch"     random initialization (default). The result inherits no
    #                 pretrained checkpoint, so its terms are those of your data.
    #   "pretrained"  the architecture's OpenMMLab checkpoint, which is
    #                 non-commercially trained and needs ALLOW_NONCOMMERCIAL_WEIGHTS=1.
    #   <path>        a .pth of your own.
    base_weights: str = "scratch"
    data_path: str = ""  # Path to annotation directory
    val_split: float = Field(default=0.2, ge=0.0, le=0.5)
    output_path: str = "./training_output"
    epochs: int = Field(default=50, ge=1)
    learning_rate: float = Field(default=0.001, gt=0)
    batch_size: int = Field(default=8, ge=1)
    num_workers: int = 4
    checkpoint_interval: int = 5  # Save every N epochs
    early_stopping_patience: int = 10
    augmentation: AugmentationConfig = Field(default_factory=AugmentationConfig)
    custom_classes: list[str] | None = None  # Override class list
    run_on_eks: bool = False  # Submit as K8s Job instead of running locally
