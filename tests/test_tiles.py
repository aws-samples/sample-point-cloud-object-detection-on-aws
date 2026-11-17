"""Unit tests for LOD tile generation."""

import numpy as np

from pointcloud_detect.models import PointCloud
from pointcloud_detect.tiles.generator import (
    _determine_point_format,
    _pack_points,
    _subdivide_octree,
)


class TestPointFormat:
    def test_xyz_only(self) -> None:
        cloud = PointCloud(xyz=np.random.rand(100, 3))
        assert _determine_point_format(cloud) == "xyz"

    def test_xyzrgb(self) -> None:
        cloud = PointCloud(
            xyz=np.random.rand(100, 3),
            rgb=np.random.randint(0, 255, (100, 3), dtype=np.uint8),
        )
        assert _determine_point_format(cloud) == "xyzrgb"

    def test_xyzrgbi(self) -> None:
        cloud = PointCloud(
            xyz=np.random.rand(100, 3),
            rgb=np.random.randint(0, 255, (100, 3), dtype=np.uint8),
            intensity=np.random.rand(100).astype(np.float32),
        )
        assert _determine_point_format(cloud) == "xyzrgbi"

    def test_intensity_without_rgb_is_xyz(self) -> None:
        cloud = PointCloud(
            xyz=np.random.rand(100, 3),
            intensity=np.random.rand(100).astype(np.float32),
        )
        # Without RGB, even with intensity, format is xyz
        # (intensity needs RGB too for xyzrgbi)
        assert _determine_point_format(cloud) == "xyz"


class TestPackPoints:
    def test_pack_xyz(self) -> None:
        xyz = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        result = _pack_points(xyz, None, None, "xyz")
        arr = np.frombuffer(result, dtype=np.float32).reshape(-1, 3)
        np.testing.assert_allclose(arr, [[1, 2, 3], [4, 5, 6]], atol=1e-6)

    def test_pack_xyzrgb(self) -> None:
        xyz = np.array([[1.0, 2.0, 3.0]])
        rgb = np.array([[255, 128, 0]], dtype=np.uint8)
        result = _pack_points(xyz, rgb, None, "xyzrgb")
        arr = np.frombuffer(result, dtype=np.float32).reshape(-1, 6)
        assert arr.shape == (1, 6)
        np.testing.assert_allclose(arr[0, :3], [1, 2, 3], atol=1e-6)
        np.testing.assert_allclose(arr[0, 3], 1.0, atol=0.01)  # 255/255
        np.testing.assert_allclose(arr[0, 4], 128 / 255, atol=0.01)
        np.testing.assert_allclose(arr[0, 5], 0.0, atol=0.01)

    def test_pack_xyzrgbi(self) -> None:
        xyz = np.array([[1.0, 2.0, 3.0]])
        rgb = np.array([[255, 255, 255]], dtype=np.uint8)
        intensity = np.array([0.5], dtype=np.float32)
        result = _pack_points(xyz, rgb, intensity, "xyzrgbi")
        arr = np.frombuffer(result, dtype=np.float32).reshape(-1, 7)
        assert arr.shape == (1, 7)
        np.testing.assert_allclose(
            arr[0, 6], 1.0, atol=0.01
        )  # 0.5/0.5 = normalized to 1.0

    def test_pack_with_indices(self) -> None:
        xyz = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]])
        indices = np.array([0, 2])
        result = _pack_points(xyz, None, None, "xyz", indices)
        arr = np.frombuffer(result, dtype=np.float32).reshape(-1, 3)
        assert arr.shape == (2, 3)
        np.testing.assert_allclose(arr[0], [1, 2, 3], atol=1e-6)
        np.testing.assert_allclose(arr[1], [7, 8, 9], atol=1e-6)


class TestSubdivideOctree:
    def test_small_cloud_single_tile(self) -> None:
        cloud = PointCloud(xyz=np.random.rand(100, 3))
        tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=500,
        )
        assert len(tiles) == 1
        assert len(tiles[0]["xyz"]) == 100
        assert tiles[0]["indices"] is not None

    def test_large_cloud_splits(self) -> None:
        rng = np.random.default_rng(42)
        cloud = PointCloud(xyz=rng.uniform(0, 100, size=(10_000, 3)))
        tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=2_000,
        )
        assert len(tiles) > 1
        # All tiles within budget
        for tile in tiles:
            assert len(tile["xyz"]) <= 2_000

    def test_total_points_preserved(self) -> None:
        rng = np.random.default_rng(42)
        cloud = PointCloud(xyz=rng.uniform(0, 50, size=(5_000, 3)))
        tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=1_000,
        )
        total = sum(len(t["xyz"]) for t in tiles)
        assert total == 5_000

    def test_indices_valid(self) -> None:
        rng = np.random.default_rng(42)
        cloud = PointCloud(xyz=rng.uniform(0, 10, size=(3_000, 3)))
        tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=1_000,
        )
        all_indices = np.concatenate([t["indices"] for t in tiles])
        # All indices unique and within range
        assert len(np.unique(all_indices)) == 3_000
        assert all_indices.max() < 3_000
        assert all_indices.min() >= 0

    def test_rgb_preserved(self) -> None:
        rng = np.random.default_rng(42)
        cloud = PointCloud(
            xyz=rng.uniform(0, 10, size=(500, 3)),
            rgb=rng.integers(0, 255, (500, 3), dtype=np.uint8),
        )
        tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=200,
        )
        for tile in tiles:
            assert tile["rgb"] is not None
            assert len(tile["rgb"]) == len(tile["xyz"])

    def test_intensity_preserved(self) -> None:
        rng = np.random.default_rng(42)
        cloud = PointCloud(
            xyz=rng.uniform(0, 10, size=(500, 3)),
            intensity=rng.uniform(0, 1, 500).astype(np.float32),
        )
        tiles = _subdivide_octree(
            cloud=cloud,
            bounds_min=cloud.bounds_min,
            bounds_max=cloud.bounds_max,
            max_points=200,
        )
        for tile in tiles:
            assert tile["intensity"] is not None
            assert len(tile["intensity"]) == len(tile["xyz"])
