"""MMDetection3D backend for 3D object detection and segmentation."""

import logging
from pathlib import Path
from typing import Any

import numpy as np

from pointcloud_detect.backends import checkpoints
from pointcloud_detect.config import ModelConfig
from pointcloud_detect.inference import DetectionBackend, SegmentationBackend
from pointcloud_detect.models import Detection, Tile

logger = logging.getLogger(__name__)

DETECTION_CONFIGS: dict[str, dict[str, str]] = {
    "fcaf3d_scannet": {
        "config": "fcaf3d/fcaf3d_2xb8_scannet-3d-18class.py",
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/"
            "fcaf3d/fcaf3d_8x2_scannet-3d-18class/"
            "fcaf3d_8x2_scannet-3d-18class_20220805_084956.pth"
        ),
        "num_dims": "6",
        "coord_type": "DEPTH",
    },
    "centerpoint_nuscenes": {
        "config": (
            "centerpoint/"
            "centerpoint_voxel01_second_secfpn_head-circlenms"
            "_8xb4-cyclic-20e_nus-3d.py"
        ),
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/"
            "centerpoint/"
            "centerpoint_01voxel_second_secfpn_circlenms_4x8_cyclic_20e_nus/"
            "centerpoint_01voxel_second_secfpn_circlenms_4x8_cyclic_20e_nus"
            "_20220810_030004-9061688e.pth"
        ),
        "num_dims": "5",
        "coord_type": "LIDAR",
    },
    "pointpillars_kitti": {
        "config": "pointpillars/pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py",
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/"
            "pointpillars/"
            "hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class/"
            "hv_pointpillars_secfpn_6x8_160e_kitti-3d-3class"
            "_20220301_150306-37dc2420.pth"
        ),
        "num_dims": "4",
        "coord_type": "LIDAR",
    },
    "votenet_scannet": {
        "config": "votenet/votenet_8xb8_scannet-3d.py",
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v1.0.0_models/"
            "votenet/"
            "votenet_8x8_scannet-3d-18class/"
            "votenet_8x8_scannet-3d-18class_20210823_234503-cf8134fa.pth"
        ),
        "num_dims": "4",
        "coord_type": "DEPTH",
    },
}

SEGMENTATION_CONFIGS: dict[str, dict[str, str]] = {
    "pointnet2_scannet": {
        "config": "pointnet2/pointnet2_ssg_2xb16-cosine-200e_scannet-seg.py",
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v0.1.0_models/"
            "pointnet2/"
            "pointnet2_ssg_16x2_cosine_200e_scannet_seg-3d-20class/"
            "pointnet2_ssg_16x2_cosine_200e_scannet_seg-3d-20class"
            "_20210514_143644-ee73704a.pth"
        ),
        "num_dims": "6",
    },
    "pointnet2_scannet_xyz": {
        "config": ("pointnet2/pointnet2_ssg_2xb16-cosine-200e_scannet-seg-xyz-only.py"),
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v0.1.0_models/"
            "pointnet2/"
            "pointnet2_ssg_xyz-only_16x2_cosine_200e_scannet_seg-3d-20class/"
            "pointnet2_ssg_xyz-only_16x2_cosine_200e_scannet_seg-3d-20class"
            "_20210514_143628-4e341a48.pth"
        ),
        "num_dims": "3",
    },
    "pointnet2_s3dis": {
        "config": "pointnet2/pointnet2_ssg_2xb16-cosine-50e_s3dis-seg.py",
        "checkpoint": (
            "https://download.openmmlab.com/mmdetection3d/v0.1.0_models/"
            "pointnet2/"
            "pointnet2_ssg_16x2_cosine_50e_s3dis_seg-3d-13class/"
            "pointnet2_ssg_16x2_cosine_50e_s3dis_seg-3d-13class"
            "_20210514_144205-995d0119.pth"
        ),
        "num_dims": "6",
    },
}

SCANNET_CLASSES = [
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

NUSCENES_CLASSES = [
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

KITTI_CLASSES = [
    "car",
    "pedestrian",
    "cyclist",
]

SCANNET_SEG_CLASSES = [
    "wall",
    "floor",
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

S3DIS_CLASSES = [
    "ceiling",
    "floor",
    "wall",
    "beam",
    "column",
    "window",
    "door",
    "table",
    "chair",
    "sofa",
    "bookcase",
    "board",
    "clutter",
]


def _find_mmdet3d_configs_dir() -> Path:
    """Locate the mmdetection3d configs directory."""
    import os

    env_dir = os.environ.get("MMDET3D_DIR")
    if env_dir:
        p = Path(env_dir) / "configs"
        if p.exists():
            return p

    try:
        import mmdet3d  # pyright: ignore[reportMissingImports]

        # A namespace package has __file__ = None, and Path(None) raises TypeError
        # rather than the ImportError this block is prepared to swallow. Skip the
        # package-relative lookup in that case and fall through to the candidates.
        if mmdet3d.__file__ is not None:
            pkg_dir = Path(mmdet3d.__file__).parent

            # pip-installed mmdet3d stores configs under .mim/configs/
            mim_configs = pkg_dir / ".mim" / "configs"
            if mim_configs.exists():
                return mim_configs

            # Source install: configs next to package
            source_configs = pkg_dir.parent / "configs"
            if source_configs.exists():
                return source_configs

    except ImportError:
        pass

    candidates = [
        Path.home() / "mmdetection3d" / "configs",
        Path("/opt/mmdetection3d/configs"),
        Path("./mmdetection3d/configs"),
    ]
    for c in candidates:
        if c.exists():
            return c

    raise FileNotFoundError(
        "Cannot locate mmdetection3d configs directory. "
        "Set MMDET3D_DIR environment variable or install mmdet3d from source."
    )


def _prepare_points_for_detection(
    tile: Tile, num_dims: int, has_rgb: bool, pipeline_normalizes_color: bool
) -> np.ndarray:
    """Prepare a point array with the correct dimensionality for the model.

    ``pipeline_normalizes_color`` says whether the model's own test pipeline contains
    a ``NormalizePointsColor`` transform. It has no default on purpose: the value
    decides whether color is handed over as 0-255 or 0-1, and guessing it wrong
    silently degrades results instead of raising. Callers should derive it with
    ``_pipeline_has(model.cfg.test_pipeline, "NormalizePointsColor")``.
    """
    n_points = tile.xyz.shape[0]

    if num_dims == 6:
        points = np.zeros((n_points, 6), dtype=np.float32)
        points[:, :3] = tile.xyz.astype(np.float32)
        if has_rgb and tile.rgb is not None:
            rgb = tile.rgb.astype(np.float32)
            points[:, 3:6] = rgb if pipeline_normalizes_color else rgb / 255.0
    elif num_dims == 5:
        points = np.zeros((n_points, 5), dtype=np.float32)
        points[:, :3] = tile.xyz.astype(np.float32)
        if tile.intensity is not None:
            points[:, 3] = tile.intensity
    elif num_dims == 4:
        points = np.zeros((n_points, 4), dtype=np.float32)
        points[:, :3] = tile.xyz.astype(np.float32)
        if tile.intensity is not None:
            points[:, 3] = tile.intensity
    else:  # 3, or any other count: xyz only
        points = tile.xyz.astype(np.float32)

    return points


def _pipeline_has(pipeline: Any, transform_type: str) -> bool:
    """Is ``transform_type`` present anywhere in an mmdet3d test pipeline?

    Pipelines are lists of dicts, but a transform may nest further stages under a
    ``transforms`` key (``MultiScaleFlipAug3D`` does this), so the search recurses
    rather than only checking the top level — otherwise a nested
    ``NormalizePointsColor`` would be missed and color scaled twice.
    """
    if isinstance(pipeline, dict):
        if pipeline.get("type") == transform_type:
            return True
        return _pipeline_has(pipeline.get("transforms"), transform_type)
    if isinstance(pipeline, (list, tuple)):
        return any(_pipeline_has(stage, transform_type) for stage in pipeline)
    return False


class MMDet3DDetectionBackend(DetectionBackend):
    """MMDetection3D detection backend."""

    def __init__(self, device: str = "auto") -> None:
        self._model: Any = None
        self._class_names: list[str] = []
        self._num_dims: int = 6
        self._device: str = device

    def load_model(self, config: ModelConfig) -> None:
        """Load detection model from config + checkpoint."""
        from mmdet3d.apis import init_model  # pyright: ignore[reportMissingImports]

        model_key = config.detection.config
        if model_key not in DETECTION_CONFIGS:
            raise ValueError(
                f"Unknown detection config: {model_key}. "
                f"Available: {list(DETECTION_CONFIGS.keys())}"
            )

        model_info = DETECTION_CONFIGS[model_key]
        self._num_dims = int(model_info["num_dims"])

        configs_dir = _find_mmdet3d_configs_dir()
        config_path = str(configs_dir / model_info["config"])

        if config.detection.weights == "pretrained":
            checkpoint = str(checkpoints.fetch(model_info["checkpoint"]))
        else:
            checkpoint = config.detection.weights

        device = self._resolve_device()
        checkpoints.enforce_weights_only()
        self._model = init_model(config_path, checkpoint, device=device)

        if model_key == "fcaf3d_scannet":
            self._class_names = SCANNET_CLASSES
        elif model_key == "votenet_scannet":
            self._class_names = SCANNET_CLASSES
        elif model_key == "centerpoint_nuscenes":
            self._class_names = NUSCENES_CLASSES
        elif model_key == "pointpillars_kitti":
            self._class_names = KITTI_CLASSES
        else:
            self._class_names = [f"class_{i}" for i in range(100)]

        logger.info("Loaded detection model: %s on %s", model_key, device)

    def detect(self, tile: Tile) -> list[Detection]:
        """Run detection inference on a tile."""
        import tempfile

        from mmdet3d.apis import (  # pyright: ignore[reportMissingImports]
            inference_detector,
        )

        has_rgb = tile.rgb is not None
        n_points = tile.xyz.shape[0]

        # Determine the load_dim expected by the model's test pipeline, and whether
        # that pipeline normalizes color itself.
        model_cfg = self._model.cfg
        load_dim = 6  # default for ScanNet-style models
        pipeline_normalizes_color = _pipeline_has(
            model_cfg.test_pipeline, "NormalizePointsColor"
        )
        for t in model_cfg.test_pipeline:
            if isinstance(t, dict) and t.get("type") == "LoadPointsFromFile":
                load_dim = t.get("load_dim", 6)
                break

        # Build points array matching expected load_dim.
        #
        # Color scaling is decided by the pipeline, not assumed. Color models such as
        # FCAF3D ScanNet include a `NormalizePointsColor` transform that divides color
        # by 255 itself; dividing here as well double-normalized to ~0, starved FCAF3D
        # of the color features it depends on, and produced zero detections on every
        # scene. VoteNet was unaffected only because its config is XYZ-only and never
        # reads the color columns.
        #
        # Writing raw 0-255 unconditionally would just move the bug: a color config
        # WITHOUT that transform expects 0-1 and would receive 255x its intended
        # range. Since the pipeline is already introspected here for load_dim,
        # inspecting it for the transform too costs nothing and removes the
        # dependency on a specific upstream config's contents.
        points = np.zeros((n_points, load_dim), dtype=np.float32)
        points[:, :3] = tile.xyz.astype(np.float32)
        if load_dim >= 6 and has_rgb and tile.rgb is not None:
            rgb = tile.rgb.astype(np.float32)
            points[:, 3:6] = rgb if pipeline_normalizes_color else rgb / 255.0
        elif load_dim >= 4 and tile.intensity is not None:
            points[:, 3] = tile.intensity

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            points.tofile(f)
            # Read only after the with block has closed the file.
            tmp_path = f.name  # nosemgrep: tempfile-without-flush

        try:
            result, _ = inference_detector(self._model, tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        pred = result.pred_instances_3d
        bboxes = pred.bboxes_3d.tensor.cpu().numpy()
        scores = pred.scores_3d.cpu().numpy()
        labels = pred.labels_3d.cpu().numpy()

        detections: list[Detection] = []
        for i in range(len(scores)):
            bbox = bboxes[i]
            cx, cy, cz, dx, dy, dz, yaw = (
                bbox[0],
                bbox[1],
                bbox[2],
                bbox[3],
                bbox[4],
                bbox[5],
                bbox[6],
            )
            label_idx = int(labels[i])
            label_name = (
                self._class_names[label_idx]
                if label_idx < len(self._class_names)
                else f"class_{label_idx}"
            )
            detections.append(
                Detection(
                    label=label_name,
                    confidence=float(scores[i]),
                    center=np.array([cx, cy, cz], dtype=np.float64),
                    size=np.array([dx, dy, dz], dtype=np.float64),
                    heading=float(yaw),
                )
            )

        return detections

    def _resolve_device(self) -> str:
        if self._device != "auto":
            return self._device
        try:
            import torch  # pyright: ignore[reportMissingImports]

            if torch.cuda.is_available():
                return "cuda:0"
        except ImportError:
            pass
        return "cpu"


class MMDet3DSegmentationBackend(SegmentationBackend):
    """MMDetection3D segmentation backend."""

    def __init__(self, device: str = "auto") -> None:
        self._model: Any = None
        self._class_names: list[str] = []
        self._num_dims: int = 6
        self._device: str = device

    def load_model(self, config: ModelConfig) -> None:
        """Load segmentation model from config + checkpoint."""
        from mmdet3d.apis import init_model  # pyright: ignore[reportMissingImports]

        model_key = config.segmentation.config
        if model_key not in SEGMENTATION_CONFIGS:
            raise ValueError(
                f"Unknown segmentation config: {model_key}. "
                f"Available: {list(SEGMENTATION_CONFIGS.keys())}"
            )

        model_info = SEGMENTATION_CONFIGS[model_key]
        self._num_dims = int(model_info["num_dims"])

        configs_dir = _find_mmdet3d_configs_dir()
        config_path = str(configs_dir / model_info["config"])

        if config.segmentation.weights == "pretrained":
            checkpoint = str(checkpoints.fetch(model_info["checkpoint"]))
        else:
            checkpoint = config.segmentation.weights

        device = self._resolve_device()
        checkpoints.enforce_weights_only()
        self._model = init_model(config_path, checkpoint, device=device)

        if "s3dis" in model_key:
            self._class_names = S3DIS_CLASSES
        else:
            self._class_names = SCANNET_SEG_CLASSES

        logger.info("Loaded segmentation model: %s on %s", model_key, device)

    def segment(self, tile: Tile) -> tuple[np.ndarray, list[str], np.ndarray]:
        """Run segmentation inference on a tile.

        Returns per-point label indices, the class name list, and per-point
        confidences.
        """
        import tempfile

        import torch  # pyright: ignore[reportMissingImports]
        from mmdet3d.apis import (  # pyright: ignore[reportMissingImports]
            inference_segmentor,
        )

        has_rgb = tile.rgb is not None

        # Same color-scaling decision as the detection path. Segmentation configs for
        # S3DIS and ScanNet carry `NormalizePointsColor`, which divides by 255 itself,
        # so this path must hand over raw 0-255 for those — but hardcoding raw would
        # give 255x the intended range to any color config without the transform.
        pipeline_normalizes_color = _pipeline_has(
            self._model.cfg.test_pipeline, "NormalizePointsColor"
        )
        points = _prepare_points_for_detection(
            tile, self._num_dims, has_rgb, pipeline_normalizes_color
        )

        # inference_segmentor expects a file path; write points to a temp .bin file
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            points.tofile(f)
            # Read only after the with block has closed the file.
            tmp_path = f.name  # nosemgrep: tempfile-without-flush

        try:
            result, _ = inference_segmentor(self._model, tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        labels = result.pred_pts_seg.pts_semantic_mask.cpu().numpy().astype(np.int32)

        # Extract per-point confidence (max softmax probability)
        if hasattr(result.pred_pts_seg, "pts_seg_logits"):
            import torch  # pyright: ignore[reportMissingImports]

            logits = result.pred_pts_seg.pts_seg_logits.cpu()
            confidences = torch.softmax(logits, dim=1).max(dim=1).values.numpy()
        else:
            confidences = np.ones(len(labels), dtype=np.float32)

        return labels, self._class_names, confidences

    def _resolve_device(self) -> str:
        if self._device != "auto":
            return self._device
        try:
            import torch  # pyright: ignore[reportMissingImports]

            if torch.cuda.is_available():
                return "cuda:0"
        except ImportError:
            pass
        return "cpu"
