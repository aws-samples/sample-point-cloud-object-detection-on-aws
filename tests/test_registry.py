"""Tests for the model registry."""

from pathlib import Path

import pytest

from pointcloud_detect.registry.store import ModelRegistry


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    """Create a fresh registry for testing."""
    return ModelRegistry(str(tmp_path / "models"))


class TestModelRegistry:
    def test_register_and_get(self, registry: ModelRegistry) -> None:
        entry = registry.register(
            version="v1.0.0",
            checkpoint_path="/models/model_v1.pth",
            base_model="votenet_scannet",
            eval_metrics={"mAP@0.25": 0.65, "mAP@0.50": 0.45},
            dataset_info="ScanNet doors, 30 epochs",
        )

        assert entry["version"] == "v1.0.0"
        assert entry["base_model"] == "votenet_scannet"

        retrieved = registry.get("v1.0.0")
        assert retrieved is not None
        assert retrieved["version"] == "v1.0.0"
        assert retrieved["eval_metrics"]["mAP@0.25"] == 0.65

    def test_get_latest(self, registry: ModelRegistry) -> None:
        registry.register(
            version="v1.0.0",
            checkpoint_path="/m1.pth",
            base_model="votenet",
            eval_metrics={},
        )
        registry.register(
            version="v1.0.1",
            checkpoint_path="/m2.pth",
            base_model="votenet",
            eval_metrics={},
        )
        registry.register(
            version="v1.0.2",
            checkpoint_path="/m3.pth",
            base_model="votenet",
            eval_metrics={},
        )

        latest = registry.get()  # No version = latest
        assert latest is not None
        assert latest["version"] == "v1.0.2"

    def test_get_nonexistent_version(self, registry: ModelRegistry) -> None:
        result = registry.get("v99.0.0")
        assert result is None

    def test_get_empty_registry(self, registry: ModelRegistry) -> None:
        result = registry.get()
        assert result is None

    def test_list_all(self, registry: ModelRegistry) -> None:
        registry.register(
            version="v1.0.0",
            checkpoint_path="/m1.pth",
            base_model="votenet",
            eval_metrics={"mAP@0.25": 0.5},
        )
        registry.register(
            version="v1.0.1",
            checkpoint_path="/m2.pth",
            base_model="votenet",
            eval_metrics={"mAP@0.25": 0.7},
        )

        models = registry.list()
        assert len(models) == 2

    def test_list_with_min_map_filter(self, registry: ModelRegistry) -> None:
        registry.register(
            version="v1.0.0",
            checkpoint_path="/m1.pth",
            base_model="votenet",
            eval_metrics={"mAP@0.25": 0.4},
        )
        registry.register(
            version="v1.0.1",
            checkpoint_path="/m2.pth",
            base_model="votenet",
            eval_metrics={"mAP@0.25": 0.7},
        )
        registry.register(
            version="v1.0.2",
            checkpoint_path="/m3.pth",
            base_model="votenet",
            eval_metrics={"mAP@0.25": 0.85},
        )

        filtered = registry.list(min_map=0.6)
        assert len(filtered) == 2
        assert all(m["eval_metrics"]["mAP@0.25"] >= 0.6 for m in filtered)

    def test_auto_version(self, registry: ModelRegistry) -> None:
        assert registry.get_latest_version() == "v1.0.0"

        registry.register(
            version="v1.0.0",
            checkpoint_path="/m1.pth",
            base_model="votenet",
            eval_metrics={},
        )
        assert registry.get_latest_version() == "v1.0.1"

        registry.register(
            version="v1.0.1",
            checkpoint_path="/m2.pth",
            base_model="votenet",
            eval_metrics={},
        )
        assert registry.get_latest_version() == "v1.0.2"

    def test_persistence(self, tmp_path: Path) -> None:
        """Registry persists across instances."""
        registry_path = str(tmp_path / "models")

        # Write with one instance
        reg1 = ModelRegistry(registry_path)
        reg1.register(
            version="v1.0.0",
            checkpoint_path="/m1.pth",
            base_model="votenet",
            eval_metrics={"mAP@0.25": 0.65},
        )

        # Read with a new instance
        reg2 = ModelRegistry(registry_path)
        result = reg2.get("v1.0.0")
        assert result is not None
        assert result["eval_metrics"]["mAP@0.25"] == 0.65

    def test_register_with_training_config(self, registry: ModelRegistry) -> None:
        entry = registry.register(
            version="v1.0.0",
            checkpoint_path="/models/model.pth",
            base_model="votenet_scannet",
            eval_metrics={"mAP@0.25": 0.7},
            training_config={"epochs": 30, "lr": 0.0005, "batch_size": 4},
            dataset_info="Livox office scans, 5 scenes",
        )

        assert entry["training_config"]["epochs"] == 30
        assert entry["dataset_info"] == "Livox office scans, 5 scenes"
