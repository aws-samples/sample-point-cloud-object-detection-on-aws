#!/usr/bin/env python3
"""Run inference with a fine-tuned VoteNet checkpoint.

This script handles the mmdet3d 1.4.0 + PyTorch 2.7 compatibility issues:
1. With --trust-checkpoint only, patches torch.load for weights_only=False
   (mmengine compat); without it, checkpoints load as plain weights only
2. Uses init_default_scope to register all mmdet3d modules
3. Sets up test_dataloader.dataset.pipeline so inference_detector works

Usage (on EC2 with GPU):
    python scripts/run_finetuned_inference.py \
        --checkpoint models/doors_livox/epoch_10.pth \
        --input tools/SUSTechPOINTS/data/livox_office_01/lidar/000000.pcd \
        --classes door --trust-checkpoint

    # Or with a .bin file (N x 6 float32: xyz + rgb):
    python scripts/run_finetuned_inference.py \
        --checkpoint models/doors_livox/epoch_10.pth \
        --input scene.bin \
        --classes door

    # Multiple classes:
    python scripts/run_finetuned_inference.py \
        --checkpoint models/doors_livox/epoch_10.pth \
        --input scene.bin \
        --classes door window
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


def read_pcd_to_bin(pcd_path: str) -> str:
    """Read binary PCD (xyzi format, 16 bytes/point) and save as .bin for mmdet3d."""
    with open(pcd_path, "rb") as f:
        n_points = 0
        while True:
            line = f.readline().decode("ascii", errors="ignore").strip()
            if line.startswith("POINTS"):
                n_points = int(line.split()[1])
            if line.startswith("DATA"):
                break

        if n_points == 0:
            print(f"ERROR: No points found in {pcd_path}")
            sys.exit(1)

        raw = f.read(n_points * 16)
        if len(raw) < n_points * 16:
            print(f"ERROR: Incomplete PCD data ({len(raw)} < {n_points * 16})")
            sys.exit(1)

    data = np.frombuffer(raw, dtype=np.float32).reshape(n_points, 4)
    xyz = data[:, :3]

    # Unpack packed RGB float -> separate channels
    rgb_packed = data[:, 3].view(np.uint32)
    r = ((rgb_packed >> 16) & 0xFF).astype(np.float32) / 255.0
    g = ((rgb_packed >> 8) & 0xFF).astype(np.float32) / 255.0
    b = (rgb_packed & 0xFF).astype(np.float32) / 255.0

    # Save as 6-dim: xyz + rgb
    points = np.column_stack([xyz, r, g, b]).astype(np.float32)

    tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    points.tofile(tmp.name)
    tmp.close()

    print(f"[io] Read {n_points} points from PCD -> {tmp.name}")
    return tmp.name


def main():
    parser = argparse.ArgumentParser(description="Inference with fine-tuned VoteNet")
    parser.add_argument(
        "--checkpoint", "-c", required=True, help="Path to .pth checkpoint"
    )
    parser.add_argument(
        "--input", "-i", required=True, help="Path to .pcd or .bin file"
    )
    parser.add_argument(
        "--classes", nargs="+", default=["door"], help="Class names (default: door)"
    )
    parser.add_argument("--score-thr", type=float, default=0.05, help="Score threshold")
    parser.add_argument(
        "--num-points", type=int, default=20000, help="Number of points to sample"
    )
    parser.add_argument("--device", default="cuda:0", help="Device (default: cuda:0)")
    parser.add_argument(
        "--output-json",
        "-o",
        default=None,
        help="Save detections as JSON for visualize_3d.py",
    )
    add_trust_checkpoint_argument(parser)
    args = parser.parse_args()

    # --- Step 1: Patch torch.load, only for a checkpoint the user trusts ---
    # mmengine's own loaders call torch.load with its default, so they only get
    # weights_only=False when the checkpoint is explicitly trusted.
    if args.trust_checkpoint:
        monkey_patch_torch_load()

    # --- Step 2: Register all mmdet3d modules ---
    from mmengine.registry import (  # pyright: ignore[reportMissingImports]
        init_default_scope,
    )

    init_default_scope("mmdet3d")
    print("[init] mmdet3d scope initialized (all modules registered)")

    from mmengine.registry import MODELS  # pyright: ignore[reportMissingImports]

    # Verify registration
    print(f"[check] VoteNet registered: {'VoteNet' in MODELS.module_dict}")

    # --- Step 3: Prepare model config overrides ---
    num_classes = len(args.classes)
    # Mean sizes per class (approximate door dimensions in meters)
    default_sizes = {"door": [1.0, 0.2, 2.1], "window": [1.2, 0.2, 1.2]}
    mean_sizes = [default_sizes.get(c, [1.0, 1.0, 1.0]) for c in args.classes]

    # --- Step 4: Build model using the STOCK config (proper test_cfg forwarding) ---
    # The key insight: VoteNet.__init__ forwards train_cfg/test_cfg to bbox_head
    # but only when instantiated via the full config chain. Using the stock config
    # file ensures proper initialization.
    import mmdet3d  # pyright: ignore[reportMissingImports]
    from mmengine.config import Config  # pyright: ignore[reportMissingImports]

    if mmdet3d.__file__ is None:
        raise RuntimeError(
            "mmdet3d resolved as a namespace package (__file__ is None), so its "
            "bundled configs cannot be located. Reinstall mmdet3d via mim."
        )
    configs_dir = Path(mmdet3d.__file__).parent / ".mim" / "configs"
    votenet_cfg_path = configs_dir / "votenet" / "votenet_8xb8_scannet-3d.py"

    print(f"[config] Loading stock config: {votenet_cfg_path}")
    cfg = Config.fromfile(str(votenet_cfg_path))

    # Override for our fine-tuned model
    cfg.model.bbox_head.num_classes = num_classes
    cfg.model.bbox_head.bbox_coder.num_sizes = num_classes
    cfg.model.bbox_head.bbox_coder.mean_sizes = mean_sizes
    cfg.model.bbox_head.bbox_coder.num_dir_bins = 12
    cfg.model.bbox_head.bbox_coder.with_rot = True
    cfg.model.test_cfg.score_thr = args.score_thr

    print("[model] Building VoteNet from stock config...")
    model = MODELS.build(cfg.model)
    model.eval()
    model.to(args.device)
    print("[model] Built successfully")

    # Load checkpoint
    print(f"[model] Loading checkpoint: {args.checkpoint}")
    checkpoint = load_checkpoint(args.checkpoint, args.device, args.trust_checkpoint)
    if "state_dict" in checkpoint:
        state_dict = checkpoint["state_dict"]
    else:
        state_dict = checkpoint
    model.load_state_dict(state_dict, strict=False)
    print("[model] Weights loaded")

    # Verify sample_mode is set
    print(
        f"[check] bbox_head.sample_mode = {getattr(model.bbox_head, 'sample_mode', 'MISSING')}"
    )

    # --- Step 5: Attach cfg with test_dataloader.dataset.pipeline ---
    test_pipeline = [
        dict(
            type="LoadPointsFromFile",
            coord_type="DEPTH",
            load_dim=6,
            use_dim=[0, 1, 2],
            shift_height=True,
            backend_args=None,
        ),
        dict(type="PointSample", num_points=args.num_points),
        dict(
            type="Pack3DDetInputs",
            keys=["points"],
        ),
    ]

    model.cfg = Config(
        dict(
            test_dataloader=dict(
                dataset=dict(
                    type="ScanNetDataset",
                    pipeline=test_pipeline,
                    box_type_3d="Depth",
                    data_root=".",
                    metainfo=dict(classes=args.classes),
                )
            ),
            test_pipeline=test_pipeline,
        )
    )
    print("[config] Test pipeline attached to model.cfg")

    # --- Step 6: Prepare input ---
    input_path = args.input
    cleanup_tmp = False

    if input_path.endswith(".pcd"):
        input_path = read_pcd_to_bin(input_path)
        cleanup_tmp = True
    elif not input_path.endswith(".bin"):
        print(f"WARNING: Unknown file extension, treating as .bin: {input_path}")

    # --- Step 7: Run inference ---
    print(f"[inference] Running on: {input_path}")
    from mmdet3d.apis import inference_detector  # pyright: ignore[reportMissingImports]

    result, _ = inference_detector(model, input_path)

    # --- Step 8: Parse results ---
    pred = result.pred_instances_3d
    bboxes = pred.bboxes_3d.tensor.cpu().numpy()
    scores = pred.scores_3d.cpu().numpy()
    labels = pred.labels_3d.cpu().numpy()

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {len(scores)} detections (before threshold filter)")
    print(f"{'=' * 60}")

    n_above = 0
    for i in range(len(scores)):
        if scores[i] >= args.score_thr:
            n_above += 1
            label_idx = int(labels[i])
            class_name = (
                args.classes[label_idx]
                if label_idx < len(args.classes)
                else f"class_{label_idx}"
            )
            bbox = bboxes[i]
            print(
                f"  [{n_above}] {class_name} conf={scores[i]:.4f} "
                f"center=({bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}) "
                f"size=({bbox[3]:.2f}, {bbox[4]:.2f}, {bbox[5]:.2f}) "
                f"yaw={bbox[6]:.3f}"
            )

    if n_above == 0:
        print("  (no detections above threshold)")
        print(f"\n  Top 5 scores: {sorted(scores, reverse=True)[:5]}")

    print(f"\nTotal above {args.score_thr}: {n_above}")

    # Export JSON for visualize_3d.py
    if args.output_json:
        import json as json_mod

        det_list = []
        for i in range(len(scores)):
            if scores[i] < args.score_thr:
                continue
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
            cls = (
                args.classes[label_idx]
                if label_idx < len(args.classes)
                else f"class_{label_idx}"
            )
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
        out_data = {"detections": det_list, "model": "votenet", "input": args.input}
        Path(args.output_json).write_text(json_mod.dumps(out_data, indent=2))
        print(f"\nSaved detections JSON: {args.output_json}")
        print(
            f"Visualize: python scripts/visualize_3d.py {args.input} {args.output_json}"
        )

    # Cleanup
    if cleanup_tmp:
        Path(input_path).unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
