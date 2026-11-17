"""Tests for dataset parsing utilities."""

from pathlib import Path

import numpy as np
import pytest

from pointcloud_detect.data.parse import (
    _compute_bbox_from_points,
    parse_kitti_label,
    parse_kitti_scene,
    parse_s3dis_room,
    parse_scannet_bbox,
)


@pytest.fixture
def tmp_data(tmp_path: Path) -> Path:
    return tmp_path


class TestKITTIParsing:
    def test_parse_label_basic(self, tmp_data: Path) -> None:
        label_file = tmp_data / "000001.txt"
        label_file.write_text(
            "Car 0.00 0 -1.57 600 170 700 300 1.50 1.60 4.20 "
            "-2.00 1.80 10.00 -1.57\n"
            "Pedestrian 0.00 0 0.50 800 200 850 400 1.70 0.60 0.80 "
            "3.00 1.50 15.00 0.30\n"
            "DontCare -1 -1 -10 0 0 0 0 -1 -1 -1 -1000 -1000 -1000 -10\n"
        )

        annotations = parse_kitti_label(label_file)
        assert len(annotations) == 2

        car = annotations[0]
        assert car["class_name"] == "car"
        assert len(car["center_xyz"]) == 3
        assert len(car["size_wlh"]) == 3
        assert isinstance(car["heading"], float)

        # Verify coordinate transform: x_lidar = z_cam
        assert car["center_xyz"][0] == pytest.approx(10.0)
        # y_lidar = -x_cam
        assert car["center_xyz"][1] == pytest.approx(2.0)

        ped = annotations[1]
        assert ped["class_name"] == "pedestrian"

    def test_parse_label_dontcare_skipped(self, tmp_data: Path) -> None:
        label_file = tmp_data / "000002.txt"
        label_file.write_text(
            "DontCare -1 -1 -10 0 0 0 0 -1 -1 -1 -1000 -1000 -1000 -10\n"
        )

        annotations = parse_kitti_label(label_file)
        assert len(annotations) == 0

    def test_parse_label_empty_file(self, tmp_data: Path) -> None:
        label_file = tmp_data / "000003.txt"
        label_file.write_text("")

        annotations = parse_kitti_label(label_file)
        assert len(annotations) == 0

    def test_parse_scene(self, tmp_data: Path) -> None:
        velodyne = tmp_data / "000001.bin"
        velodyne.write_bytes(b"\x00" * 64)

        label = tmp_data / "000001.txt"
        label.write_text(
            "Car 0.00 0 -1.57 600 170 700 300 1.50 1.60 4.20 -2.00 1.80 10.00 -1.57\n"
        )

        result = parse_kitti_scene(velodyne, label)
        assert result["scene_id"] == "000001"
        assert result["num_annotations"] == 1
        assert result["point_cloud_path"] == str(velodyne)


class TestScanNetParsing:
    def test_parse_bbox_basic(self, tmp_data: Path) -> None:
        bboxes = np.array(
            [
                [1.0, 2.0, 0.5, 0.8, 0.8, 1.2, 2],  # class 2 = chair
                [3.0, 4.0, 1.0, 1.5, 2.0, 0.8, 4],  # class 4 = table
            ]
        )
        bbox_path = tmp_data / "scene0000_00_bbox.npy"
        np.save(bbox_path, bboxes)

        annotations = parse_scannet_bbox(bbox_path)
        assert len(annotations) == 2

        chair = annotations[0]
        assert chair["class_name"] == "chair"
        assert chair["center_xyz"] == [1.0, 2.0, 0.5]
        assert chair["size_wlh"] == [0.8, 0.8, 1.2]
        assert chair["heading"] == 0.0

        table = annotations[1]
        assert table["class_name"] == "table"

    def test_parse_bbox_missing_file(self, tmp_data: Path) -> None:
        bbox_path = tmp_data / "nonexistent_bbox.npy"
        annotations = parse_scannet_bbox(bbox_path)
        assert len(annotations) == 0

    def test_parse_bbox_invalid_shape(self, tmp_data: Path) -> None:
        bboxes = np.array([1.0, 2.0, 3.0])
        bbox_path = tmp_data / "bad_bbox.npy"
        np.save(bbox_path, bboxes)

        annotations = parse_scannet_bbox(bbox_path)
        assert len(annotations) == 0


class TestS3DISParsing:
    def test_parse_room_basic(self, tmp_data: Path) -> None:
        room_dir = tmp_data / "office_1"
        ann_dir = room_dir / "Annotations"
        ann_dir.mkdir(parents=True)

        # Create a chair instance with 10 points in a box
        rng = np.random.default_rng(42)
        chair_points = rng.uniform([1.0, 2.0, 0.0], [1.5, 2.5, 0.9], size=(10, 3))
        rgb = rng.integers(0, 255, size=(10, 3))
        data = np.hstack([chair_points, rgb])
        np.savetxt(ann_dir / "chair_1.txt", data)

        # Create a wall instance
        wall_points = rng.uniform([0.0, 0.0, 0.0], [0.1, 5.0, 3.0], size=(20, 3))
        rgb2 = rng.integers(0, 255, size=(20, 3))
        data2 = np.hstack([wall_points, rgb2])
        np.savetxt(ann_dir / "wall_1.txt", data2)

        annotations = parse_s3dis_room(room_dir)
        assert len(annotations) == 2

        class_names = {a["class_name"] for a in annotations}
        assert "chair" in class_names
        assert "wall" in class_names

        for ann in annotations:
            assert len(ann["center_xyz"]) == 3
            assert len(ann["size_wlh"]) == 3
            assert all(s > 0 for s in ann["size_wlh"])

    def test_parse_room_no_annotations_dir(self, tmp_data: Path) -> None:
        room_dir = tmp_data / "empty_room"
        room_dir.mkdir()

        annotations = parse_s3dis_room(room_dir)
        assert len(annotations) == 0

    def test_parse_room_unknown_class_skipped(self, tmp_data: Path) -> None:
        room_dir = tmp_data / "office_2"
        ann_dir = room_dir / "Annotations"
        ann_dir.mkdir(parents=True)

        rng = np.random.default_rng(0)
        points = rng.uniform(size=(10, 6))
        np.savetxt(ann_dir / "unknown_class_1.txt", points)

        annotations = parse_s3dis_room(room_dir)
        assert len(annotations) == 0


class TestBBoxComputation:
    def test_compute_bbox(self) -> None:
        points = np.array([[0.0, 0.0, 0.0], [2.0, 4.0, 6.0], [1.0, 2.0, 3.0]])
        center, size = _compute_bbox_from_points(points)
        assert center == pytest.approx([1.0, 2.0, 3.0])
        assert size == pytest.approx([2.0, 4.0, 6.0])


class TestDatasetArchives:
    """T42: dataset archives are extracted with the tar data filter."""

    @staticmethod
    def _tgz(path: Path, members: dict[str, bytes]) -> None:
        import io
        import tarfile

        with tarfile.open(path, "w:gz") as tf:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    def test_benign_archive_extracts(self, tmp_path: Path) -> None:
        from pointcloud_detect.data.download import download_nuscenes_mini

        root = tmp_path / "nuscenes"
        root.mkdir()
        self._tgz(root / "v1.0-mini.tgz", {"samples/LIDAR_TOP/a.bin": b"\0" * 20})
        download_nuscenes_mini(tmp_path)
        assert (root / "samples" / "LIDAR_TOP" / "a.bin").exists()

    def test_member_outside_root_is_refused(self, tmp_path: Path) -> None:
        import tarfile

        from pointcloud_detect.data.download import download_nuscenes_mini

        root = tmp_path / "nuscenes"
        root.mkdir()
        self._tgz(root / "v1.0-mini.tgz", {"../escaped.txt": b"pwned"})
        with pytest.raises(tarfile.FilterError):
            download_nuscenes_mini(tmp_path)
        assert not (tmp_path / "escaped.txt").exists()

    def test_absolute_member_lands_inside_root(self, tmp_path: Path) -> None:
        # The data filter strips the leading "/" rather than writing to it.
        from pointcloud_detect.data.download import download_nuscenes_mini

        root = tmp_path / "nuscenes"
        root.mkdir()
        target = tmp_path / "absolute-escape.txt"
        self._tgz(root / "v1.0-mini.tgz", {str(target): b"pwned"})
        download_nuscenes_mini(tmp_path)
        assert not target.exists()
        assert (root / str(target).lstrip("/")).read_bytes() == b"pwned"

    def test_link_leaving_root_is_refused(self, tmp_path: Path) -> None:
        import tarfile

        from pointcloud_detect.data.download import download_nuscenes_mini

        root = tmp_path / "nuscenes"
        root.mkdir()
        with tarfile.open(root / "v1.0-mini.tgz", "w:gz") as tf:
            link = tarfile.TarInfo("samples/evil")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../../outside"
            tf.addfile(link)
        with pytest.raises(tarfile.FilterError):
            download_nuscenes_mini(tmp_path)
        assert not (root / "samples" / "evil").is_symlink()

    def test_kitti_archive_that_does_not_match_its_pin_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import zipfile

        from pointcloud_detect.data import download

        root = tmp_path / "kitti"
        root.mkdir()
        with zipfile.ZipFile(root / "data_object_calib.zip", "w") as zf:
            zf.writestr("training/calib/000000.txt", "substituted")
        monkeypatch.setitem(
            download.KITTI_ARCHIVES, "calib", ("data_object_calib.zip", "0" * 64)
        )
        with pytest.raises(ValueError, match="refusing to extract"):
            download.download_kitti(tmp_path)
        assert not (root / "training").exists()

    def test_kitti_archive_that_matches_its_pin_is_extracted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib
        import zipfile

        from pointcloud_detect.data import download

        root = tmp_path / "kitti"
        root.mkdir()
        archive = root / "data_object_calib.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("training/calib/000000.txt", "P0: 1 0 0")
        pin = hashlib.sha256(archive.read_bytes()).hexdigest()
        monkeypatch.setattr(
            download, "KITTI_ARCHIVES", {"calib": ("data_object_calib.zip", pin)}
        )
        download.download_kitti(tmp_path)
        assert (root / "training" / "calib" / "000000.txt").read_text() == "P0: 1 0 0"

    def test_missing_kitti_archives_print_the_registration_steps(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pointcloud_detect.data import download

        download.download_kitti(tmp_path)
        out = capsys.readouterr().out
        assert "user_login.php" in out
        for filename, _ in download.KITTI_ARCHIVES.values():
            assert filename in out
        assert not any((tmp_path / "kitti").glob("*.zip"))

    def test_kitti_pins_are_sha256_hex_digests(self) -> None:
        import re

        from pointcloud_detect.data.download import KITTI_ARCHIVES

        for filename, sha in KITTI_ARCHIVES.values():
            assert filename.endswith(".zip")
            assert re.fullmatch(r"[0-9a-f]{64}", sha), filename
