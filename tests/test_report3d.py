"""The 3D HTML report, through the CLI and through scripts/visualize_3d.py."""

import json
import runpy
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

laspy = pytest.importorskip("laspy")

DETECTION = {
    "label": "door",
    "confidence": 0.9,
    "bbox_obb": {"center": [5, 5, 1], "size": [1, 0.2, 2], "heading": 0.0},
    "bbox_aabb": {"min": [4.5, 4.9, 0], "max": [5.5, 5.1, 2]},
}


@pytest.fixture
def scan(tmp_path: Path) -> tuple[Path, Path]:
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = [0.001, 0.001, 0.001]
    las = laspy.LasData(header)
    rng = np.random.default_rng(0)
    las.x, las.y, las.z = rng.random(500) * 10, rng.random(500) * 10, rng.random(500)
    las_path = tmp_path / "scan.las"
    las.write(str(las_path))
    det_path = tmp_path / "dets.json"
    det_path.write_text(json.dumps({"detections": [DETECTION]}))
    return las_path, det_path


def test_cli_writes_the_report_in_process(scan, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from pointcloud_detect.cli import app

    las_path, det_path = scan
    out = tmp_path / "out"
    with (
        mock.patch("webbrowser.open") as browser,
        mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")),
    ):
        result = CliRunner().invoke(
            app, ["visualize", str(las_path), str(det_path), "-o", str(out)]
        )
    assert result.exit_code == 0, result.output
    report = out / "scan_3d.html"
    assert "door" in report.read_text()
    browser.assert_called_once()


def test_script_wrapper_writes_the_same_report(
    scan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pointcloud_detect.report3d import main

    las_path, det_path = scan
    main(["visualize", str(las_path), str(det_path), str(tmp_path / "a")])
    script = Path(__file__).parent.parent / "scripts" / "visualize_3d.py"
    argv = [str(script), str(las_path), str(det_path), str(tmp_path / "b")]
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_path(str(script), run_name="__main__")
    a = (tmp_path / "a" / "scan_3d.html").read_text()
    assert a == (tmp_path / "b" / "scan_3d.html").read_text()
