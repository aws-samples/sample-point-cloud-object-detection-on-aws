"""Visualize detection results as a bird's-eye view plot."""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np

LABEL_COLORS = {
    "car": "red",
    "truck": "orange",
    "bus": "darkorange",
    "pedestrian": "lime",
    "motorcycle": "blue",
    "bicycle": "cyan",
    "traffic cone": "yellow",
    "barrier": "magenta",
}


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: python visualize.py <point_cloud.bin> <detections.json> [output_dir]"
        )
        sys.exit(1)

    pc_path = Path(sys.argv[1])
    det_path = Path(sys.argv[2])
    out_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("./output")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load point cloud
    data = np.fromfile(pc_path, dtype=np.float32).reshape(-1, 4)
    xyz = data[:, :3]
    intensity = data[:, 3]

    # Load detections
    with open(det_path, encoding="utf-8") as f:
        result = json.load(f)
    detections = result["detections"]

    # Bird's-eye view (X-Y plane)
    fig, ax = plt.subplots(1, 1, figsize=(16, 12), facecolor="black")
    ax.set_facecolor("black")

    # Plot points colored by intensity
    ax.scatter(
        xyz[:, 0],
        xyz[:, 1],
        c=intensity,
        cmap="gray",
        s=0.1,
        alpha=0.6,
    )

    # Draw bounding boxes
    for det in detections:
        aabb = det["bbox_aabb"]
        mn = aabb["min"]
        mx = aabb["max"]
        w = mx[0] - mn[0]
        h = mx[1] - mn[1]
        color = LABEL_COLORS.get(det["label"], "white")

        rect = patches.Rectangle(
            (mn[0], mn[1]),
            w,
            h,
            linewidth=2,
            edgecolor=color,
            facecolor="none",
        )
        ax.add_patch(rect)
        ax.text(
            mn[0],
            mx[1] + 0.5,
            f"{det['label']} {det['confidence']:.2f}",
            color=color,
            fontsize=8,
            fontweight="bold",
        )

    ax.set_xlabel("X (m)", color="white")
    ax.set_ylabel("Y (m)", color="white")
    ax.set_title(
        f"Bird's Eye View — {pc_path.name} ({len(detections)} detections)",
        color="white",
    )
    ax.tick_params(colors="white")
    ax.set_aspect("equal")

    png_path = out_dir / f"{pc_path.stem}_bev.png"
    fig.savefig(png_path, dpi=150, bbox_inches="tight", facecolor="black")
    plt.close()

    print(f"Saved: {png_path}")
    print("Detections:")
    for det in detections:
        print(
            f"  - {det['label']} (conf: {det['confidence']:.2f}) at {det['bbox_obb']['center']}"
        )


if __name__ == "__main__":
    main()
