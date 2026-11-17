#!/usr/bin/env python3
"""Interactive 3D HTML report of detections; the code is pointcloud_detect.report3d.

    python scripts/visualize_3d.py <point_cloud> <detections.json> [output_dir] \\
        [--subsample N] [--coord-frame FRAME] [--seg segmentation.json]
"""

import sys
from pathlib import Path

# Runs from a checkout as well as from an environment the project is installed in.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pointcloud_detect.report3d import main  # noqa: E402

if __name__ == "__main__":
    main()
