"""End-to-end pipeline tests using synthetic point clouds."""

import warnings
from pathlib import Path

import numpy as np
import orjson
import pytest
from pydantic import ValidationError

from pointcloud_detect.config import (
    PipelineConfig,
    PostprocessingConfig,
    TilingConfig,
)
from pointcloud_detect.models import Detection, PointCloud, Tile
from pointcloud_detect.pipeline import run_pipeline
from pointcloud_detect.postprocessing import (
    compute_aabb,
    filter_by_confidence,
    filter_by_excluded_labels,
    filter_by_size,
    iou_3d_aabb,
    nms_3d,
    normalize_detections,
)
from pointcloud_detect.preprocessing import preprocess, voxel_downsample
from pointcloud_detect.tiling import (
    _density_median_split,
    _octant_bounds,
    adaptive_tile,
    compute_density_grid,
)


def _indices(tile: Tile) -> np.ndarray:
    """Return a tile's point_indices, asserting the invariant that they are set.

    Tile.point_indices is Optional because the field defaults to None, but every tile
    adaptive_tile emits populates it. Asserting here states that invariant once instead
    of leaving each call site to subscript, len() or iterate a value typed as possibly
    None.
    """
    assert tile.point_indices is not None
    return tile.point_indices


def _mmdet3d_available() -> bool:
    try:
        import mmdet3d  # noqa: F401  # pyright: ignore[reportMissingImports]

        return True
    except ImportError:
        return False


def _minkowski_available() -> bool:
    try:
        import MinkowskiEngine  # noqa: F401  # pyright: ignore[reportMissingImports]

        return True
    except ImportError:
        return False


def _make_synthetic_ply(path: Path, num_points: int = 1000) -> None:
    """Write a minimal ASCII PLY file with random points."""
    rng = np.random.default_rng(42)
    xyz = rng.uniform(-10, 10, size=(num_points, 3))
    rgb = rng.integers(0, 255, size=(num_points, 3))

    with open(path, "w") as f:
        f.write("ply\n")
        f.write("format ascii 1.0\n")
        f.write(f"element vertex {num_points}\n")
        f.write("property float x\n")
        f.write("property float y\n")
        f.write("property float z\n")
        f.write("property uchar red\n")
        f.write("property uchar green\n")
        f.write("property uchar blue\n")
        f.write("end_header\n")
        for i in range(num_points):
            f.write(
                f"{xyz[i, 0]:.6f} {xyz[i, 1]:.6f} {xyz[i, 2]:.6f} "
                f"{rgb[i, 0]} {rgb[i, 1]} {rgb[i, 2]}\n"
            )


class TestPreprocessing:
    def test_voxel_downsample_reduces_points(self) -> None:
        rng = np.random.default_rng(0)
        xyz = rng.uniform(0, 1, size=(10000, 3))
        indices = voxel_downsample(xyz, voxel_size=0.1)
        assert len(indices) < 10000
        assert len(indices) > 0

    def test_preprocess_respects_color_flag(self) -> None:
        cloud = PointCloud(
            xyz=np.random.rand(100, 3),
            rgb=np.random.randint(0, 255, (100, 3), dtype=np.uint8),
        )
        config = PipelineConfig()
        config.preprocessing.use_color = False
        config.preprocessing.downsample = False
        result = preprocess(cloud, config.preprocessing)
        assert result.rgb is None

    def test_preprocess_keeps_color_when_enabled(self) -> None:
        cloud = PointCloud(
            xyz=np.random.rand(100, 3),
            rgb=np.random.randint(0, 255, (100, 3), dtype=np.uint8),
        )
        config = PipelineConfig()
        config.preprocessing.use_color = True
        config.preprocessing.downsample = False
        result = preprocess(cloud, config.preprocessing)
        assert result.rgb is not None


class TestTiling:
    def test_adaptive_tile_splits_large_cloud(self) -> None:
        rng = np.random.default_rng(1)
        cloud = PointCloud(xyz=rng.uniform(0, 100, size=(500_000, 3)))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 100_000
        tiles = adaptive_tile(cloud, config.tiling)
        assert len(tiles) > 1
        for tile in tiles:
            # Leaf tiles are within budget; guard-band overlap can add points, so
            # allow headroom. Emitted-over-budget tiles only occur below min size.
            assert tile.xyz.shape[0] <= config.tiling.max_points_per_tile * 2

    def test_small_cloud_single_tile(self) -> None:
        cloud = PointCloud(xyz=np.random.rand(100, 3))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 100_000
        tiles = adaptive_tile(cloud, config.tiling)
        assert len(tiles) == 1

    def test_empty_cloud_no_tiles(self) -> None:
        cloud = PointCloud(xyz=np.zeros((0, 3), dtype=np.float64))
        tiles = adaptive_tile(cloud, PipelineConfig().tiling)
        assert tiles == []

    def test_all_points_covered_no_loss(self) -> None:
        # Every source point must appear in at least one tile (guard band may
        # place a boundary point in more than one, but never fewer than one).
        rng = np.random.default_rng(7)
        cloud = PointCloud(xyz=rng.uniform(0, 100, size=(50_000, 3)))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 10_000
        config.tiling.overlap_margin_m = 0.0  # exact partition, no double-count
        tiles = adaptive_tile(cloud, config.tiling)
        covered = np.concatenate([_indices(t) for t in tiles])
        assert np.array_equal(np.unique(covered), np.arange(cloud.num_points))

    def test_indices_in_range(self) -> None:
        rng = np.random.default_rng(9)
        cloud = PointCloud(xyz=rng.uniform(0, 80, size=(40_000, 3)))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 8_000
        tiles = adaptive_tile(cloud, config.tiling)
        for tile in tiles:
            assert _indices(tile).min() >= 0
            assert _indices(tile).max() < cloud.num_points

    def test_thin_z_scene_does_not_split_z(self) -> None:
        # NOTE: this assertion also holds for a single-largest-axis splitter, so
        # it does not by itself demonstrate axis qualification. The discriminating
        # coverage for simultaneous multi-axis splitting is
        # test_cubic_scene_splits_all_axes (strictly interior on all three axes)
        # and TestOctantBounds below (child count per qualifying axis set).
        # Outdoor-like: large XY (100 m), thin Z (3 m < 2*min_tile_size_m=10 m).
        # The Z axis must never be split, so every tile spans the full Z extent.
        rng = np.random.default_rng(11)
        xy = rng.uniform(0, 100, size=(200_000, 2))
        z = rng.uniform(0, 3, size=(200_000, 1))
        cloud = PointCloud(xyz=np.hstack([xy, z]))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 20_000
        config.tiling.min_tile_size_m = 5.0
        config.tiling.overlap_margin_m = 0.0
        tiles = adaptive_tile(cloud, config.tiling)
        assert len(tiles) > 1
        z_min, z_max = cloud.bounds_min[2], cloud.bounds_max[2]
        for tile in tiles:
            assert np.isclose(tile.extent_min[2], z_min)
            assert np.isclose(tile.extent_max[2], z_max)

    def test_cubic_scene_splits_all_axes(self) -> None:
        # Indoor-like: roughly cubic and large on every axis (40 m each, all
        # >= 2*min_tile_size_m). At least one tile must be strictly interior on
        # all three axes, proving a genuine 8-way (octree) split occurred.
        rng = np.random.default_rng(13)
        cloud = PointCloud(xyz=rng.uniform(0, 40, size=(300_000, 3)))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 30_000
        config.tiling.min_tile_size_m = 5.0
        config.tiling.overlap_margin_m = 0.0
        tiles = adaptive_tile(cloud, config.tiling)
        gmin, gmax = cloud.bounds_min, cloud.bounds_max
        interior_all_axes = [
            t
            for t in tiles
            if all(
                t.extent_min[a] > gmin[a] + 1e-6 and t.extent_max[a] < gmax[a] - 1e-6
                for a in range(3)
            )
        ]
        assert interior_all_axes, "expected at least one fully-interior octant"

    def test_symmetric_clusters_are_not_bisected(self) -> None:
        # NOTE: symmetric clusters at x=10 and x=90 are separated by the geometric
        # midpoint (x=50) just as cleanly as by the point-count median, so this
        # asserts a property a midpoint splitter also satisfies. Kept as a
        # regression guard on cluster integrity, NOT as evidence of density-aware
        # placement — that lives in
        # TestSplitPlanePlacement::test_median_balances_point_counts_on_skewed_data,
        # which fails against a midpoint splitter.
        rng = np.random.default_rng(17)
        left = np.column_stack(
            [
                rng.normal(10, 1, 60_000),
                rng.uniform(0, 40, 60_000),
                rng.uniform(0, 40, 60_000),
            ]
        )
        right = np.column_stack(
            [
                rng.normal(90, 1, 60_000),
                rng.uniform(0, 40, 60_000),
                rng.uniform(0, 40, 60_000),
            ]
        )
        cloud = PointCloud(xyz=np.vstack([left, right]))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 70_000
        config.tiling.min_tile_size_m = 5.0
        config.tiling.overlap_margin_m = 0.0
        tiles = adaptive_tile(cloud, config.tiling)
        # No tile should contain a large slice of BOTH clusters — i.e. the split
        # separated them. Check that each tile is dominated by one cluster.
        for tile in tiles:
            xs = tile.xyz[:, 0]
            near_left = np.sum(xs < 50)
            near_right = np.sum(xs >= 50)
            # A tile may legitimately hold few stray points from the other side;
            # assert it isn't a near-even mix (which would mean a mid-cluster cut).
            minority = min(near_left, near_right)
            majority = max(near_left, near_right)
            if majority > 0:
                assert minority / majority < 0.2


class TestDensityGrid:
    def test_counts_sum_to_total(self) -> None:
        rng = np.random.default_rng(3)
        xyz = rng.uniform(0, 100, size=(10_000, 3))
        grid, origin, shape = compute_density_grid(xyz, resolution=10.0)
        assert int(grid.sum()) == 10_000
        assert tuple(grid.shape) == tuple(shape)
        np.testing.assert_allclose(origin, xyz.min(axis=0))

    def test_dense_region_has_higher_count(self) -> None:
        # Pack most points into one corner; that cell must dominate.
        rng = np.random.default_rng(5)
        dense = rng.uniform(0, 5, size=(9_000, 3))
        sparse = rng.uniform(0, 100, size=(1_000, 3))
        xyz = np.vstack([dense, sparse])
        grid, _origin, _shape = compute_density_grid(xyz, resolution=10.0)
        assert grid[0, 0, 0] >= 8_000

    def test_single_point_grid(self) -> None:
        xyz = np.array([[1.0, 2.0, 3.0]])
        grid, _origin, shape = compute_density_grid(xyz, resolution=10.0)
        assert int(grid.sum()) == 1
        assert np.all(shape >= 1)


class TestPostprocessing:
    @staticmethod
    def _det(**overrides: object) -> Detection:
        kwargs: dict[str, object] = {
            "label": "chair",
            "confidence": 0.9,
            "center": np.array([5.0, 5.0, 1.0]),
            "size": np.array([1.0, 1.0, 2.0]),
            "heading": 0.0,
        }
        kwargs.update(overrides)
        return Detection(**kwargs)  # type: ignore[arg-type]

    def test_iou_returns_zero_when_only_one_aabb_bound_is_set(self) -> None:
        """A half-populated AABB must return 0.0, not raise.

        aabb_min and aabb_max are independent Optional fields and nothing enforces
        that they are set as a pair. Guarding on aabb_min alone let the subtraction
        below reach `ndarray - None` and raise TypeError. Only compute_aabb() happens
        to set both, so any directly-constructed or deserialised Detection could
        trigger it.
        """
        full = compute_aabb(self._det())

        # min set, max missing -- the case the original guard let through
        min_only = self._det()
        min_only.aabb_min = np.array([0.0, 0.0, 0.0])
        min_only.aabb_max = None
        assert iou_3d_aabb(full, min_only) == 0.0
        assert iou_3d_aabb(min_only, full) == 0.0

        # max set, min missing -- symmetric, and caught by the original guard
        max_only = self._det()
        max_only.aabb_min = None
        max_only.aabb_max = np.array([1.0, 1.0, 1.0])
        assert iou_3d_aabb(full, max_only) == 0.0
        assert iou_3d_aabb(max_only, full) == 0.0

    def test_iou_still_computes_for_fully_populated_aabbs(self) -> None:
        """The guard must not swallow the normal path: identical boxes are IoU 1.0."""
        a = compute_aabb(self._det())
        b = compute_aabb(self._det())
        assert iou_3d_aabb(a, b) == pytest.approx(1.0)

        far = compute_aabb(self._det(center=np.array([100.0, 100.0, 100.0])))
        assert iou_3d_aabb(a, far) == 0.0

    def test_compute_aabb(self) -> None:
        det = Detection(
            label="chair",
            confidence=0.9,
            center=np.array([5.0, 5.0, 1.0]),
            size=np.array([1.0, 1.0, 2.0]),
            heading=0.0,
        )
        det = compute_aabb(det)
        assert det.aabb_min is not None
        assert det.aabb_max is not None
        np.testing.assert_allclose(det.aabb_min, [4.5, 4.5, 0.0], atol=1e-6)
        np.testing.assert_allclose(det.aabb_max, [5.5, 5.5, 2.0], atol=1e-6)

    def test_confidence_filter(self) -> None:
        dets = [
            Detection(
                label="a",
                confidence=0.9,
                center=np.zeros(3),
                size=np.ones(3),
                heading=0.0,
            ),
            Detection(
                label="b",
                confidence=0.3,
                center=np.zeros(3),
                size=np.ones(3),
                heading=0.0,
            ),
        ]
        filtered = filter_by_confidence(dets, threshold=0.5)
        assert len(filtered) == 1
        assert filtered[0].label == "a"

    def test_nms_removes_duplicates(self) -> None:
        det1 = Detection(
            label="chair",
            confidence=0.95,
            center=np.array([5.0, 5.0, 1.0]),
            size=np.array([1.0, 1.0, 2.0]),
            heading=0.0,
        )
        det2 = Detection(
            label="chair",
            confidence=0.85,
            center=np.array([5.1, 5.1, 1.0]),
            size=np.array([1.0, 1.0, 2.0]),
            heading=0.0,
        )
        det1 = compute_aabb(det1)
        det2 = compute_aabb(det2)
        result = nms_3d([det1, det2], iou_threshold=0.3)
        assert len(result) == 1
        assert result[0].confidence == 0.95

    def test_normalize_coordinates(self) -> None:
        det = Detection(
            label="table",
            confidence=0.8,
            center=np.array([5.0, 5.0, 5.0]),
            size=np.array([2.0, 2.0, 1.0]),
            heading=0.0,
        )
        scene_min = np.array([0.0, 0.0, 0.0])
        scene_max = np.array([10.0, 10.0, 10.0])
        result = normalize_detections([det], scene_min, scene_max)
        np.testing.assert_allclose(result[0].center, [0.5, 0.5, 0.5])
        np.testing.assert_allclose(result[0].size, [0.2, 0.2, 0.1])

    def test_filter_by_size_min(self) -> None:
        dets = [
            Detection(
                label="chair",
                confidence=0.9,
                center=np.zeros(3),
                size=np.array([0.5, 0.5, 0.8]),
                heading=0.0,
            ),
            Detection(
                label="building",
                confidence=0.9,
                center=np.zeros(3),
                size=np.array([10.0, 8.0, 5.0]),
                heading=0.0,
            ),
        ]
        result = filter_by_size(dets, min_size_m=1.0)
        assert len(result) == 1
        assert result[0].label == "building"

    def test_filter_by_size_max(self) -> None:
        dets = [
            Detection(
                label="chair",
                confidence=0.9,
                center=np.zeros(3),
                size=np.array([0.5, 0.5, 0.8]),
                heading=0.0,
            ),
            Detection(
                label="building",
                confidence=0.9,
                center=np.zeros(3),
                size=np.array([10.0, 8.0, 5.0]),
                heading=0.0,
            ),
        ]
        result = filter_by_size(dets, max_size_m=2.0)
        assert len(result) == 1
        assert result[0].label == "chair"

    def test_filter_by_excluded_labels(self) -> None:
        dets = [
            Detection(
                label="chair",
                confidence=0.9,
                center=np.zeros(3),
                size=np.ones(3),
                heading=0.0,
            ),
            Detection(
                label="curtain",
                confidence=0.8,
                center=np.zeros(3),
                size=np.ones(3),
                heading=0.0,
            ),
            Detection(
                label="table",
                confidence=0.85,
                center=np.zeros(3),
                size=np.ones(3),
                heading=0.0,
            ),
        ]
        result = filter_by_excluded_labels(dets, ["curtain", "Curtain"])
        assert len(result) == 2
        assert all(d.label != "curtain" for d in result)

    def test_excluded_labels_case_insensitive(self) -> None:
        dets = [
            Detection(
                label="Chair",
                confidence=0.9,
                center=np.zeros(3),
                size=np.ones(3),
                heading=0.0,
            ),
        ]
        result = filter_by_excluded_labels(dets, ["chair"])
        assert len(result) == 0


class TestEndToEnd:
    @pytest.mark.skipif(not _mmdet3d_available(), reason="mmdet3d not installed")
    def test_full_pipeline_with_ply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real pretrained checkpoints, which are non-commercially licensed: opt in.
        monkeypatch.setenv("ALLOW_NONCOMMERCIAL_WEIGHTS", "1")
        ply_path = tmp_path / "test_scene.ply"
        _make_synthetic_ply(ply_path, num_points=5000)

        config = PipelineConfig()
        config.output.directory = tmp_path / "output"
        config.preprocessing.downsample = False
        config.model.segmentation.enabled = True
        config.tiling.max_points_per_tile = 2000

        result = run_pipeline(ply_path, config)

        assert result.tile_count > 1
        # Detections may be 0 on synthetic data with pre-trained models (domain gap)
        assert isinstance(result.detections, list)
        assert result.segmentation is not None

        output_json = config.output.directory / "test_scene_detections.json"
        assert output_json.exists()

        data = orjson.loads(output_json.read_bytes())
        assert "metadata" in data
        assert "detections" in data
        assert data["metadata"]["tile_count"] == result.tile_count

    @pytest.mark.skipif(not _mmdet3d_available(), reason="mmdet3d not installed")
    def test_normalized_output(self, tmp_path: Path) -> None:
        ply_path = tmp_path / "norm_test.ply"
        _make_synthetic_ply(ply_path, num_points=1000)

        config = PipelineConfig()
        config.output.directory = tmp_path / "output"
        config.output.normalize_coordinates = True
        config.preprocessing.downsample = False
        config.postprocessing.raw_mode = True

        run_pipeline(ply_path, config)

        output_json = config.output.directory / "norm_test_detections.json"
        data = orjson.loads(output_json.read_bytes())
        assert data["metadata"]["coordinates_normalized"] is True

        for det in data["detections"]:
            center = det["bbox_obb"]["center"]
            assert all(0.0 <= c <= 1.0 for c in center)


class TestSplitPlanePlacement:
    """Direct coverage of split-plane selection.

    These test the contract rather than inferring it from emitted tiles. The
    integration-level tests above cannot distinguish a point-count median from a
    geometric midpoint on symmetric data, which is how the min_tile_size_m
    violation below went unnoticed.
    """

    def test_median_balances_point_counts_on_skewed_data(self) -> None:
        # 80% of mass near x=10, 20% spread out to x=100. A geometric midpoint
        # (50.0) splits these 80/20; the point-count median splits them ~50/50.
        rng = np.random.default_rng(23)
        dense = rng.normal(10.0, 1.0, 80_000)
        sparse = rng.uniform(20.0, 100.0, 20_000)
        coords = np.concatenate([dense, sparse])

        plane = _density_median_split(coords, 0.0, 100.0, min_tile_size_m=1.0)

        midpoint = 50.0
        assert not np.isclose(plane, midpoint), "fell back to the midpoint"
        low = int(np.count_nonzero(coords <= plane))
        high = int(np.count_nonzero(coords > plane))
        # Balanced within 10% — the property a midpoint cut does not have here.
        assert abs(low - high) / len(coords) < 0.10
        # And demonstrate the midpoint genuinely is unbalanced on this input.
        mid_low = int(np.count_nonzero(coords <= midpoint))
        assert abs(mid_low - (len(coords) - mid_low)) / len(coords) > 0.50

    def test_median_clamped_so_both_children_clear_minimum(self) -> None:
        # All mass hugs the lower bound, so the raw median is ~0.5 — which would
        # leave a child 0.5 m wide against a 5 m minimum. The plane must be pushed
        # into the legal window. See
        # test_out_of_window_median_clamps_rather_than_reverting_to_midpoint for
        # the stronger assertion that it lands on the nearest legal plane.
        rng = np.random.default_rng(29)
        coords = rng.uniform(0.0, 1.0, 50_000)

        plane = _density_median_split(coords, 0.0, 20.0, min_tile_size_m=5.0)

        assert plane - 0.0 >= 5.0, f"low child only {plane:.3f} m wide"
        assert 20.0 - plane >= 5.0, f"high child only {20.0 - plane:.3f} m wide"

    def test_empty_coords_returns_midpoint(self) -> None:
        plane = _density_median_split(
            np.array([], dtype=np.float64), 0.0, 10.0, min_tile_size_m=1.0
        )
        assert np.isclose(plane, 5.0)


class TestOctantBounds:
    """Child enumeration must carry its own side bits.

    Re-deriving the side from child bounds is only valid while the plane sits
    strictly inside the parent, which is why a plane equal to a bound previously
    classified every child as the high side and dropped all points.
    """

    def test_child_count_matches_qualifying_axis_count(self) -> None:
        lo = np.zeros(3)
        hi = np.array([10.0, 10.0, 10.0])
        assert len(_octant_bounds(lo, hi, {0: 5.0})) == 2
        assert len(_octant_bounds(lo, hi, {0: 5.0, 1: 5.0})) == 4
        assert len(_octant_bounds(lo, hi, {0: 5.0, 1: 5.0, 2: 5.0})) == 8

    def test_sides_are_carried_not_inferred(self) -> None:
        # Plane equal to the parent's lower bound: inferring the side from
        # child_min[axis] >= plane would call every child "high".
        lo = np.zeros(3)
        hi = np.array([10.0, 10.0, 10.0])
        children = _octant_bounds(lo, hi, {0: 0.0})

        sides = sorted(s[0] for _, _, s in children)
        assert sides == [False, True], "both children reported the same side"


class TestTilingRobustness:
    """Degenerate inputs that previously failed silently or unboundedly."""

    def test_non_finite_coordinates_raise(self) -> None:
        # NaN previously became the split plane, every comparison against it was
        # False, and tiles were emitted holding zero points — a job reporting
        # success having dropped the entire cloud.
        rng = np.random.default_rng(31)
        xyz = rng.uniform(0, 50, size=(10_000, 3))
        xyz[7, 1] = np.nan
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 1_000

        with pytest.raises(ValueError, match="non-finite"):
            adaptive_tile(PointCloud(xyz=xyz), config.tiling)

    def test_infinite_coordinates_raise(self) -> None:
        rng = np.random.default_rng(37)
        xyz = rng.uniform(0, 50, size=(10_000, 3))
        xyz[11, 2] = np.inf
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 1_000

        with pytest.raises(ValueError, match="non-finite"):
            adaptive_tile(PointCloud(xyz=xyz), config.tiling)

    def test_zero_min_tile_size_rejected_by_config(self) -> None:
        # min_tile_size_m = 0 made every axis "splittable", including zero-extent
        # ones, which produced a plane equal to both bounds and zero tiles.
        with pytest.raises(ValidationError):
            TilingConfig(min_tile_size_m=0.0)

    def test_zero_density_grid_resolution_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TilingConfig(density_grid_resolution_m=0.0)

    def test_planar_cloud_covers_all_points(self) -> None:
        # Zero extent on Z. Every point must still land in some tile.
        rng = np.random.default_rng(41)
        xyz = np.column_stack(
            [
                rng.uniform(0, 20, 5_000),
                rng.uniform(0, 20, 5_000),
                np.full(5_000, 7.0),
            ]
        )
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 1_000
        config.tiling.min_tile_size_m = 1.0
        config.tiling.overlap_margin_m = 0.0

        tiles = adaptive_tile(PointCloud(xyz=xyz), config.tiling)

        assert len(tiles) > 0
        covered = np.unique(np.concatenate([_indices(t) for t in tiles]))
        assert covered.size == 5_000

    def test_emitted_tiles_respect_min_tile_size(self) -> None:
        # The invariant the axis guard claims and an unclamped median cut breaks.
        # A tight cluster hugging the lower bound drags the median hard against
        # that bound, so the "high" child is nearly the whole parent and the
        # "low" child collapses. Measured 7/8 tiles below a 5 m minimum, smallest
        # 2.0045 m, before the plane was clamped. Diffuse or symmetric clusters
        # do NOT reproduce this — the median lands legally and the bug hides.
        rng = np.random.default_rng(11)
        tight = rng.normal([2, 2, 2], [0.3, 0.3, 0.3], size=(90_000, 3))
        tail = rng.uniform([0, 0, 0], [100, 100, 100], size=(10_000, 3))
        xyz = np.vstack([tight, tail])

        config = PipelineConfig()
        config.tiling.max_points_per_tile = 20_000
        config.tiling.min_tile_size_m = 5.0
        config.tiling.overlap_margin_m = 0.0

        tiles = adaptive_tile(PointCloud(xyz=xyz), config.tiling)

        assert tiles
        # The guarantee holds per axis only where that axis was actually CUT. An
        # axis that never qualified keeps the root extent, which may legitimately
        # be under the minimum (see test_planar_scene_keeps_unsplit_axis_extent).
        # Asserting it unconditionally passes here only because this fixture is a
        # cube, so every axis qualifies.
        root_extent = xyz.max(axis=0) - xyz.min(axis=0)
        min_size = config.tiling.min_tile_size_m
        for tile in tiles:
            extent = tile.extent_max - tile.extent_min
            for axis in range(3):
                was_cut = extent[axis] < root_extent[axis] - 1e-9
                if was_cut:
                    assert extent[axis] >= min_size - 1e-9, (
                        f"axis {axis} was split to {extent[axis]}, below the "
                        f"{min_size} minimum"
                    )

    def test_out_of_window_median_clamps_rather_than_reverting_to_midpoint(
        self,
    ) -> None:
        # The legal window is [lo + min_size, hi - min_size]. A median outside it
        # is pulled to the nearest legal plane, which is by construction at least
        # as close to the true median as the midpoint. Falling back to the midpoint
        # threw the density signal away in exactly the skewed distributions this
        # function exists for, and since the window narrows to nothing as the
        # extent approaches 2 x min_size, the deepest (densest) levels of the tree
        # silently became a midpoint splitter.
        coords = np.full(1_000, 5.0)  # median 5.0, well below lo_limit
        plane = _density_median_split(coords, lo=0.0, hi=100.0, min_tile_size_m=20.0)

        assert plane == 20.0, "expected the nearest legal plane, not the midpoint 50"

        # Symmetric case at the top of the range.
        coords_high = np.full(1_000, 95.0)
        plane_high = _density_median_split(
            coords_high, lo=0.0, hi=100.0, min_tile_size_m=20.0
        )
        assert plane_high == 80.0

        # A legal median is returned untouched.
        coords_mid = np.full(1_000, 42.0)
        assert (
            _density_median_split(coords_mid, lo=0.0, hi=100.0, min_tile_size_m=20.0)
            == 42.0
        )

    def test_planar_scene_keeps_unsplit_axis_extent(self) -> None:
        # A ground-plane-dominated cloud: Z extent is zero, so Z never qualifies
        # to split and every tile keeps a zero Z extent. Guards the weaker,
        # correct reading of the min-tile-size invariant — an earlier version of
        # test_emitted_tiles_respect_min_tile_size asserted every axis of every
        # tile cleared the minimum, which this scene falsifies.
        rng = np.random.default_rng(23)
        xyz = np.column_stack(
            [
                rng.uniform(0, 100, size=40_000),
                rng.uniform(0, 100, size=40_000),
                np.zeros(40_000),
            ]
        )

        config = PipelineConfig()
        config.tiling.max_points_per_tile = 5_000
        config.tiling.min_tile_size_m = 5.0
        config.tiling.overlap_margin_m = 0.0

        tiles = adaptive_tile(PointCloud(xyz=xyz), config.tiling)

        assert tiles
        z_extents = [t.extent_max[2] - t.extent_min[2] for t in tiles]
        assert all(z == 0.0 for z in z_extents), (
            "Z never qualifies to split on a planar scene, so every tile should "
            f"keep the root's zero Z extent; got {set(z_extents)}"
        )
        # X and Y did qualify, so they must respect the floor.
        for tile in tiles:
            extent = tile.extent_max - tile.extent_min
            assert extent[0] >= config.tiling.min_tile_size_m - 1e-9
            assert extent[1] >= config.tiling.min_tile_size_m - 1e-9

    def test_dense_sub_minimum_region_warns_instead_of_silently_over_budget(
        self,
    ) -> None:
        # Stop condition 2: a region already within 2 x min_tile_size_m cannot be
        # cut finer, so it is emitted over budget. Capping its contents would drop
        # points, so the only honest options are to fail or to tell the caller.
        # It used to do neither: the tile went to inference carrying multiples of
        # the budget and nothing downstream inspected the count.
        rng = np.random.default_rng(71)
        blob = rng.normal([50, 50, 50], [0.15, 0.15, 0.15], size=(200_000, 3))
        spread = rng.uniform(0, 100, size=(20_000, 3))
        xyz = np.vstack([blob, spread])

        config = PipelineConfig()
        config.tiling.max_points_per_tile = 10_000
        config.tiling.min_tile_size_m = 5.0
        config.tiling.overlap_margin_m = 0.0

        with pytest.warns(RuntimeWarning, match="cannot be subdivided further"):
            tiles = adaptive_tile(PointCloud(xyz=xyz), config.tiling)

        # The tile is still emitted — warning, not dropping.
        assert tiles
        core_counts = [len(_indices(t)) for t in tiles]
        assert max(core_counts) > config.tiling.max_points_per_tile, (
            "expected at least one over-budget tile for this fixture"
        )
        # And no points were lost to the warning path.
        covered = np.unique(np.concatenate([_indices(t) for t in tiles]))
        assert covered.size == len(xyz)

    def test_no_warning_when_every_tile_is_within_budget(self) -> None:
        # The warning must not fire on ordinary scenes, or operators will learn to
        # ignore it.
        rng = np.random.default_rng(73)
        xyz = rng.uniform(0, 100, size=(30_000, 3))

        config = PipelineConfig()
        config.tiling.max_points_per_tile = 5_000
        config.tiling.min_tile_size_m = 1.0
        config.tiling.overlap_margin_m = 0.0

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            tiles = adaptive_tile(PointCloud(xyz=xyz), config.tiling)

        assert tiles

    def test_max_tiles_ceiling_raises(self) -> None:
        # Each tile is one inference call, so unbounded tile growth is a cost
        # blow-up rather than a correctness bug. Fail loudly.
        rng = np.random.default_rng(47)
        xyz = rng.uniform(0, 100, size=(120_000, 3))
        config = PipelineConfig()
        config.tiling.max_points_per_tile = 100
        config.tiling.min_tile_size_m = 1.0
        config.tiling.overlap_margin_m = 0.0
        config.tiling.max_tiles = 32

        with pytest.raises(ValueError, match="max_tiles"):
            adaptive_tile(PointCloud(xyz=xyz), config.tiling)


class TestDensityGridBounds:
    """The grid is sized by scene extent, not point count.

    One outlier coordinate is therefore enough to make the allocation unbounded,
    and point-cloud coordinates are untrusted input. Measured before the cap: 1000
    well-behaved points plus a single point at 5e6 m requested 888 PiB.
    """

    def test_outlier_coordinate_raises_instead_of_allocating(self) -> None:
        rng = np.random.default_rng(53)
        xyz = np.vstack(
            [rng.uniform(0, 10, size=(1_000, 3)), np.array([[5e6, 5e6, 5e6]])]
        )

        with pytest.raises(ValueError, match="cells"):
            compute_density_grid(xyz, 10.0)

    def test_non_positive_resolution_raises(self) -> None:
        rng = np.random.default_rng(59)
        xyz = rng.uniform(0, 10, size=(100, 3))

        with pytest.raises(ValueError, match="resolution"):
            compute_density_grid(xyz, 0.0)
        with pytest.raises(ValueError, match="resolution"):
            compute_density_grid(xyz, -1.0)

    def test_non_finite_coordinates_raise(self) -> None:
        rng = np.random.default_rng(61)
        xyz = rng.uniform(0, 10, size=(100, 3))
        xyz[3, 0] = np.nan

        with pytest.raises(ValueError, match="NaN or inf"):
            compute_density_grid(xyz, 1.0)

    def test_normal_scene_still_works(self) -> None:
        # Regression guard: the cap must not reject an ordinary scene.
        rng = np.random.default_rng(67)
        xyz = rng.uniform(0, 200, size=(50_000, 3))

        grid, origin, shape = compute_density_grid(xyz, 10.0)

        assert int(grid.sum()) == 50_000
        assert tuple(shape) == (20, 20, 20)
        assert np.allclose(origin, xyz.min(axis=0))


def _reference_tile_contents(
    cloud: PointCloud, tiles: list, margin: float
) -> list[tuple]:
    """Select each tile's points by scanning the whole cloud.

    This is the pre-optimisation strategy, kept as an independent oracle: the
    tiler now narrows a candidate set down the recursion instead, and the two must
    agree exactly. Returns an order-independent, comparable structure.
    """
    out = []
    for t in tiles:
        lo = t.extent_min - margin
        hi = t.extent_max + margin
        mask = np.all((cloud.xyz >= lo) & (cloud.xyz <= hi), axis=1)
        idx = np.where(mask)[0]
        out.append(
            (
                tuple(np.round(t.extent_min, 9)),
                tuple(np.round(t.extent_max, 9)),
                tuple(int(i) for i in np.sort(idx)),
            )
        )
    return sorted(out)


def _actual_tile_contents(tiles: list) -> list[tuple]:
    return sorted(
        (
            tuple(np.round(t.extent_min, 9)),
            tuple(np.round(t.extent_max, 9)),
            tuple(int(i) for i in np.sort(_indices(t))),
        )
        for t in tiles
    )


class TestCandidateNarrowingEquivalence:
    """The tiler narrows a candidate index set as it recurses rather than
    re-scanning the whole cloud per tile, turning O(N x tiles) into O(N x depth).

    The transformation is only safe because a child's margin-expanded region is
    contained in its parent's (child_min >= parent_min implies child_min - margin
    >= parent_min - margin). These tests pin that equivalence against a
    whole-cloud oracle, so a future change to the recursion cannot silently drop
    guard-band points.
    """

    @staticmethod
    def _clustered(seed: int = 11) -> np.ndarray:
        rng = np.random.default_rng(seed)
        blocks = []
        for _ in range(12):
            centre = rng.uniform(10, 190, size=3)
            blocks.append(centre + rng.normal(0, 1.5, size=(1_500, 3)))
        blocks.append(rng.uniform(0, 200, size=(3_000, 3)))
        return np.vstack(blocks).astype(np.float64)

    @pytest.mark.parametrize("margin", [0.0, 0.5, 2.0, 5.0])
    def test_matches_whole_cloud_selection(self, margin: float) -> None:
        cloud = PointCloud(xyz=self._clustered())
        cfg = TilingConfig(
            max_points_per_tile=2_000,
            min_tile_size_m=1.0,
            overlap_margin_m=margin,
        )

        tiles = adaptive_tile(cloud, cfg)

        assert len(tiles) > 1, "scene should subdivide, or this proves nothing"
        assert _actual_tile_contents(tiles) == _reference_tile_contents(
            cloud, tiles, margin
        )

    def test_no_point_is_lost(self) -> None:
        # Every point must appear in at least one tile. Narrowing the candidate
        # set too aggressively would silently drop points, and the pipeline would
        # still report success.
        cloud = PointCloud(xyz=self._clustered(seed=29))
        cfg = TilingConfig(
            max_points_per_tile=2_000, min_tile_size_m=1.0, overlap_margin_m=1.0
        )

        tiles = adaptive_tile(cloud, cfg)

        covered = set()
        for t in tiles:
            covered.update(int(i) for i in _indices(t))
        assert covered == set(range(cloud.num_points))

    def test_guard_band_actually_duplicates_boundary_points(self) -> None:
        # With a margin, total points across tiles must exceed the cloud size,
        # otherwise the guard band is not doing its job and edge-straddling
        # objects would be cut.
        cloud = PointCloud(xyz=self._clustered(seed=31))
        with_band = adaptive_tile(
            cloud,
            TilingConfig(
                max_points_per_tile=2_000, min_tile_size_m=1.0, overlap_margin_m=2.0
            ),
        )
        without_band = adaptive_tile(
            cloud,
            TilingConfig(
                max_points_per_tile=2_000, min_tile_size_m=1.0, overlap_margin_m=0.0
            ),
        )

        banded = sum(len(t.xyz) for t in with_band)
        plain = sum(len(t.xyz) for t in without_band)

        assert banded > plain

        # With no margin, tiles very nearly partition the cloud, but not exactly:
        # child assignment during subdivision uses a half-open interval on the
        # split axis, while _emit_tile selects on a closed interval, so a point
        # lying exactly on a split plane is claimed by both neighbours. The excess
        # is therefore the count of such coincidences - tiny for float
        # coordinates, and pre-existing rather than a product of candidate
        # narrowing (measured at 1.0002x both before and after that change).
        assert plain >= cloud.num_points
        assert plain < cloud.num_points * 1.01

    def test_root_block_is_not_copied(self) -> None:
        # A single-tile scene must not duplicate the cloud to select its points.
        # cloud.xyz[arange(N)] is a full copy that yields something identical to
        # cloud.xyz; on a 50 GB cloud that is a 50 GB copy in the code path meant
        # to bound memory.
        rng = np.random.default_rng(37)
        cloud = PointCloud(xyz=rng.uniform(0, 10, size=(500, 3)))
        cfg = TilingConfig(
            max_points_per_tile=10_000, min_tile_size_m=1.0, overlap_margin_m=1.0
        )

        tiles = adaptive_tile(cloud, cfg)

        assert len(tiles) == 1
        assert len(tiles[0].xyz) == 500


class TestExcludedLabelsMigration:
    """The label-exclusion config key was renamed to ``excluded_labels``.

    The hazard in a config-key rename is silence: pydantic ignores unknown keys by
    default, so an existing config would keep parsing, the filter would simply not
    run, and detections the operator expected to be excluded would appear in the
    output with no error. PostprocessingConfig sets extra="forbid" so that case
    fails immediately and names the offending key.
    """

    def test_superseded_and_misspelt_keys_are_rejected_loudly(self) -> None:
        # extra="forbid" is the whole guarantee: any key the model does not define
        # raises instead of being ignored. That covers the superseded filter key
        # and ordinary typos alike, and pydantic echoes the offending key from the
        # operator's own file so the error is actionable without this repo having
        # to name the retired term.
        with pytest.raises(ValidationError) as exc:
            PostprocessingConfig(confidence_threshhold=0.9)  # type: ignore[call-arg]

        message = str(exc.value)
        assert "confidence_threshhold" in message
        assert "extra" in message.lower()

    def test_new_key_works_and_filters(self) -> None:
        cfg = PostprocessingConfig(excluded_labels=["Curtain"])
        dets = [
            Detection(
                label="curtain",
                confidence=0.9,
                center=np.array([0.5, 0.5, 0.5]),
                size=np.array([1.0, 1.0, 1.0]),
                heading=0.0,
            ),
            Detection(
                label="chair",
                confidence=0.9,
                center=np.array([2.0, 2.0, 2.0]),
                size=np.array([1.0, 1.0, 1.0]),
                heading=0.0,
            ),
        ]

        kept = filter_by_excluded_labels(dets, cfg.excluded_labels)

        assert [d.label for d in kept] == ["chair"]
