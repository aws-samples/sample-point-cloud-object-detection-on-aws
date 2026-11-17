"""Training loop for fine-tuning detection models."""

import logging
import time
from pathlib import Path

from pointcloud_detect.training.config import TrainingConfig
from pointcloud_detect.training.data_loader import (
    get_class_names,
    load_annotations,
    split_train_val,
)

logger = logging.getLogger(__name__)


def run_training(config: TrainingConfig) -> dict:
    """Execute fine-tuning training loop.

    Returns training metadata including final metrics and checkpoint path.
    """
    logger.info(
        "Starting fine-tuning: base_model=%s, epochs=%d, lr=%s",
        config.base_model,
        config.epochs,
        config.learning_rate,
    )

    # Load and validate data
    annotations = load_annotations(config.data_path)
    train_data, val_data = split_train_val(annotations, config.val_split)
    class_names = config.custom_classes or get_class_names(annotations)

    logger.info("Classes: %s", class_names)
    logger.info(
        "Training scenes: %d, Validation scenes: %d", len(train_data), len(val_data)
    )

    output_path = Path(config.output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    # Check if mmdet3d is available for real training
    try:
        import mmdet3d  # noqa: F401  # pyright: ignore[reportMissingImports]

        from pointcloud_detect.training.mmdet3d_trainer import run_real_training

        return run_real_training(config)
    except ImportError:
        logger.warning("mmdet3d not installed — running stub training loop")
        return _train_stub(config, train_data, val_data, class_names, output_path)


def _train_stub(
    config: TrainingConfig,
    train_data: list[dict],
    val_data: list[dict],
    class_names: list[str],
    output_path: Path,
) -> dict:
    """Stub training loop for testing without mmdet3d."""
    import json

    start_time = time.perf_counter()

    for epoch in range(1, config.epochs + 1):
        # Simulate training
        loss = 1.0 / (epoch + 1)
        logger.info("Epoch %d/%d — loss: %.4f", epoch, config.epochs, loss)

        # Save checkpoint at interval
        if epoch % config.checkpoint_interval == 0:
            ckpt_path = output_path / f"checkpoint_epoch_{epoch}.json"
            ckpt_path.write_text(
                json.dumps(
                    {
                        "epoch": epoch,
                        "loss": loss,
                        "base_model": config.base_model,
                        "classes": class_names,
                    }
                )
            )
            logger.info("Saved checkpoint: %s", ckpt_path)

    elapsed = time.perf_counter() - start_time

    # Final checkpoint
    final_path = output_path / "model_final.json"
    final_path.write_text(
        json.dumps(
            {
                "epoch": config.epochs,
                "base_model": config.base_model,
                "classes": class_names,
                "training_time_s": elapsed,
                "train_scenes": len(train_data),
                "val_scenes": len(val_data),
            }
        )
    )

    return {
        "checkpoint_path": str(final_path),
        "epochs_completed": config.epochs,
        "training_time_s": round(elapsed, 2),
        "class_names": class_names,
        "train_scenes": len(train_data),
        "val_scenes": len(val_data),
    }


def _train_mmdet3d(
    config: TrainingConfig,
    train_data: list[dict],
    val_data: list[dict],
    class_names: list[str],
    output_path: Path,
) -> dict:
    """Real training using MMDetection3D (transfer learning)."""
    # This would use mmdet3d's training APIs:
    # 1. Build config from base model + custom classes
    # 2. Load pre-trained weights
    # 3. Modify head for custom class count
    # 4. Run training loop with configured optimizer
    # 5. Save best checkpoint based on val mAP

    # Placeholder — actual implementation depends on mmdet3d version
    logger.info("MMDetection3D training would execute here")
    logger.info("Base model: %s", config.base_model)
    logger.info("Custom classes: %s", class_names)
    logger.info(
        "Epochs: %d, LR: %s, Batch: %d",
        config.epochs,
        config.learning_rate,
        config.batch_size,
    )

    # For now, fall back to stub
    return _train_stub(config, train_data, val_data, class_names, output_path)


if __name__ == "__main__":
    """CLI entry point for training."""
    import argparse

    import yaml

    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="Fine-tune point cloud detection model"
    )
    parser.add_argument("--config", "-c", required=True, help="Training config YAML")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg_data = yaml.safe_load(f)
    cfg = TrainingConfig(**cfg_data)

    result = run_training(cfg)
    logger.info("Training complete: %s", result)
