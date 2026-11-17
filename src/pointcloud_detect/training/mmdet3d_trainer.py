"""Real MMDetection3D fine-tuning.

Converts our unified annotation format to mmdet3d's format and runs
actual GPU training using the mmengine Runner.

Usage:
    python -m pointcloud_detect.training.mmdet3d_trainer \
        --config configs/finetune_doors_livox.yaml

Requirements:
    - mmdet3d, mmengine, mmcv, mmdet installed
    - GPU with CUDA
    - Annotations in our unified format (from SUSTechPOINTS convert script)
"""

import json
import logging
from pathlib import Path

import numpy as np

try:
    import mmdet3d.datasets  # noqa: F401 — registers datasets  # pyright: ignore[reportMissingImports]
    import mmdet3d.models  # noqa: F401 — registers models  # pyright: ignore[reportMissingImports]
except ImportError:
    pass

from pointcloud_detect.training.config import TrainingConfig
from pointcloud_detect.training.data_loader import (
    get_class_names,
    load_annotations,
    split_train_val,
)

logger = logging.getLogger(__name__)

# Default door size in meters [width, length, height]
DEFAULT_MEAN_SIZES = {"door": [1.0, 0.2, 2.1], "window": [1.2, 0.2, 1.2]}

# The two indoor architectures this trainer builds, keyed by the DETECTION_CONFIGS
# entry whose mmdet3d config (and, for base_weights="pretrained", checkpoint) it uses.
FCAF3D_KEY = "fcaf3d_scannet"
VOTENET_KEY = "votenet_scannet"
ARCHITECTURE_CONFIGS = {
    FCAF3D_KEY: "fcaf3d/fcaf3d_2xb8_scannet-3d-18class.py",
    VOTENET_KEY: "votenet/votenet_8xb8_scannet-3d.py",
}
SCRATCH_WEIGHTS = frozenset({"scratch", "none"})


def architecture_key(base_model: str) -> str:
    """The architecture to train for ``base_model``: FCAF3D if it names FCAF3D.

    Anything else trains VoteNet, which needs no sparse-convolution extension.
    FCAF3D (anchor-free, MinkowskiEngine) detects better indoors but needs
    MinkowskiEngine, which only the GPU image has.
    """
    return FCAF3D_KEY if "fcaf3d" in base_model.lower() else VOTENET_KEY


def resolve_load_from(base_model: str, base_weights: str, fetch=None) -> str | None:
    """The checkpoint training starts from, or None to train from scratch.

    - "scratch" / "none": random initialization. The result inherits no pretrained
      checkpoint, so the data you train on decides its terms.
    - "pretrained": the trained architecture's OpenMMLab checkpoint, fetched through
      backends.checkpoints, which verifies its pin and refuses unless
      ALLOW_NONCOMMERCIAL_WEIGHTS is set (it was trained on non-commercial data).
      The architecture's own checkpoint, so FCAF3D weights never land in VoteNet.
    - anything else: a path to your own .pth, used as given and never fetched.
    """
    choice = base_weights.strip()
    if choice.lower() in SCRATCH_WEIGHTS:
        return None
    if choice.lower() == "pretrained":
        if fetch is None:
            from pointcloud_detect.backends.checkpoints import fetch
        from pointcloud_detect.backends.mmdet3d import DETECTION_CONFIGS

        url = DETECTION_CONFIGS[architecture_key(base_model)]["checkpoint"]
        return str(fetch(url))
    return choice


def patched_metainfo(stock: dict, classes: list[str]) -> dict:
    """ScanNetDataset.METAINFO for ``classes``, merged into the stock one.

    Replacing the dict outright dropped keys the dataset's __init__ reads
    ('seg_all_class_ids', 'seg_valid_class_ids', palette). Only the class names and
    the per-class id lists change, sized to ``classes``.
    """
    n = len(classes)
    meta = dict(stock)
    meta["classes"] = tuple(classes)
    meta["seg_all_class_ids"] = tuple(range(n))
    meta["seg_valid_class_ids"] = tuple(range(n))
    meta["palette"] = [
        [(37 * i) % 256, (17 * i) % 256, (89 * i) % 256] for i in range(n)
    ]
    return meta


def _read_pcd_points(pcd_path: Path) -> np.ndarray | None:
    """Read XYZ + RGB from a binary PCD file."""
    with open(pcd_path, "rb") as f:
        n_points = 0
        while True:
            line = f.readline().decode("ascii", errors="ignore").strip()
            if line.startswith("POINTS"):
                n_points = int(line.split()[1])
            if line.startswith("DATA"):
                break

        if n_points == 0:
            return None

        raw = f.read(n_points * 16)
        if len(raw) < n_points * 16:
            return None

        data = np.frombuffer(raw, dtype=np.float32).reshape(n_points, 4)
        xyz = data[:, :3]

        # Unpack RGB, kept raw (0-255): the FCAF3D pipeline's NormalizePointsColor
        # divides by 255 itself, and VoteNet reads XYZ only.
        rgb_packed = data[:, 3].view(np.uint32)
        r = ((rgb_packed >> 16) & 0xFF).astype(np.float32)
        g = ((rgb_packed >> 8) & 0xFF).astype(np.float32)
        b = (rgb_packed & 0xFF).astype(np.float32)

        return np.column_stack([xyz, r, g, b]).astype(np.float32)


def convert_annotations_to_scannet_format(
    annotations: list[dict],
    classes: list[str],
    output_dir: Path,
    data_path: str = "",
) -> int:
    """Convert our unified annotations to mmdet3d ScanNet format.

    Returns number of scenes successfully converted.
    """
    points_dir = output_dir / "points"
    points_dir.mkdir(parents=True, exist_ok=True)

    data_list = []

    for ann in annotations:
        scene_id = ann["scene_id"]

        # Try to load point cloud:
        # 1. Check point_cloud_path field (for pre-exported .bin files)
        # 2. Fall back to SUSTechPOINTS PCD
        points = None
        pc_path = ann.get("point_cloud_path", "")

        if pc_path and Path(pc_path).exists():
            # Pre-exported .bin file (e.g., from nuScenes conversion)
            raw = np.fromfile(str(pc_path), dtype=np.float32)
            num_cols = 6  # Default: xyz + rgb/intensity
            if raw.size % 6 == 0:
                num_cols = 6
            elif raw.size % 5 == 0:
                num_cols = 5
            elif raw.size % 4 == 0:
                num_cols = 4
            elif raw.size % 3 == 0:
                num_cols = 3
            points = raw.reshape(-1, num_cols)
            # Ensure at least 6 columns for pipeline compatibility
            if points.shape[1] < 6:
                padded = np.zeros((len(points), 6), dtype=np.float32)
                padded[:, : points.shape[1]] = points
                points = padded
        elif (Path(data_path) / pc_path).exists() and pc_path:
            # Relative path from data_path
            raw = np.fromfile(str(Path(data_path) / pc_path), dtype=np.float32)
            num_cols = 6
            if raw.size % 6 == 0:
                num_cols = 6
            elif raw.size % 5 == 0:
                num_cols = 5
            elif raw.size % 4 == 0:
                num_cols = 4
            points = raw.reshape(-1, num_cols)
            if points.shape[1] < 6:
                padded = np.zeros((len(points), 6), dtype=np.float32)
                padded[:, : points.shape[1]] = points
                points = padded
        else:
            # Fall back to SUSTechPOINTS PCD
            pcd_path = Path(f"tools/SUSTechPOINTS/data/{scene_id}/lidar/000000.pcd")
            if pcd_path.exists():
                points = _read_pcd_points(pcd_path)
            else:
                logger.warning("No point cloud for %s, skipping", scene_id)
                continue

        if points is None or len(points) == 0:
            continue

        # Normalize point cloud to be centered at origin
        # (critical for nuScenes data which uses global coords)
        centroid = points[:, :3].mean(axis=0)
        points[:, :3] -= centroid

        # Save as .bin (N x 6: xyz + rgb)
        points.tofile(str(points_dir / f"{scene_id}.bin"))

        # Convert bbox annotations (also shift centers by centroid)
        instances = []
        for box in ann["annotations"]:
            class_name = box["class_name"].lower()
            if class_name not in classes:
                continue
            label_idx = classes.index(class_name)
            cx, cy, cz = box["center_xyz"]
            # Shift box center by the same centroid
            cx -= centroid[0]
            cy -= centroid[1]
            cz -= centroid[2]
            w, length, h = box["size_wlh"]
            heading = box["heading"]
            instances.append(
                {
                    # float(): the centroid shift leaves numpy scalars, which JSON
                    # cannot hold.
                    "bbox_3d": [float(v) for v in (cx, cy, cz, w, length, h, heading)],
                    "bbox_label_3d": label_idx,
                }
            )

        if not instances:
            continue

        data_list.append(
            {
                "lidar_points": {
                    "lidar_path": f"{scene_id}.bin",
                    "num_pts_feats": 6,
                },
                "axis_align_matrix": np.eye(4).tolist(),
                "pts_instance_mask_path": "",
                "pts_semantic_mask_path": "",
                "instances": instances,
            }
        )

        logger.info("  %s: %d points, %d boxes", scene_id, len(points), len(instances))

    # Save the info file.
    # Store our ACTUAL class names in its metainfo.
    # The dataset class's METAINFO['classes'] will be monkey-patched before
    # instantiation to match (see run_real_training).
    info_data = {
        "data_list": data_list,
        "metainfo": {
            "classes": classes,
            "categories": {c: i for i, c in enumerate(classes)},
        },
    }
    # mmdet3d reads its dataset infos with mmengine.load, which picks the format by the
    # file's extension. JSON rather than the pickle mmdet3d's own converters write, so
    # training reads its info file as data and never unpickles anything to do so.
    with open(output_dir / "scannet_infos_train.json", "w", encoding="utf-8") as f:
        json.dump(info_data, f)

    logger.info("Converted %d scenes to %s", len(data_list), output_dir)
    return len(data_list)


def run_real_training(config: TrainingConfig) -> dict:
    """Run real MMDetection3D fine-tuning.

    1. Load and split annotations
    2. Convert to mmdet3d format
    3. Load the architecture's stock config (FCAF3D or VoteNet) + override for our
       data; start from scratch unless base_weights says otherwise
    4. Run training via mmengine Runner
    5. Return result with checkpoint path
    """
    from mmengine.config import Config  # pyright: ignore[reportMissingImports]
    from mmengine.runner import Runner  # pyright: ignore[reportMissingImports]

    logger.info("=" * 60)
    logger.info("Real mmdet3d fine-tuning")
    logger.info("  Base model: %s", config.base_model)
    logger.info("  Data: %s", config.data_path)
    logger.info("  Epochs: %d", config.epochs)
    logger.info("=" * 60)

    # 1. Load annotations
    annotations = load_annotations(config.data_path)
    classes = config.custom_classes or get_class_names(annotations)
    train_data, val_data = split_train_val(annotations, config.val_split)
    logger.info("Classes: %s", classes)
    logger.info("Train: %d, Val: %d", len(train_data), len(val_data))

    # 2. Convert to scannet format
    output_dir = Path(config.output_path)
    data_dir = output_dir / "scannet_format"
    n_converted = convert_annotations_to_scannet_format(
        train_data, classes, data_dir, config.data_path
    )

    if n_converted == 0:
        raise ValueError("No scenes converted — check annotations and PCD files exist")

    # 3. Load the architecture's stock config
    import mmdet3d  # pyright: ignore[reportMissingImports]

    if mmdet3d.__file__ is None:
        raise RuntimeError(
            "mmdet3d resolved as a namespace package (__file__ is None), so its "
            "bundled configs cannot be located. Reinstall mmdet3d via mim, or set "
            "MMDET3D_DIR to a checkout containing configs/."
        )
    configs_dir = Path(mmdet3d.__file__).parent / ".mim" / "configs"
    arch = architecture_key(config.base_model)
    is_fcaf3d = arch == FCAF3D_KEY
    arch_cfg_path = configs_dir / ARCHITECTURE_CONFIGS[arch]

    if not arch_cfg_path.exists():
        raise FileNotFoundError(f"Architecture config not found: {arch_cfg_path}")

    logger.info("Architecture: %s (config %s)", arch, arch_cfg_path.name)
    cfg = Config.fromfile(str(arch_cfg_path))

    # 4. Override config for our fine-tuning
    cfg.work_dir = str(output_dir)
    cfg.train_cfg.max_epochs = config.epochs
    cfg.optim_wrapper.optimizer.lr = config.learning_rate

    # Starting weights (resolve_load_from). The default is from scratch. A pretrained
    # checkpoint is a local copy verified against its pinned SHA-256, not the URL,
    # which mmengine would download unchecked, and only with the non-commercial
    # opt-in (backends/checkpoints.py). Set explicitly: None also clears any
    # load_from the stock config carries.
    from pointcloud_detect.backends import checkpoints

    checkpoints.enforce_weights_only()
    cfg.load_from = resolve_load_from(config.base_model, config.base_weights)
    logger.info(
        "Starting weights: %s",
        "from scratch" if cfg.load_from is None else cfg.load_from,
    )

    # Disable validation (evaluate separately after training)
    cfg.val_cfg = None
    cfg.val_dataloader = None
    cfg.val_evaluator = None

    # Point to our data
    cfg.train_dataloader.dataset.dataset.data_root = str(data_dir)
    cfg.train_dataloader.dataset.dataset.data_prefix = dict(pts="points")
    cfg.train_dataloader.dataset.dataset.ann_file = "scannet_infos_train.json"
    cfg.train_dataloader.dataset.dataset.metainfo = dict(classes=classes)
    cfg.train_dataloader.dataset.times = max(
        50, 200 // n_converted
    )  # Repeat small datasets
    cfg.train_dataloader.batch_size = config.batch_size
    cfg.train_dataloader.num_workers = (
        0  # Avoid multiprocessing issues with small datasets
    )

    # Model head: set number of classes. FCAF3D is anchor-free (no bbox_coder);
    # VoteNet uses a PartialBinBasedBBoxCoder sized to the class count.
    cfg.model.bbox_head.num_classes = len(classes)
    load_annotations_step = dict(
        type="LoadAnnotations3D",
        with_bbox_3d=True,
        with_label_3d=True,
        with_mask_3d=False,
        with_seg_3d=False,
        backend_args=None,
    )
    pack_step = dict(
        type="Pack3DDetInputs", keys=["points", "gt_bboxes_3d", "gt_labels_3d"]
    )
    if is_fcaf3d:
        # XYZ + RGB with color normalization and no shift_height, as in the stock
        # FCAF3D ScanNet config. The .bin files carry raw 0-255 color, which
        # NormalizePointsColor divides; dividing beforehand starves FCAF3D of color.
        cfg.train_dataloader.dataset.dataset.pipeline = [
            dict(
                type="LoadPointsFromFile",
                coord_type="DEPTH",
                shift_height=False,
                use_color=True,
                load_dim=6,
                use_dim=[0, 1, 2, 3, 4, 5],
                backend_args=None,
            ),
            load_annotations_step,
            dict(type="GlobalAlignment", rotation_axis=2),
            # 100k points ran a 22 GB A10G out of memory on scenes with many boxes
            # (target assignment builds a points x boxes distance matrix).
            dict(type="PointSample", num_points=40000),
            dict(
                type="RandomFlip3D",
                sync_2d=False,
                flip_ratio_bev_horizontal=0.5,
                flip_ratio_bev_vertical=0.5,
            ),
            dict(
                type="GlobalRotScaleTrans",
                rot_range=[-0.087266, 0.087266],
                scale_ratio_range=[0.9, 1.1],
                translation_std=[0.1, 0.1, 0.1],
                shift_height=False,
            ),
            dict(type="NormalizePointsColor", color_mean=None),
            pack_step,
        ]
    else:
        mean_sizes = [DEFAULT_MEAN_SIZES.get(c, [1.0, 1.0, 1.0]) for c in classes]
        cfg.model.bbox_head.bbox_coder = dict(
            type="PartialBinBasedBBoxCoder",
            num_sizes=len(classes),
            num_dir_bins=12,
            with_rot=True,
            mean_sizes=mean_sizes,
        )
        # Simplified pipeline: xyz only (4 channels with shift_height), no masks
        cfg.train_dataloader.dataset.dataset.pipeline = [
            dict(
                type="LoadPointsFromFile",
                coord_type="DEPTH",
                load_dim=6,
                use_dim=[0, 1, 2],
                shift_height=True,
                backend_args=None,
            ),
            load_annotations_step,
            dict(type="PointSample", num_points=20000),
            dict(type="RandomFlip3D", flip_ratio_bev_horizontal=0.5),
            dict(
                type="GlobalRotScaleTrans",
                rot_range=[-0.087266, 0.087266],
                scale_ratio_range=[0.95, 1.05],
                shift_height=True,
            ),
            pack_step,
        ]

    # Checkpoint hook
    cfg.default_hooks.checkpoint = dict(
        type="CheckpointHook", interval=config.checkpoint_interval
    )

    logger.info("Config ready. Starting Runner...")

    # 5. Monkey-patch dataset METAINFO to accept our custom class names.
    # Det3DDataset.__init__ checks self.METAINFO['classes'].index(name) which
    # raises ValueError if custom class names aren't in the hardcoded tuple.
    # By patching the class-level METAINFO, any custom class name works.
    from mmdet3d.datasets import ScanNetDataset  # pyright: ignore[reportMissingImports]

    ScanNetDataset.METAINFO = patched_metainfo(ScanNetDataset.METAINFO, classes)
    logger.info("Patched ScanNetDataset.METAINFO: %d classes", len(classes))

    # 6. Run training
    runner = Runner.from_cfg(cfg)
    runner.train()

    # Export the weights the worker loads: the model's tensors and plain metadata,
    # which load weights-only with nothing allowlisted. mmengine's epoch_*.pth stay
    # beside it for resuming, but they also pickle the message hub (HistoryBuffer,
    # numpy objects, getattr), so they do not load weights-only.
    import torch  # pyright: ignore[reportMissingImports]
    from mmengine.runner.checkpoint import (  # pyright: ignore[reportMissingImports]
        get_state_dict,
        weights_to_cpu,
    )

    weights_path = Path(config.output_path) / "model.pth"
    torch.save(
        {
            "state_dict": weights_to_cpu(get_state_dict(runner.model)),
            "meta": {
                "dataset_meta": {"classes": list(classes)},
                "base_model": config.base_model,
            },
        },
        weights_path,
    )

    logger.info("Training complete! Weights: %s", weights_path)

    return {
        "checkpoint_path": str(weights_path),
        "epochs_completed": config.epochs,
        "class_names": classes,
        "train_scenes": len(train_data),
        "val_scenes": len(val_data),
        "num_converted": n_converted,
    }


if __name__ == "__main__":
    import argparse

    import yaml

    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Fine-tune with real MMDetection3D")
    parser.add_argument("--config", "-c", required=True, help="Training config YAML")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg_data = yaml.safe_load(f)
    training_config = TrainingConfig(**cfg_data)

    result = run_real_training(training_config)
    logger.info("Result: %s", json.dumps(result, indent=2))
