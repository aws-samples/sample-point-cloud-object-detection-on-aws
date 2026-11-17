#!/usr/bin/env python3
"""Run inference with a fine-tuned CenterPoint checkpoint.

CenterPoint is designed for outdoor LiDAR (voxel-based BEV detection).
Use this for road infrastructure: guardrails, barriers, traffic cones, etc.

Usage:
    python scripts/run_centerpoint_inference.py \
        --checkpoint models/guardrails_centerpoint/epoch_20.pth \
        --input data/annotations/barriers/points/nuscenes_scene-0061_frame_00.bin \
        --classes guardrail --trust-checkpoint

    # With custom point cloud range:
    python scripts/run_centerpoint_inference.py \
        --checkpoint models/guardrails_centerpoint/epoch_20.pth \
        --input scene.bin \
        --classes guardrail \
        --point-cloud-range -51.2 -51.2 -5.0 51.2 51.2 3.0
"""

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np

from pointcloud_detect.backends.user_checkpoints import (
    add_trust_checkpoint_argument,
    load_checkpoint,
    monkey_patch_torch_load,
)


def main():
    parser = argparse.ArgumentParser(description="CenterPoint inference")
    parser.add_argument("--checkpoint", "-c", required=True, help="Path to .pth")
    parser.add_argument(
        "--input", "-i", required=True, help="Path to .bin file (N x 5 float32)"
    )
    parser.add_argument(
        "--classes", nargs="+", default=["guardrail"], help="Class names"
    )
    parser.add_argument("--score-thr", type=float, default=0.1, help="Score threshold")
    parser.add_argument("--device", default="cuda:0", help="Device")
    parser.add_argument(
        "--output-json",
        "-o",
        default=None,
        help="Save detections as JSON for visualize_3d.py",
    )
    parser.add_argument(
        "--point-cloud-range",
        type=float,
        nargs=6,
        default=[0, -39.68, -3, 69.12, 39.68, 1],
        help="Point cloud range",
    )
    parser.add_argument(
        "--voxel-size", type=float, nargs=3, default=[0.16, 0.16, 4], help="Voxel size"
    )
    add_trust_checkpoint_argument(parser)
    args = parser.parse_args()

    # mmengine's own loaders call torch.load with its default, so they only get
    # weights_only=False when the checkpoint is explicitly trusted.
    if args.trust_checkpoint:
        monkey_patch_torch_load()

    from mmengine.registry import (  # pyright: ignore[reportMissingImports]
        init_default_scope,
    )

    init_default_scope("mmdet3d")

    from mmdet3d.apis import inference_detector  # pyright: ignore[reportMissingImports]
    from mmengine.config import Config  # pyright: ignore[reportMissingImports]
    from mmengine.registry import MODELS  # pyright: ignore[reportMissingImports]

    num_classes = len(args.classes)
    pcr = args.point_cloud_range

    # Load PointPillars KITTI config (matches training)
    import mmdet3d  # pyright: ignore[reportMissingImports]

    if mmdet3d.__file__ is None:
        raise RuntimeError(
            "mmdet3d resolved as a namespace package (__file__ is None), so its "
            "bundled configs cannot be located. Reinstall mmdet3d via mim."
        )
    configs_dir = Path(mmdet3d.__file__).parent / ".mim" / "configs"
    pp_cfg_path = (
        configs_dir
        / "pointpillars"
        / "pointpillars_hv_secfpn_8xb6-160e_kitti-3d-3class.py"
    )

    if not pp_cfg_path.exists():
        candidates = list((configs_dir / "pointpillars").glob("*kitti*3class*"))
        pp_cfg_path = candidates[0] if candidates else None
        if not pp_cfg_path:
            print("ERROR: No PointPillars config found")
            sys.exit(1)

    print(f"[config] Loading: {pp_cfg_path.name}")
    cfg = Config.fromfile(str(pp_cfg_path))

    # Override only bbox_head for our classes (minimal changes)
    pcr = args.point_cloud_range

    if hasattr(cfg.model, "bbox_head"):
        head = cfg.model.bbox_head
    elif hasattr(cfg.model, "pts_bbox_head"):
        head = cfg.model.pts_bbox_head
    else:
        print("ERROR: No bbox_head found")
        sys.exit(1)

    head.num_classes = num_classes
    head.anchor_generator.sizes = [[2.0, 0.5, 1.0]] * num_classes
    head.anchor_generator.ranges = [
        [pcr[0], pcr[1], -1.0, pcr[3], pcr[4], -1.0]
    ] * num_classes
    head.anchor_generator.rotations = [0, 1.57]

    # Match train_cfg/test_cfg
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
        score_thr=args.score_thr,
        min_bbox_size=0,
        nms_pre=100,
        max_num=50,
    )

    # Build model
    print("[model] Building PointPillars...")
    model = MODELS.build(cfg.model)
    model.eval()
    model.to(args.device)
    print("[model] Built")

    # Load checkpoint
    print(f"[model] Loading: {args.checkpoint}")
    ckpt = load_checkpoint(args.checkpoint, args.device, args.trust_checkpoint)
    state_dict = ckpt.get("state_dict", ckpt)
    model.load_state_dict(state_dict, strict=False)
    print("[model] Loaded")

    # Set up test pipeline on model.cfg
    test_pipeline = [
        dict(
            type="LoadPointsFromFile",
            coord_type="LIDAR",
            load_dim=5,
            use_dim=4,
            backend_args=None,
        ),
        dict(type="PointsRangeFilter", point_cloud_range=pcr),
        dict(type="Pack3DDetInputs", keys=["points"]),
    ]

    model.cfg = Config(
        dict(
            test_dataloader=dict(
                dataset=dict(
                    type="KittiDataset",
                    pipeline=test_pipeline,
                    box_type_3d="LiDAR",
                    metainfo=dict(classes=args.classes),
                )
            ),
            test_pipeline=test_pipeline,
        )
    )

    # Prepare input
    input_path = args.input
    if not input_path.endswith(".bin"):
        print(f"WARNING: Expected .bin file, got: {input_path}")

    # Ensure input is 5-dim
    raw = np.fromfile(input_path, dtype=np.float32)
    if raw.size % 5 != 0:
        # Reshape to whatever works, pad to 5
        if raw.size % 6 == 0:
            pts = raw.reshape(-1, 6)[:, :5]
        elif raw.size % 4 == 0:
            pts = raw.reshape(-1, 4)
            pts = np.column_stack([pts, np.zeros(len(pts), dtype=np.float32)])
        elif raw.size % 3 == 0:
            pts = raw.reshape(-1, 3)
            pts = np.column_stack([pts, np.zeros((len(pts), 2), dtype=np.float32)])
        else:
            pts = raw.reshape(-1, 5)

        tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        pts.astype(np.float32).tofile(tmp.name)
        tmp.close()
        input_path = tmp.name
        print(f"[io] Converted to 5-dim: {len(pts)} points -> {input_path}")

    # Run inference
    print(f"[inference] Running on: {input_path}")
    result, _ = inference_detector(model, input_path)

    # Parse results
    pred = result.pred_instances_3d
    bboxes = pred.bboxes_3d.tensor.cpu().numpy()
    scores = pred.scores_3d.cpu().numpy()
    labels = pred.labels_3d.cpu().numpy()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {len(scores)} detections")
    print(f"{'=' * 60}")

    n_above = 0
    for i in range(len(scores)):
        if scores[i] >= args.score_thr:
            n_above += 1
            label_idx = int(labels[i])
            cls = (
                args.classes[label_idx]
                if label_idx < len(args.classes)
                else f"class_{label_idx}"
            )
            b = bboxes[i]
            print(
                f"  [{n_above}] {cls} conf={scores[i]:.4f} "
                f"center=({b[0]:.2f}, {b[1]:.2f}, {b[2]:.2f}) "
                f"size=({b[3]:.2f}, {b[4]:.2f}, {b[5]:.2f}) yaw={b[6]:.3f}"
            )

    if n_above == 0:
        print("  (no detections above threshold)")
        if len(scores) > 0:
            print(f"  Top 5 scores: {sorted(scores, reverse=True)[:5]}")

    print(f"\nTotal above {args.score_thr}: {n_above}")

    # Export JSON for visualize_3d.py
    if args.output_json:
        import json as json_mod

        det_list = []
        for i in range(len(scores)):
            if scores[i] < args.score_thr:
                continue
            b = bboxes[i]
            cx, cy, cz, dx, dy, dz, yaw = b[0], b[1], b[2], b[3], b[4], b[5], b[6]
            label_idx = int(labels[i])
            cls = (
                args.classes[label_idx]
                if label_idx < len(args.classes)
                else f"class_{label_idx}"
            )
            # Compute AABB from OBB center + size (approximate, ignoring rotation)
            half = np.array([dx / 2, dy / 2, dz / 2])
            det_list.append(
                {
                    "label": cls,
                    "confidence": round(float(scores[i]), 4),
                    "bbox_obb": {
                        "center": [
                            round(float(cx), 3),
                            round(float(cy), 3),
                            round(float(cz), 3),
                        ],
                        "size": [
                            round(float(dx), 3),
                            round(float(dy), 3),
                            round(float(dz), 3),
                        ],
                        "heading": round(float(yaw), 4),
                    },
                    "bbox_aabb": {
                        "min": [
                            round(float(cx - half[0]), 3),
                            round(float(cy - half[1]), 3),
                            round(float(cz - half[2]), 3),
                        ],
                        "max": [
                            round(float(cx + half[0]), 3),
                            round(float(cy + half[1]), 3),
                            round(float(cz + half[2]), 3),
                        ],
                    },
                }
            )
        out_data = {
            "detections": det_list,
            "model": "pointpillars",
            "input": args.input,
        }
        Path(args.output_json).write_text(json_mod.dumps(out_data, indent=2))
        print(f"\nSaved detections JSON: {args.output_json}")
        print(
            f"Visualize: python scripts/visualize_3d.py {args.input} {args.output_json}"
        )


if __name__ == "__main__":
    sys.exit(main() or 0)
