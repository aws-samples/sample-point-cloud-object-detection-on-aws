"""Tests for the fine-tuning training pipeline."""

import json
from pathlib import Path

import pytest

from pointcloud_detect.training.config import AugmentationConfig, TrainingConfig
from pointcloud_detect.training.data_loader import (
    _validate_annotation,
    get_class_names,
    load_annotations,
    split_train_val,
)


@pytest.fixture
def sample_annotations(tmp_path: Path) -> Path:
    """Create sample annotation files for testing."""
    ann_dir = tmp_path / "annotations"
    ann_dir.mkdir()

    for i in range(5):
        ann = {
            "scene_id": f"scene_{i:03d}",
            "point_cloud_path": f"/data/scene_{i:03d}.ply",
            "num_annotations": 2,
            "annotations": [
                {
                    "center_xyz": [float(i), 0.0, 1.0],
                    "size_wlh": [1.0, 0.2, 2.1],
                    "heading": 0.0,
                    "class_name": "door",
                },
                {
                    "center_xyz": [float(i) + 2, 0.0, 1.5],
                    "size_wlh": [1.2, 0.2, 1.2],
                    "heading": 0.5,
                    "class_name": "window",
                },
            ],
        }
        (ann_dir / f"scene_{i:03d}.json").write_text(json.dumps(ann))

    return ann_dir


class TestTrainingConfig:
    def test_default_config(self) -> None:
        config = TrainingConfig()
        assert config.base_model == "fcaf3d_scannet"
        assert config.epochs == 50
        assert config.learning_rate == 0.001
        assert config.val_split == 0.2

    def test_custom_config(self) -> None:
        config = TrainingConfig(
            base_model="votenet_scannet",
            epochs=30,
            learning_rate=0.0005,
            custom_classes=["door"],
        )
        assert config.base_model == "votenet_scannet"
        assert config.epochs == 30
        assert config.custom_classes == ["door"]

    def test_augmentation_config(self) -> None:
        aug = AugmentationConfig()
        assert aug.random_flip is True
        assert aug.random_rotate is True
        assert aug.random_scale == (0.9, 1.1)


class TestDataLoader:
    def test_load_annotations(self, sample_annotations: Path) -> None:
        annotations = load_annotations(str(sample_annotations))
        assert len(annotations) == 5
        assert annotations[0]["scene_id"] == "scene_000"
        assert len(annotations[0]["annotations"]) == 2

    def test_load_annotations_missing_dir(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_annotations("/nonexistent/path")

    def test_load_annotations_empty_dir(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="No annotation JSON files"):
            load_annotations(str(empty_dir))

    def test_validate_annotation_valid(self) -> None:
        ann = {
            "scene_id": "test",
            "point_cloud_path": "/data/test.ply",
            "annotations": [
                {
                    "center_xyz": [1, 2, 3],
                    "size_wlh": [1, 1, 1],
                    "heading": 0.0,
                    "class_name": "door",
                }
            ],
        }
        # Should not raise
        _validate_annotation(ann, "test.json")

    def test_validate_annotation_missing_keys(self) -> None:
        ann = {"scene_id": "test"}  # Missing point_cloud_path and annotations
        with pytest.raises(ValueError, match="missing keys"):
            _validate_annotation(ann, "test.json")

    def test_validate_annotation_bad_center(self) -> None:
        ann = {
            "scene_id": "test",
            "point_cloud_path": "/data/test.ply",
            "annotations": [
                {
                    "center_xyz": [1, 2],
                    "size_wlh": [1, 1, 1],
                    "heading": 0.0,
                    "class_name": "door",
                }
            ],
        }
        with pytest.raises(ValueError, match="center_xyz must have 3"):
            _validate_annotation(ann, "test.json")

    def test_split_train_val(self, sample_annotations: Path) -> None:
        annotations = load_annotations(str(sample_annotations))
        train, val = split_train_val(annotations, val_split=0.2, seed=42)
        assert len(train) + len(val) == 5
        assert len(val) >= 1

    def test_split_deterministic(self, sample_annotations: Path) -> None:
        annotations = load_annotations(str(sample_annotations))
        train1, val1 = split_train_val(annotations, val_split=0.2, seed=42)
        train2, val2 = split_train_val(annotations, val_split=0.2, seed=42)
        assert [a["scene_id"] for a in train1] == [a["scene_id"] for a in train2]

    def test_get_class_names(self, sample_annotations: Path) -> None:
        annotations = load_annotations(str(sample_annotations))
        classes = get_class_names(annotations)
        assert classes == ["door", "window"]


class TestTrainer:
    def test_run_training_stub(self, sample_annotations: Path, tmp_path: Path) -> None:
        from pointcloud_detect.training.trainer import run_training

        config = TrainingConfig(
            data_path=str(sample_annotations),
            output_path=str(tmp_path / "output"),
            epochs=5,
            checkpoint_interval=2,
        )
        result = run_training(config)

        assert result["epochs_completed"] == 5
        assert result["class_names"] == ["door", "window"]
        assert result["train_scenes"] == 4
        assert result["val_scenes"] == 1
        assert Path(result["checkpoint_path"]).exists()

    def test_checkpoints_saved(self, sample_annotations: Path, tmp_path: Path) -> None:
        from pointcloud_detect.training.trainer import run_training

        config = TrainingConfig(
            data_path=str(sample_annotations),
            output_path=str(tmp_path / "output"),
            epochs=10,
            checkpoint_interval=5,
        )
        run_training(config)

        output_dir = tmp_path / "output"
        assert (output_dir / "checkpoint_epoch_5.json").exists()
        assert (output_dir / "checkpoint_epoch_10.json").exists()
        assert (output_dir / "model_final.json").exists()


class TestInferenceScriptCheckpoints:
    """T28: full unpickling needs an explicit --trust-checkpoint."""

    SCRIPTS = ("run_centerpoint_inference.py", "run_finetuned_inference.py")

    @staticmethod
    def _load_script(name: str):
        import importlib.util

        path = Path(__file__).parent.parent / "scripts" / name
        spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.fixture
    def fake_torch(self, monkeypatch: pytest.MonkeyPatch):
        import sys
        import types

        calls: list[dict] = []
        torch = types.ModuleType("torch")
        torch.serialization = types.SimpleNamespace(  # type: ignore[attr-defined]
            unsafe=["mmengine.logging.history_buffer.HistoryBuffer"]
        )

        def inspect(path):
            calls.append({"inspected": path})
            return torch.serialization.unsafe

        def load(path, map_location=None, weights_only=None):
            calls.append({"path": path, "weights_only": weights_only})
            return {"state_dict": {}}

        torch.serialization.get_unsafe_globals_in_checkpoint = inspect  # type: ignore[attr-defined]
        torch.load = load  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", torch)
        return torch, calls

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_flag_defaults_off(self, script: str) -> None:
        import argparse

        module = self._load_script(script)
        parser = argparse.ArgumentParser()
        module.add_trust_checkpoint_argument(parser)
        assert parser.parse_args([]).trust_checkpoint is False
        assert parser.parse_args(["--trust-checkpoint"]).trust_checkpoint is True

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_untrusted_load_refuses_unsafe_globals_without_unpickling(
        self, script: str, fake_torch
    ) -> None:
        _, calls = fake_torch
        module = self._load_script(script)
        with pytest.raises(SystemExit) as err:
            module.load_checkpoint("ckpt.pth", "cpu", trusted=False)
        assert "--trust-checkpoint" in str(err.value.code)
        assert "HistoryBuffer" in str(err.value.code)
        # Inspected, never loaded.
        assert calls == [{"inspected": "ckpt.pth"}]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_untrusted_plain_weights_load_weights_only(
        self, script: str, fake_torch
    ) -> None:
        torch, calls = fake_torch
        torch.serialization.unsafe = []
        module = self._load_script(script)
        assert module.load_checkpoint("ckpt.pth", "cpu", trusted=False) == {
            "state_dict": {}
        }
        assert calls == [
            {"inspected": "ckpt.pth"},
            {"path": "ckpt.pth", "weights_only": True},
        ]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_trusted_load_warns_and_unpickles(
        self, script: str, fake_torch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, calls = fake_torch
        module = self._load_script(script)
        assert module.load_checkpoint("ckpt.pth", "cpu", trusted=True) == {
            "state_dict": {}
        }
        assert calls == [{"path": "ckpt.pth", "weights_only": False}]
        assert "runs any code" in capsys.readouterr().err

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_torch_load_is_only_patched_behind_the_flag(self, script: str) -> None:
        # The global patch changes torch.load's default for mmengine's loaders
        # too, so main() must apply it only when the flag was given.
        source = (Path(__file__).parent.parent / "scripts" / script).read_text()
        body = source.split("def main", 1)[1]
        assert "if args.trust_checkpoint:\n        monkey_patch_torch_load()" in body
        # The only full-unpickling load left is load_checkpoint's trusted branch.
        assert "torch.load(" not in body


class TestInfoFiles:
    """The training info files are JSON, so training never unpickles a file."""

    @staticmethod
    def _scene(tmp_path: Path, name: str, cols: int) -> Path:
        import numpy as np

        points = np.random.default_rng(0).random((64, cols), dtype=np.float32) * 4
        path = tmp_path / f"{name}.bin"
        points.tofile(str(path))
        return path

    def test_trainer_writes_a_json_info_file(self, tmp_path: Path) -> None:
        import json

        from pointcloud_detect.training.mmdet3d_trainer import (
            convert_annotations_to_scannet_format,
        )

        bin_path = self._scene(tmp_path, "scene", 6)
        ann = {
            "scene_id": "scene",
            "point_cloud_path": str(bin_path),
            "annotations": [
                {
                    "class_name": "Door",
                    "center_xyz": [1.0, 2.0, 0.5],
                    "size_wlh": [0.9, 0.1, 2.0],
                    "heading": 0.0,
                }
            ],
        }
        out = tmp_path / "out"
        assert convert_annotations_to_scannet_format([ann], ["door"], out) == 1
        assert not list(out.glob("*.pkl"))
        info = json.loads((out / "scannet_infos_train.json").read_text())
        (box,) = [i["bbox_3d"] for i in info["data_list"][0]["instances"]]
        assert all(type(v) is float for v in box) and len(box) == 7
        assert info["metainfo"]["classes"] == ["door"]

    def test_centerpoint_script_writes_a_json_info_file(self, tmp_path: Path) -> None:
        import importlib.util
        import json

        path = Path(__file__).parent.parent / "scripts" / "finetune_centerpoint.py"
        spec = importlib.util.spec_from_file_location("finetune_centerpoint", path)
        assert spec is not None and spec.loader is not None
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)

        data = tmp_path / "data"
        data.mkdir()
        self._scene(data, "frame", 5)
        ann = {
            "scene_id": "frame",
            "point_cloud_path": "frame.bin",
            "annotations": [
                {
                    "class_name": "guardrail",
                    "center_xyz": [5.0, 1.0, 0.4],
                    "size_wlh": [0.3, 4.0, 0.8],
                    "heading": 0.1,
                }
            ],
        }
        (data / "frame.json").write_text(json.dumps(ann))
        out = tmp_path / "out"
        n_scenes, _ = script.build_nuscenes_infos(str(data), ["guardrail"], out)
        assert n_scenes == 1
        assert not list(out.glob("*.pkl"))
        info = json.loads((out / "nuscenes_infos_train.json").read_text())
        assert info["data_list"][0]["instances"][0]["bbox_3d"] == [
            5.0,
            1.0,
            0.4,
            0.3,
            4.0,
            0.8,
            0.1,
        ]


class TestCleanTraining:
    """Training defaults to scratch; pretrained weights are opt-in and gated."""

    def test_default_is_from_scratch(self) -> None:
        assert TrainingConfig().base_weights == "scratch"

    def test_architecture_key(self) -> None:
        from pointcloud_detect.training import mmdet3d_trainer as mt

        assert mt.architecture_key("fcaf3d_scannet") == mt.FCAF3D_KEY
        assert mt.architecture_key("FCAF3D_custom_ft_1") == mt.FCAF3D_KEY
        assert mt.architecture_key("votenet_scannet") == mt.VOTENET_KEY
        assert mt.architecture_key("anything_else") == mt.VOTENET_KEY

    @pytest.mark.parametrize("weights", ["scratch", "none", " Scratch "])
    def test_scratch_fetches_nothing(self, weights: str) -> None:
        from pointcloud_detect.training.mmdet3d_trainer import resolve_load_from

        def fetch(url: str) -> str:
            raise AssertionError(f"fetched {url}")

        assert resolve_load_from("votenet_scannet", weights, fetch=fetch) is None

    def test_a_path_is_used_as_given_and_never_fetched(self) -> None:
        from pointcloud_detect.training.mmdet3d_trainer import resolve_load_from

        def fetch(url: str) -> str:
            raise AssertionError(f"fetched {url}")

        got = resolve_load_from("fcaf3d_scannet", "models/mine.pth", fetch=fetch)
        assert got == "models/mine.pth"

    def test_pretrained_fetches_the_trained_architectures_own_checkpoint(self) -> None:
        from pointcloud_detect.backends.mmdet3d import DETECTION_CONFIGS
        from pointcloud_detect.training import mmdet3d_trainer as mt

        calls: list[str] = []

        def fetch(url: str) -> str:
            calls.append(url)
            return "/cache/x.pth"

        assert mt.resolve_load_from("votenet_x", "pretrained", fetch=fetch)
        assert calls == [DETECTION_CONFIGS[mt.VOTENET_KEY]["checkpoint"]]

    def test_pretrained_is_refused_without_the_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pointcloud_detect.backends.checkpoints import NonCommercialWeightsDisabled
        from pointcloud_detect.training.mmdet3d_trainer import resolve_load_from

        monkeypatch.delenv("ALLOW_NONCOMMERCIAL_WEIGHTS", raising=False)
        with pytest.raises(NonCommercialWeightsDisabled):
            resolve_load_from("votenet_scannet", "pretrained")

    def test_patched_metainfo_keeps_stock_keys(self) -> None:
        from pointcloud_detect.training.mmdet3d_trainer import patched_metainfo

        stock = {"classes": ("a", "b", "c"), "seg_all_class_ids": (0, 1, 2), "x": 1}
        meta = patched_metainfo(stock, ["door", "window"])
        assert meta["classes"] == ("door", "window")
        assert meta["seg_all_class_ids"] == (0, 1)
        assert meta["seg_valid_class_ids"] == (0, 1)
        assert len(meta["palette"]) == 2
        assert meta["x"] == 1
        assert stock["classes"] == ("a", "b", "c")  # not mutated

    def test_the_clean_recipe_config_trains_from_scratch(self) -> None:
        import yaml

        root = Path(__file__).resolve().parents[1]
        path = root / "configs" / "train_clean_indoor_craslab.yaml"
        cfg = TrainingConfig(**yaml.safe_load(path.read_text()))
        assert cfg.base_weights == "scratch"
        assert cfg.custom_classes
