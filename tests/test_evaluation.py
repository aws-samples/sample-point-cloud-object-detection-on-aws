"""Tests for model evaluation and comparison."""

import json
from pathlib import Path

import pytest

from pointcloud_detect.evaluation.evaluator import compare_models, evaluate_model


@pytest.fixture
def sample_val_data(tmp_path: Path) -> Path:
    """Create sample validation annotations."""
    val_dir = tmp_path / "val"
    val_dir.mkdir()

    for i in range(3):
        ann = {
            "scene_id": f"val_scene_{i}",
            "point_cloud_path": f"/data/val_{i}.ply",
            "num_annotations": 2,
            "annotations": [
                {
                    "center_xyz": [float(i), 0.0, 1.0],
                    "size_wlh": [1.0, 0.2, 2.1],
                    "heading": 0.0,
                    "class_name": "door",
                },
                {
                    "center_xyz": [float(i) + 3, 1.0, 1.5],
                    "size_wlh": [0.4, 0.4, 3.0],
                    "heading": 0.0,
                    "class_name": "column",
                },
            ],
        }
        (val_dir / f"val_scene_{i}.json").write_text(json.dumps(ann))

    return val_dir


@pytest.fixture
def sample_checkpoint(tmp_path: Path) -> Path:
    """Create a dummy model checkpoint."""
    ckpt = tmp_path / "model.json"
    ckpt.write_text(json.dumps({"epoch": 30, "base_model": "votenet_scannet"}))
    return ckpt


class TestEvaluateModel:
    def test_produces_metrics(
        self, sample_checkpoint: Path, sample_val_data: Path
    ) -> None:
        metrics = evaluate_model(str(sample_checkpoint), str(sample_val_data))

        assert "mAP@0.25" in metrics
        assert "mAP@0.5" in metrics
        assert 0.0 <= metrics["mAP@0.25"] <= 1.0
        assert 0.0 <= metrics["mAP@0.5"] <= 1.0

    def test_includes_per_class(
        self, sample_checkpoint: Path, sample_val_data: Path
    ) -> None:
        metrics = evaluate_model(str(sample_checkpoint), str(sample_val_data))

        assert "per_class_ap" in metrics
        assert "door" in metrics["per_class_ap"]
        assert "column" in metrics["per_class_ap"]
        assert "AP@0.25" in metrics["per_class_ap"]["door"]

    def test_reports_scene_count(
        self, sample_checkpoint: Path, sample_val_data: Path
    ) -> None:
        metrics = evaluate_model(str(sample_checkpoint), str(sample_val_data))

        assert metrics["num_val_scenes"] == 3
        assert metrics["num_classes"] == 2
        assert set(metrics["class_names"]) == {"door", "column"}

    def test_custom_iou_thresholds(
        self, sample_checkpoint: Path, sample_val_data: Path
    ) -> None:
        metrics = evaluate_model(
            str(sample_checkpoint),
            str(sample_val_data),
            iou_thresholds=[0.1, 0.5, 0.75],
        )

        assert "mAP@0.1" in metrics
        assert "mAP@0.5" in metrics
        assert "mAP@0.75" in metrics


class TestCompareModels:
    def test_comparison_returns_winner(
        self, sample_val_data: Path, tmp_path: Path
    ) -> None:
        ckpt_a = tmp_path / "model_a.json"
        ckpt_b = tmp_path / "model_b.json"
        ckpt_a.write_text(json.dumps({"epoch": 10}))
        ckpt_b.write_text(json.dumps({"epoch": 30}))

        comparison = compare_models(str(ckpt_a), str(ckpt_b), str(sample_val_data))

        assert "model_a" in comparison
        assert "model_b" in comparison
        assert "delta" in comparison
        assert "winner" in comparison
        assert comparison["winner"] in ("model_a", "model_b")

    def test_comparison_has_metrics_for_both(
        self, sample_val_data: Path, tmp_path: Path
    ) -> None:
        ckpt_a = tmp_path / "model_a.json"
        ckpt_b = tmp_path / "model_b.json"
        ckpt_a.write_text(json.dumps({"epoch": 10}))
        ckpt_b.write_text(json.dumps({"epoch": 30}))

        comparison = compare_models(str(ckpt_a), str(ckpt_b), str(sample_val_data))

        assert "mAP@0.25" in comparison["model_a"]["metrics"]
        assert "mAP@0.25" in comparison["model_b"]["metrics"]
        assert "mAP@0.25" in comparison["delta"]
