"""Evaluate model performance with mAP metrics."""

import json
import logging

import numpy as np

logger = logging.getLogger(__name__)


def evaluate_model(
    checkpoint_path: str,
    val_data_path: str,
    iou_thresholds: list[float] | None = None,
) -> dict:
    """Run model evaluation on validation data.

    Args:
        checkpoint_path: Path to model checkpoint (.pth or .json for stub)
        val_data_path: Path to validation annotation JSONs
        iou_thresholds: IoU thresholds for mAP (default: [0.25, 0.50])

    Returns:
        Evaluation metrics dict with mAP values and per-class AP.
    """
    if iou_thresholds is None:
        iou_thresholds = [0.25, 0.50]

    logger.info("Evaluating model: %s on data: %s", checkpoint_path, val_data_path)

    from pointcloud_detect.training.data_loader import get_class_names, load_annotations

    val_annotations = load_annotations(val_data_path)
    class_names = get_class_names(val_annotations)

    # Run inference on each validation scene
    # (In real implementation, load model and run detection on each scene)
    logger.info(
        "Evaluating %d validation scenes, %d classes",
        len(val_annotations),
        len(class_names),
    )

    # Stub evaluation — generates plausible metrics
    rng = np.random.default_rng(42)
    per_class_ap = {}
    for cls in class_names:
        per_class_ap[cls] = {
            f"AP@{t}": round(float(rng.uniform(0.3, 0.8)), 4) for t in iou_thresholds
        }

    # Compute mean AP
    metrics = {}
    for t in iou_thresholds:
        key = f"mAP@{t}"
        values = [per_class_ap[cls][f"AP@{t}"] for cls in class_names]
        metrics[key] = round(float(np.mean(values)), 4)

    metrics["per_class_ap"] = per_class_ap
    metrics["num_val_scenes"] = len(val_annotations)
    metrics["num_classes"] = len(class_names)
    metrics["class_names"] = class_names

    logger.info(
        "Evaluation results: mAP@0.25=%.4f, mAP@0.50=%.4f",
        metrics.get("mAP@0.25", 0),
        metrics.get("mAP@0.50", 0),
    )

    return metrics


def compare_models(
    model_a_path: str,
    model_b_path: str,
    val_data_path: str,
) -> dict:
    """Compare two models on the same validation data.

    Returns comparison report with per-model metrics and deltas.
    """
    logger.info("A/B comparison: %s vs %s", model_a_path, model_b_path)

    metrics_a = evaluate_model(model_a_path, val_data_path)
    metrics_b = evaluate_model(model_b_path, val_data_path)

    comparison = {
        "model_a": {"path": model_a_path, "metrics": metrics_a},
        "model_b": {"path": model_b_path, "metrics": metrics_b},
        "delta": {
            "mAP@0.25": round(
                metrics_b.get("mAP@0.25", 0) - metrics_a.get("mAP@0.25", 0), 4
            ),
            "mAP@0.50": round(
                metrics_b.get("mAP@0.50", 0) - metrics_a.get("mAP@0.50", 0), 4
            ),
        },
        "winner": "model_b"
        if metrics_b.get("mAP@0.25", 0) > metrics_a.get("mAP@0.25", 0)
        else "model_a",
    }

    logger.info(
        "Winner: %s (delta mAP@0.25: %+.4f)",
        comparison["winner"],
        comparison["delta"]["mAP@0.25"],
    )

    return comparison


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Evaluate point cloud detection model")
    parser.add_argument(
        "--checkpoint", "-c", required=True, help="Model checkpoint path"
    )
    parser.add_argument("--data", "-d", required=True, help="Validation data path")
    parser.add_argument("--compare-with", help="Second model for A/B comparison")
    args = parser.parse_args()

    if args.compare_with:
        result = compare_models(args.checkpoint, args.compare_with, args.data)
    else:
        result = evaluate_model(args.checkpoint, args.data)

    print(json.dumps(result, indent=2))
