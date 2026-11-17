"""Tests for scripts/convert-zenodo-craslab.py, the clean-model data converter.

The trainer reads each annotation's point_cloud_path as a raw float32 N x 6 .bin
and always holds out at least one scene for validation, so the converter must
write .bin files (not PLY) and more than one scene. These pin both.
"""

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import numpy as np

from pointcloud_detect.training.data_loader import split_train_val

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "convert_zenodo_craslab", ROOT / "scripts" / "convert-zenodo-craslab.py"
)
assert _spec and _spec.loader
conv = importlib.util.module_from_spec(_spec)
sys.modules["convert_zenodo_craslab"] = conv
_spec.loader.exec_module(conv)

CHAIR, DESK, WALL = 7, 6, 3


def _cube(center, size, n, label, rng):
    pts = rng.uniform(-0.5, 0.5, size=(n, 3)) * np.asarray(size) + np.asarray(center)
    rgb = rng.integers(0, 256, size=(n, 3))
    lab = np.full((n, 1), label)
    return np.hstack([pts, rgb, np.zeros((n, 1)), lab])


def _cloud(tmp_path: Path) -> Path:
    """Two 6 m blocks, one chair each, a desk in the first, a wall across both."""
    rng = np.random.default_rng(0)
    rows = np.vstack(
        [
            _cube([1.0, 1.0, 0.5], [0.5, 0.5, 1.0], 400, CHAIR, rng),
            _cube([3.0, 3.0, 0.4], [1.5, 0.8, 0.8], 600, DESK, rng),
            _cube([8.0, 1.0, 0.5], [0.5, 0.5, 1.0], 400, CHAIR, rng),
            _cube([6.0, 5.5, 1.5], [12.0, 0.1, 3.0], 3000, WALL, rng),
        ]
    )
    path = tmp_path / "cloud.txt"
    with open(path, "w") as f:
        f.write("//X Y Z R G B Intensity Label\n")
        f.write(f"{len(rows)}\n")
        for r in rows:
            f.write(
                f"{r[0]:.4f} {r[1]:.4f} {r[2]:.4f} "
                f"{int(r[3])} {int(r[4])} {int(r[5])} 0 {int(r[7])}\n"
            )
    return path


def test_writes_bin_scenes_the_trainer_can_split(tmp_path):
    out = tmp_path / "scenes"
    rc = conv.main(
        [
            "--input", str(_cloud(tmp_path)),
            "--out-dir", str(out),
            "--block-size", "6.0",
            "--min-block-points", "100",
            "--voxel", "0.1",
            "--min-points", "50",
        ]
    )  # fmt: skip
    assert rc == 0

    jsons = sorted(out.glob("*.json"))
    assert len(jsons) == 2
    anns = [json.loads(p.read_text()) for p in jsons]
    names = sorted(b["class_name"] for a in anns for b in a["annotations"])
    assert names == ["chair", "chair", "desk"]  # the wall is structure, skipped

    for ann in anns:
        assert ann["provenance"]["license"] == "CC BY 4.0"
        bin_path = out / ann["point_cloud_path"]
        assert bin_path.suffix == ".bin"
        pts = np.fromfile(bin_path, dtype=np.float32).reshape(-1, 6)
        assert len(pts) >= 100
        assert pts[:, 3:].max() > 1.0  # raw 0-255 RGB, not normalized

    train, val = split_train_val(anns, 0.2)
    assert train and val


def test_reads_the_zip_directly(tmp_path):
    cloud = _cloud(tmp_path)
    zpath = tmp_path / "craslabannotated.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(cloud, "craslab.txt")
    xyz, rgb, labels = conv.parse_cloud(zpath)
    assert xyz.shape == (4400, 3)
    assert rgb.dtype == np.uint8
    assert set(labels.tolist()) == {CHAIR, DESK, WALL}


def test_downsample_dedups_across_chunks(tmp_path):
    xyz, _, _ = conv.parse_cloud(_cloud(tmp_path), downsample_voxel=0.5, chunk_lines=7)
    keys = {tuple(k) for k in np.floor(xyz / 0.5).astype(int).tolist()}
    assert len(keys) == len(xyz)


def test_box_belongs_to_the_block_holding_its_center():
    xyz = np.array([[0.1, 0.1, 0.0], [5.9, 0.1, 0.0], [6.1, 0.1, 0.0]], np.float32)
    box = {"center_xyz": [5.8, 0.1, 0.0], "size_wlh": [1, 1, 1], "class_name": "desk"}
    blocks = conv.split_into_blocks(xyz, [box], block_size=6.0, min_block_points=1)
    assert [(key, len(idx)) for key, idx, _ in blocks] == [((0, 0), 2)]
