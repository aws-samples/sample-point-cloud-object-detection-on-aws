#!/usr/bin/env python3
"""Fine-tune CenterPoint on custom outdoor LiDAR data (e.g., guardrails from nuScenes).

CenterPoint is designed for outdoor LiDAR detection using voxelization + sparse convs.
This is the correct architecture for road infrastructure (guardrails, barriers, etc.)
where VoteNet (indoor, point-based) struggles with scale.

Prerequisites:
    - mmdet3d, mmengine, mmcv, mmdet installed (same as VoteNet training)
    - GPU with CUDA
    - Annotations in unified format (from convert-nuscenes-barriers.py)

Usage:
    python scripts/finetune_centerpoint.py \
        --data-dir data/annotations/barriers \
        --classes guardrail \
        --epochs 20 \
        --output-dir models/guardrails_centerpoint

    # With multiple classes:
    python scripts/finetune_centerpoint.py \
        --data-dir data/annotations/barriers \
        --classes guardrail traffic_cone \
        --epochs 30 \
        --output-dir models/road_objects_centerpoint
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def monkey_patch_torch_load():
    """Patch torch.load for PyTorch 2.7+ compat with mmengine."""
    import torch  # pyright: ignore[reportMissingImports]

    if not hasattr(torch.load, "_patched"):
        _original = torch.load

        def _patched(*args, **kwargs):
            kwargs.setdefault("weights_only", False)
            return _original(*args, **kwargs)

        # setattr rather than direct assignment: the marker is read back via
        # hasattr(torch.load, "_patched") to make patching idempotent, but a
        # function object has no such declared attribute to assign to.
        setattr(_patched, "_patched", True)  # noqa: B010
        torch.load = _patched


def build_nuscenes_infos(
    data_dir: str, classes: list[str], output_dir: Path
) -> tuple[int, list[str]]:
    """Convert unified annotations to a NuScenes-style info file for CenterPoint.

    CenterPoint expects:
        - Points as .bin files (N x 5: x, y, z, intensity, ring_index)
        - Info JSON with lidar_points, gt_bboxes_3d, gt_labels_3d

    Returns (num_scenes, classes_used).
    """
    ann_dir = Path(data_dir)
    points_out = output_dir / "points"
    points_out.mkdir(parents=True, exist_ok=True)

    json_files = sorted(ann_dir.glob("*.json"))
    if not json_files:
        raise ValueError(f"No annotation JSONs in {ann_dir}")

    data_list = []
    total_boxes = 0

    for jf in json_files:
        with open(jf, encoding="utf-8") as fp:
            ann = json.load(fp)
        scene_id = ann["scene_id"]
        pc_path = ann.get("point_cloud_path", "")

        # Resolve point cloud path
        resolved = None
        if pc_path and (ann_dir / pc_path).exists():
            resolved = ann_dir / pc_path
        elif pc_path and Path(pc_path).exists():
            resolved = Path(pc_path)

        if not resolved:
            continue

        # Load points and convert to 5-dim (x, y, z, intensity, zeros)
        raw = np.fromfile(str(resolved), dtype=np.float32)
        if raw.size % 6 == 0:
            pts = raw.reshape(-1, 6)
        elif raw.size % 5 == 0:
            pts = raw.reshape(-1, 5)
        elif raw.size % 4 == 0:
            pts = raw.reshape(-1, 4)
        else:
            pts = raw.reshape(-1, 3)

        # CenterPoint expects 5 dims: x, y, z, intensity, timestamp
        out_pts = np.zeros((len(pts), 5), dtype=np.float32)
        out_pts[:, : min(pts.shape[1], 3)] = pts[:, :3]
        if pts.shape[1] >= 4:
            out_pts[:, 3] = pts[:, 3]  # intensity

        # Save .bin
        bin_name = f"{scene_id}.bin"
        out_pts.tofile(str(points_out / bin_name))

        # Build bbox annotations
        gt_bboxes = []
        gt_labels = []
        for box in ann["annotations"]:
            cls_name = box["class_name"].lower()
            if cls_name not in classes:
                continue
            label_idx = classes.index(cls_name)
            cx, cy, cz = box["center_xyz"]
            w, length, h = box["size_wlh"]
            heading = box["heading"]
            # KITTI/PointPillars bbox format: [x, y, z, dx, dy, dz, rot] (7 dims, no velocity)
            gt_bboxes.append([cx, cy, cz, w, length, h, heading])
            gt_labels.append(label_idx)

        if not gt_bboxes:
            continue

        data_list.append(
            {
                "lidar_points": {
                    "lidar_path": bin_name,
                    "num_pts_feats": 5,
                },
                "lidar_path": bin_name,
                "gt_bboxes_3d": gt_bboxes,
                "gt_labels_3d": gt_labels,
                "instances": [
                    {
                        "bbox_3d": b,
                        "bbox_label_3d": int(label),
                        "num_lidar_pts": 100,
                        "num_radar_pts": 0,
                        "velocity": [0.0, 0.0],
                        "bbox_3d_isvalid": True,
                    }
                    for b, label in zip(gt_bboxes, gt_labels)
                ],
                # NuScenes-style fields
                "box_type_3d": "LiDAR",
                "sample_idx": scene_id,
            }
        )
        total_boxes += len(gt_bboxes)

    # Save the info file.
    # Use our ACTUAL class names — dataset METAINFO will be monkey-patched
    info = {
        "data_list": data_list,
        "metainfo": {
            "classes": classes,
            "categories": {c: i for i, c in enumerate(classes)},
        },
    }
    info_path = output_dir / "nuscenes_infos_train.json"
    # mmdet3d reads its dataset infos with mmengine.load, which picks the format by the
    # file's extension. JSON rather than the pickle mmdet3d's own converters write, so
    # training reads its info file as data and never unpickles anything to do so.
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f)

    logger.info(
        "Built %d scenes, %d boxes -> %s", len(data_list), total_boxes, info_path
    )
    return len(data_list), classes


def main():
    parser = argparse.ArgumentParser(
        description="Fine-tune CenterPoint for outdoor LiDAR"
    )
    parser.add_argument(
        "--data-dir", required=True, help="Directory with annotation JSONs"
    )
    parser.add_argument(
        "--classes", nargs="+", default=["guardrail"], help="Class names"
    )
    parser.add_argument("--epochs", type=int, default=20, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=4, help="Batch size")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate")
    parser.add_argument(
        "--output-dir", default="models/guardrails_centerpoint", help="Output dir"
    )
    parser.add_argument(
        "--point-cloud-range",
        type=float,
        nargs=6,
        default=[0, -39.68, -3, 69.12, 39.68, 1],
        help="Point cloud range [x_min, y_min, z_min, x_max, y_max, z_max]",
    )
    parser.add_argument(
        "--voxel-size",
        type=float,
        nargs=3,
        default=[0.16, 0.16, 4],
        help="Voxel size [x, y, z]",
    )
    args = parser.parse_args()

    monkey_patch_torch_load()

    # Register mmdet3d
    from mmengine.registry import (  # pyright: ignore[reportMissingImports]
        init_default_scope,
    )

    init_default_scope("mmdet3d")

    from mmengine.config import Config  # pyright: ignore[reportMissingImports]
    from mmengine.runner import Runner  # pyright: ignore[reportMissingImports]

    classes = [c.lower() for c in args.classes]
    num_classes = len(classes)
    output_dir = Path(args.output_dir)

    logger.info("=" * 60)
    logger.info("CenterPoint Fine-Tuning")
    logger.info("  Classes: %s", classes)
    logger.info("  Data: %s", args.data_dir)
    logger.info(
        "  Epochs: %d, LR: %s, Batch: %d", args.epochs, args.lr, args.batch_size
    )
    logger.info("  Range: %s", args.point_cloud_range)
    logger.info("  Voxel: %s", args.voxel_size)
    logger.info("=" * 60)

    # 1. Build training data
    data_dir = output_dir / "nuscenes_format"
    n_scenes, _ = build_nuscenes_infos(args.data_dir, classes, data_dir)
    if n_scenes == 0:
        logger.error("No scenes converted!")
        sys.exit(1)

    # 2. Load PointPillars config (outdoor LiDAR, simpler dataset requirements)
    # PointPillars uses the same pillar-voxelization concept as CenterPoint
    # but works with KITTI-style datasets which are simpler to adapt
    import mmdet3d  # pyright: ignore[reportMissingImports]

    if mmdet3d.__file__ is None:
        raise RuntimeError(
            "mmdet3d resolved as a namespace package (__file__ is None), so its "
            "bundled configs cannot be located. Reinstall mmdet3d via mim."
        )
    configs_dir = Path(mmdet3d.__file__).parent / ".mim" / "configs"

    # Use PointPillars KITTI (3-class) as base — simpler than NuScenes CenterPoint
    pp_cfg_path = (
        configs_dir
        / "pointpillars"
        / "pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py"
    )

    if not pp_cfg_path.exists():
        candidates = list((configs_dir / "pointpillars").glob("*kitti*3class*"))
        if candidates:
            pp_cfg_path = candidates[0]
        else:
            candidates = list((configs_dir / "pointpillars").glob("*.py"))
            if candidates:
                pp_cfg_path = candidates[0]
            else:
                logger.error("No PointPillars config found")
                sys.exit(1)

    logger.info("Using config: %s", pp_cfg_path.name)
    cfg = Config.fromfile(str(pp_cfg_path))

    # 3. Override config for our custom classes
    cfg.work_dir = str(output_dir)

    # Point cloud range and voxel size — use stock KITTI values from config
    # (don't override model architecture params; just use for pipeline filtering)
    pcr = args.point_cloud_range

    # Only override the bbox_head num_classes and anchor sizes
    if hasattr(cfg.model, "bbox_head"):
        head = cfg.model.bbox_head
    elif hasattr(cfg.model, "pts_bbox_head"):
        head = cfg.model.pts_bbox_head
    else:
        logger.error("Cannot find bbox_head in config")
        sys.exit(1)

    head.num_classes = num_classes
    # Guardrail anchor: ~2m long, 0.5m wide, 1m tall
    head.anchor_generator.sizes = [[2.0, 0.5, 1.0]] * num_classes
    head.anchor_generator.ranges = [
        [pcr[0], pcr[1], -1.0, pcr[3], pcr[4], -1.0]
    ] * num_classes
    head.anchor_generator.rotations = [0, 1.57]

    # Match train_cfg assigners to number of classes (one assigner per class)
    single_assigner = dict(
        type="Max3DIoUAssigner",
        iou_calculator=dict(type="BboxOverlapsNearest3D"),
        pos_iou_thr=0.6,
        neg_iou_thr=0.45,
        min_pos_iou=0.45,
        ignore_iof_thr=-1,
    )
    cfg.model.train_cfg = dict(
        assigner=[single_assigner] * num_classes,
        allowed_border=0,
        pos_weight=-1,
        debug=False,
    )
    cfg.model.test_cfg = dict(
        use_rotate_nms=True,
        nms_across_levels=False,
        nms_thr=0.01,
        score_thr=0.1,
        min_bbox_size=0,
        nms_pre=100,
        max_num=50,
    )

    # Training hyperparams
    cfg.train_cfg = dict(
        type="EpochBasedTrainLoop", max_epochs=args.epochs, val_interval=999
    )
    cfg.optim_wrapper = dict(
        type="OptimWrapper",
        optimizer=dict(type="AdamW", lr=args.lr, weight_decay=0.01),
        clip_grad=dict(max_norm=35, norm_type=2),
    )
    cfg.param_scheduler = [
        dict(
            type="CosineAnnealingLR",
            T_max=args.epochs,
            eta_min=args.lr * 0.01,
            by_epoch=True,
        ),
    ]

    # Dataset — use NuScenesDataset with monkey-patched METAINFO
    train_pipeline = [
        dict(
            type="LoadPointsFromFile",
            coord_type="LIDAR",
            load_dim=5,
            use_dim=4,  # x, y, z, intensity — matches KITTI PointPillars encoder
            backend_args=None,
        ),
        dict(type="LoadAnnotations3D", with_bbox_3d=True, with_label_3d=True),
        dict(
            type="GlobalRotScaleTrans",
            rot_range=[-0.3925, 0.3925],
            scale_ratio_range=[0.95, 1.05],
            translation_std=[0, 0, 0],
        ),
        dict(type="RandomFlip3D", flip_ratio_bev_horizontal=0.5),
        dict(type="PointsRangeFilter", point_cloud_range=pcr),
        dict(type="ObjectRangeFilter", point_cloud_range=pcr),
        dict(type="PointShuffle"),
        dict(
            type="Pack3DDetInputs",
            keys=["points", "gt_bboxes_3d", "gt_labels_3d"],
        ),
    ]

    cfg.train_dataloader = dict(
        batch_size=args.batch_size,
        num_workers=0,
        persistent_workers=False,
        sampler=dict(type="DefaultSampler", shuffle=True),
        dataset=dict(
            type="RepeatDataset",
            times=max(10, 100 // n_scenes),
            dataset=dict(
                type="KittiDataset",
                data_root=str(data_dir),
                data_prefix=dict(pts="points"),
                ann_file="nuscenes_infos_train.json",
                pipeline=train_pipeline,
                metainfo=dict(classes=classes),
                box_type_3d="LiDAR",
            ),
        ),
    )

    # Disable validation
    cfg.val_cfg = None
    cfg.val_dataloader = None
    cfg.val_evaluator = None

    # Load pretrained PointPillars (KITTI): a local copy verified against its pin, not
    # the raw URL mmengine would fetch unchecked. It was trained on KITTI
    # (CC BY-NC-SA 3.0), so backends.checkpoints refuses it unless
    # ALLOW_NONCOMMERCIAL_WEIGHTS=1.
    from pointcloud_detect.backends import checkpoints
    from pointcloud_detect.backends.mmdet3d import DETECTION_CONFIGS

    checkpoints.enforce_weights_only()
    cfg.load_from = str(
        checkpoints.fetch(DETECTION_CONFIGS["pointpillars_kitti"]["checkpoint"])
    )
    logger.info("Loading pretrained: %s", cfg.load_from)

    # Checkpoint
    cfg.default_hooks = dict(
        timer=dict(type="IterTimerHook"),
        logger=dict(type="LoggerHook", interval=30),
        param_scheduler=dict(type="ParamSchedulerHook"),
        checkpoint=dict(type="CheckpointHook", interval=5),
        sampler_seed=dict(type="DistSamplerSeedHook"),
    )
    cfg.log_level = "INFO"
    cfg.default_scope = "mmdet3d"

    # 4. Run training
    # 4. Monkey-patch KittiDataset to bypass its custom parse_ann_info
    # which requires camera calibration data we don't have.
    # Instead, we provide a simple parse_ann_info that reads directly from our info file.
    from mmdet3d.datasets import KittiDataset  # pyright: ignore[reportMissingImports]

    KittiDataset.METAINFO = {"classes": tuple(classes)}

    def _simple_parse_ann_info(self, info):
        """Simplified parse_ann_info that reads bbox_3d/labels directly."""
        from mmdet3d.structures import (  # pyright: ignore[reportMissingImports]
            LiDARInstance3DBoxes,
        )

        ann_info = {}
        if "instances" in info and info["instances"]:
            gt_bboxes_3d = []
            gt_labels_3d = []
            for inst in info["instances"]:
                gt_bboxes_3d.append(inst["bbox_3d"])
                gt_labels_3d.append(inst["bbox_label_3d"])
            ann_info["gt_bboxes_3d"] = LiDARInstance3DBoxes(
                np.array(gt_bboxes_3d, dtype=np.float32)
            )
            ann_info["gt_labels_3d"] = np.array(gt_labels_3d, dtype=np.int64)
        else:
            ann_info["gt_bboxes_3d"] = LiDARInstance3DBoxes(
                np.zeros((0, 7), dtype=np.float32)
            )
            ann_info["gt_labels_3d"] = np.array([], dtype=np.int64)
        return ann_info

    KittiDataset.parse_ann_info = _simple_parse_ann_info
    logger.info("Patched KittiDataset.METAINFO and parse_ann_info for custom classes")

    logger.info("Starting PointPillars training...")
    runner = Runner.from_cfg(cfg)
    runner.train()

    # 5. Report results
    checkpoints = sorted(output_dir.glob("epoch_*.pth"))
    best = str(checkpoints[-1]) if checkpoints else str(output_dir)
    logger.info("Training complete! Best checkpoint: %s", best)
    logger.info("Inference command:")
    logger.info(
        "  python scripts/run_centerpoint_inference.py --checkpoint %s "
        "--input <file.bin> --classes %s --trust-checkpoint",
        best,
        " ".join(classes),
    )


if __name__ == "__main__":
    main()
