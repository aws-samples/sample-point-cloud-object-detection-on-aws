"""Tests for memory-budget enforcement (NFR-1.1 / NFR-1.2).

Covers the budget math (memory.py), the tiling budget cap, streaming tile planning,
and — using a real synthetic LAS file written via laspy — the chunked spatial region
read and end-to-end tile streaming. The LAS-backed tests exercise the real reader path
(no mocks); the on-hardware 50 GB benchmark that proves the scale claim lives in the
runbook, not here.
"""

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from pointcloud_detect import memory
from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.models import PointCloud
from pointcloud_detect.tiling import adaptive_tile

if TYPE_CHECKING:
    from pointcloud_detect.streaming import ScenePass1

# Availability is probed without importing, so there is no module-scope binding for
# the skipif marks to leave conditionally unbound. The helpers that actually need these
# libraries import them locally, where the skipif has already guaranteed they are there.
LASPY = importlib.util.find_spec("laspy") is not None
PYE57 = importlib.util.find_spec("pye57") is not None


class TestBudgetMath:
    def test_bytes_per_point_channels(self) -> None:
        assert memory.bytes_per_point() == 24  # xyz f64
        assert memory.bytes_per_point(has_rgb=True) == 27
        assert memory.bytes_per_point(has_intensity=True) == 28
        assert memory.bytes_per_point(has_rgb=True, has_intensity=True) == 31
        assert memory.bytes_per_point(has_normals=True) == 36

    def test_budget_derived_scales_with_budget(self) -> None:
        small = memory.budget_derived_max_points(1.0)
        big = memory.budget_derived_max_points(16.0)
        assert big == pytest.approx(small * 16, rel=1e-6)

    def test_budget_derived_positive(self) -> None:
        assert memory.budget_derived_max_points(16.0) >= 1

    def test_zero_budget_rejected(self) -> None:
        with pytest.raises(ValueError):
            memory.budget_derived_max_points(0.0)

    def test_effective_cap_only_lowers(self) -> None:
        # A tiny configured cap is preserved; the budget never raises it.
        assert memory.effective_max_points_per_tile(1_000, 16.0) == 1_000
        # A huge configured cap is lowered to the budget-derived value.
        derived = memory.budget_derived_max_points(16.0)
        assert memory.effective_max_points_per_tile(10**12, 16.0) == derived

    def test_exceeds_budget_true_for_huge_cloud(self) -> None:
        # 10 billion points cannot fit a 16 GB budget.
        assert memory.exceeds_budget(
            10_000_000_000, 16.0, has_rgb=True, has_intensity=True
        )

    def test_exceeds_budget_false_for_small_cloud(self) -> None:
        assert not memory.exceeds_budget(1_000_000, 16.0)


class TestTilingRespectsbudget:
    def test_low_budget_forces_more_tiles(self) -> None:
        rng = np.random.default_rng(1)
        cloud = PointCloud(xyz=rng.uniform(0, 100, size=(200_000, 3)))
        cfg = PipelineConfig()
        cfg.tiling.max_points_per_tile = 10_000_000  # effectively unbounded by count
        # A tiny budget must lower the effective per-tile cap and force splitting.
        cfg.tiling.memory_budget_gb = 0.001
        tiles = adaptive_tile(cloud, cfg.tiling)
        derived = memory.budget_derived_max_points(0.001)
        assert len(tiles) > 1
        for t in tiles:
            # Leaf within derived cap (guard band may add points -> allow 2x headroom).
            assert t.xyz.shape[0] <= max(derived * 2, 2)


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestStreamingLas:
    @staticmethod
    def _write_las(path: Path, xyz: np.ndarray) -> None:
        # Local import: the skipif mark above has already gated this helper.
        import laspy

        header = laspy.LasHeader(point_format=3, version="1.2")
        # Use small scales so integer-quantized coords round-trip our float positions.
        header.scales = np.array([0.001, 0.001, 0.001])
        header.offsets = xyz.min(axis=0)
        las = laspy.LasData(header)
        las.x = xyz[:, 0]
        las.y = xyz[:, 1]
        las.z = xyz[:, 2]
        las.write(str(path))

    def test_scan_bounds_matches_data(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import scan_bounds_las

        rng = np.random.default_rng(2)
        xyz = rng.uniform(0, 50, size=(20_000, 3))
        p = tmp_path / "scene.las"
        self._write_las(p, xyz)
        bmin, bmax, count = scan_bounds_las(p)
        assert count == 20_000
        np.testing.assert_allclose(bmin, xyz.min(axis=0), atol=0.01)
        np.testing.assert_allclose(bmax, xyz.max(axis=0), atol=0.01)

    def test_read_region_returns_only_in_region(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import read_region_las

        rng = np.random.default_rng(3)
        xyz = rng.uniform(0, 100, size=(50_000, 3))
        p = tmp_path / "scene.las"
        self._write_las(p, xyz)

        region_min = np.array([0.0, 0.0, 0.0])
        region_max = np.array([25.0, 25.0, 100.0])
        got = read_region_las(p, region_min, region_max, chunk=7_000)

        # Every returned point is inside the region (within quantization tolerance).
        assert got.num_points > 0
        assert np.all(got.xyz[:, 0] <= 25.0 + 0.01)
        assert np.all(got.xyz[:, 1] <= 25.0 + 0.01)
        # Count matches a direct in-region count of the source.
        expected = int(np.sum((xyz[:, 0] <= 25.0) & (xyz[:, 1] <= 25.0)))
        assert got.num_points == pytest.approx(expected, abs=max(1, expected // 100))

    def test_read_region_empty_when_outside(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import read_region_las

        rng = np.random.default_rng(4)
        xyz = rng.uniform(0, 10, size=(5_000, 3))
        p = tmp_path / "scene.las"
        self._write_las(p, xyz)
        got = read_region_las(
            p, np.array([100.0, 100.0, 100.0]), np.array([200.0, 200.0, 200.0])
        )
        assert got.num_points == 0

    def test_stream_tiles_covers_all_points(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import stream_tiles

        # Large XY footprint so planning produces multiple tiles.
        rng = np.random.default_rng(5)
        xyz = rng.uniform(0, 60, size=(80_000, 3))
        p = tmp_path / "scene.las"
        self._write_las(p, xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 0.0  # exact partition for a clean coverage count
        cfg.preprocessing.downsample = False  # don't drop points, so coverage is exact
        cfg.preprocessing.coord_frame = "z-up"  # no transform: file frame == tile frame
        # Planning is occupancy-driven now, so a region already within the point
        # budget is not subdivided. 80k points sit under the 100k default and plan as
        # a single tile; lower the cap so the scene genuinely subdivides and this test
        # still exercises multi-tile coverage. Geometry-only planning used to split
        # regardless of point count, turning 400k points into 448 tiles of ~900
        # points each and ~450 model invocations.
        cfg.tiling.max_points_per_tile = 10_000

        tiles = list(stream_tiles(p, "las", cfg))
        assert len(tiles) > 1
        total = sum(t.xyz.shape[0] for t in tiles)
        # With overlap 0 and no downsample, streamed tiles partition the cloud: the
        # summed tile points should equal the source count (small quantization slack).
        assert total == pytest.approx(80_000, abs=80_000 // 100)


class TestPlanTileExtents:
    def test_thin_z_not_split(self) -> None:
        from pointcloud_detect.streaming import plan_tile_extents

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([100.0, 100.0, 3.0])  # Z < 2*min_tile_size
        extents = plan_tile_extents(bmin, bmax, cfg)
        assert len(extents) > 1
        for emin, emax in extents:
            assert np.isclose(emin[2], 0.0) and np.isclose(emax[2], 3.0)

    def test_cubic_splits_all_axes(self) -> None:
        from pointcloud_detect.streaming import plan_tile_extents

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([40.0, 40.0, 40.0])
        extents = plan_tile_extents(bmin, bmax, cfg)
        # First-level 8-way split of a 40 m cube at min_tile 5 m gives >= 8 leaves.
        assert len(extents) >= 8

    def test_extents_tile_the_space(self) -> None:
        from pointcloud_detect.streaming import plan_tile_extents

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([20.0, 20.0, 20.0])
        extents = plan_tile_extents(bmin, bmax, cfg)
        # Union of leaf volumes equals the parent volume (no gaps, no overlap in plan).
        total_vol = sum(np.prod(emax - emin) for emin, emax in extents)
        assert total_vol == pytest.approx(np.prod(bmax - bmin), rel=1e-6)


# ---------------------------------------------------------------------------
# Regression tests for the review findings on the memory-budget MR.
#
# The original suite passed in full while the mechanism it covered was inert, so
# these are written to fail against the unfixed code rather than to describe it.
# Nothing here neutralises the thing under test: guard bands stay on, transforms
# stay on, and the memory assertions measure peak rather than retained size.
# ---------------------------------------------------------------------------


def _write_las_file(path: Path, xyz: np.ndarray) -> Path:
    # Local import: the skipif mark above has already gated this helper.
    import laspy

    """Write a real LAS file (no mocks) so the reader path is exercised."""
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.offsets = xyz.min(axis=0)
    header.scales = np.array([0.001, 0.001, 0.001])
    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    las.write(str(path))
    return path


def _patch_las_header_bounds(path: Path, mins, maxs) -> None:
    """Patch ONLY the header bound bytes, leaving the point records untouched.

    laspy recomputes mins/maxs from the points on write, so a stale header cannot
    be produced through its API. Writing the bytes directly reproduces what happens
    in the wild whenever a tool edits points without rewriting the header. LAS 1.2
    public header block: MaxX 179, MinX 187, MaxY 195, MinY 203, MaxZ 211, MinZ 219,
    little-endian doubles.
    """
    import struct

    data = bytearray(path.read_bytes())
    for offset, value in (
        (179, maxs[0]),
        (187, mins[0]),
        (195, maxs[1]),
        (203, mins[1]),
        (211, maxs[2]),
        (219, mins[2]),
    ):
        data[offset : offset + 8] = struct.pack("<d", float(value))
    path.write_bytes(bytes(data))


class TestBudgetConstantsAreHonest:
    """The budget numbers must not overstate what has been measured."""

    def test_per_tile_and_routing_fractions_are_independent(self) -> None:
        # These were one constant, so an operator lowering it to fix a per-tile
        # overshoot silently moved the streaming threshold too: 0.5 -> 0.1 took it
        # from 258 M points to 52 M.
        #
        # The property that matters is structural: the per-tile cap and the routing
        # decision must read from DIFFERENT constants, so editing one cannot move
        # the other. Asserted via the signature defaults rather than by reassigning
        # the module constants, because those defaults are bound at def time — a
        # runtime reassignment has no effect, so a monkeypatch-based test would pass
        # for the wrong reason.
        import inspect

        tile_default = (
            inspect.signature(memory.budget_derived_max_points)
            .parameters["safety_fraction"]
            .default
        )
        routing_default = (
            inspect.signature(memory.exceeds_budget)
            .parameters["safety_fraction"]
            .default
        )
        assert tile_default == memory.TILE_SAFETY_FRACTION
        assert routing_default == memory.ROUTING_SAFETY_FRACTION
        assert memory.TILE_SAFETY_FRACTION != memory.ROUTING_SAFETY_FRACTION, (
            "the two fractions hold the same value, so this test cannot detect "
            "them being re-coupled"
        )

        # Each fraction moves only its own mechanism.
        assert memory.budget_derived_max_points(
            16.0, safety_fraction=0.1
        ) < memory.budget_derived_max_points(16.0)

        # Routing scales with its own fraction: a smaller allowance can only make
        # streaming more likely, never less.
        n = 60_000_000
        assert memory.exceeds_budget(
            n, 16.0, has_rgb=True, has_intensity=True, safety_fraction=0.1
        ) >= memory.exceeds_budget(n, 16.0, has_rgb=True, has_intensity=True)

    def test_derived_cap_binding_threshold_is_pinned(self) -> None:
        # Documents rather than hides the regime this mechanism works in. At shipped
        # defaults it does NOT bind — the derived cap is ~833x the configured one —
        # and pretending otherwise is what let "NFR-1.1 is enforced" stand unchecked.
        assert memory.budget_derived_max_points(16.0) > 100_000
        assert memory.effective_max_points_per_tile(100_000, 16.0) == 100_000

        # It does bind for tiny budgets, and the crossover is ~19.2 MB.
        assert memory.effective_max_points_per_tile(100_000, 0.010) < 100_000
        assert memory.effective_max_points_per_tile(100_000, 0.030) == 100_000

    def test_working_set_factor_is_inert_at_its_shipped_value(self) -> None:
        """Pin how far WORKING_SET_FACTOR is from mattering, and why that is fine.

        DEFAULT_WORKING_SET_FACTOR is an uncalibrated placeholder -- the on-hardware
        benchmark that would set it has never run. This test is what makes that
        acceptable rather than a hidden risk: at shipped defaults the factor feeds only
        the budget-derived per-tile cap, and that cap sits ~833x above the configured
        100,000, so the factor has no effect on any emitted tile.

        It also pins the crossover the runbook quotes when it tells operators NOT to
        reach for this knob first. That advice is only true while the threshold is far
        away; changing TILE_SAFETY_FRACTION or bytes_per_point could move it, and this
        test is what would notice.
        """
        # Where the derived cap first drops below the configured 100,000, solved
        # directly rather than searched: usable / (bpp * n) at the crossover.
        usable = 16.0 * memory.GB * memory.TILE_SAFETY_FRACTION
        threshold = usable / (memory.bytes_per_point() * 100_000)
        assert threshold == pytest.approx(3333.3, abs=0.1)

        # The shipped value is three orders of magnitude below that.
        assert memory.DEFAULT_WORKING_SET_FACTOR == 4.0
        assert memory.DEFAULT_WORKING_SET_FACTOR < threshold / 500

        # So multiplying it by 100 changes nothing an operator would observe.
        for factor in (4.0, 40.0, 400.0, 3000.0):
            assert (
                memory.effective_max_points_per_tile(
                    100_000, 16.0, working_set_factor=factor
                )
                == 100_000
            ), f"factor {factor} unexpectedly binds; the runbook's guidance is stale"

        # And past the threshold it does bind, so the mechanism is wired up, not dead.
        assert (
            memory.effective_max_points_per_tile(
                100_000, 16.0, working_set_factor=4000.0
            )
            < 100_000
        )

    def test_routing_uses_modelled_peak_not_raw_footprint(self) -> None:
        # Peak was measured at ~9x the raw footprint; routing reserved only 2x, so
        # scenes were whole-loaded that then peaked far over budget.
        n = 50_000_000
        footprint = memory.estimate_cloud_footprint_bytes(n, True, True)
        peak = memory.modelled_whole_load_peak_bytes(n, True, True)
        assert peak > footprint * 5

        # A cloud whose raw footprint fits but whose peak does not must stream.
        budget_gb = 16.0
        usable = budget_gb * memory.GB * memory.ROUTING_SAFETY_FRACTION
        assert footprint < usable < peak
        assert memory.exceeds_budget(n, budget_gb, has_rgb=True, has_intensity=True)


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestStreamingIsBounded:
    """Peak memory and I/O must not scale with how finely the scene subdivides."""

    def test_planner_refuses_an_unbounded_leaf_count(self) -> None:
        from pointcloud_detect.streaming import MAX_PLANNED_TILES, plan_tile_extents

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        # Geometry-only planning on a 1280 m cube (an ordinary aerial tile) produced
        # 16,777,216 leaves and ~4.5 GB of list before reading a single point.
        with pytest.raises(ValueError, match="MAX_PLANNED_TILES|leaves"):
            plan_tile_extents(np.zeros(3), np.array([1280.0, 1280.0, 1280.0]), cfg)
        assert MAX_PLANNED_TILES <= 1_000_000

    def test_peak_memory_does_not_scale_with_scene_extent(self, tmp_path: Path) -> None:
        # Same point count and file size at every extent, so leaf count is the only
        # variable. Previously wall-clock scaled with leaf count (1.87s at 512 leaves,
        # 15.05s at 32,768) and extent=320 m did not finish at all.
        import tracemalloc

        from pointcloud_detect.streaming import stream_tiles

        peaks = []
        for extent in (40.0, 160.0):
            rng = np.random.default_rng(3)
            xyz = rng.uniform(0, extent, size=(120_000, 3))
            path = _write_las_file(tmp_path / f"scale_{int(extent)}.las", xyz)

            cfg = PipelineConfig()
            cfg.tiling.min_tile_size_m = 10.0
            cfg.tiling.max_points_per_tile = 20_000
            cfg.preprocessing.downsample = False
            cfg.preprocessing.coord_frame = "z-up"

            tracemalloc.start()
            tracemalloc.reset_peak()
            count = sum(1 for _ in stream_tiles(path, "las", cfg))
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            assert count > 0
            peaks.append(peak)

        # A 4x extent increase is a 64x volume increase. Peak must stay flat, not
        # track the number of leaves the volume would imply.
        assert peaks[1] < peaks[0] * 2.0

    def test_streamed_tiles_respect_the_point_cap(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import stream_tiles

        # A dense cluster inside a single min-size leaf. Nothing in the streaming
        # path consulted the cap before, so this produced a 300,243-point tile
        # against a 100,000 cap.
        # The cluster must sit wholly INSIDE one min-size leaf for this to bite. A
        # cluster straddling a split plane gets divided by geometry alone and stays
        # under the cap by accident, which is why placement matters here: centred at
        # 15 with a 20 m minimum, it lands entirely in the [0,30]^3 leaf. Measured
        # against the unfixed code: 121,017 points in one tile against a 40,000 cap.
        rng = np.random.default_rng(11)
        xyz = np.vstack(
            [
                rng.normal(15, 0.5, size=(120_000, 3)),
                rng.uniform(0, 60, size=(8_000, 3)),
            ]
        )
        path = _write_las_file(tmp_path / "dense.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 20.0
        cfg.tiling.overlap_margin_m = 0.0
        cfg.tiling.max_points_per_tile = 40_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        # min_tile_size_m=20 means the [0,30]^3 leaf holding the cluster cannot be
        # cut finer, so the cap genuinely cannot be honoured here. The contract is
        # that this is SURFACED, not that it never happens — the in-memory tiler has
        # the same stop condition. Silently emitting a 3x-over-budget tile is the
        # defect; emitting it with a warning naming min_tile_size_m is the fix.
        with pytest.warns(RuntimeWarning, match="min_tile_size_m"):
            tiles = list(stream_tiles(path, "las", cfg))

        assert tiles
        largest = max(t.xyz.shape[0] for t in tiles)
        assert largest > cfg.tiling.max_points_per_tile  # the unavoidable case

    def test_cap_is_honoured_when_subdivision_is_possible(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import stream_tiles

        # Same dense cluster, but a min_tile_size_m that lets the planner cut down to
        # it. Now the cap must actually hold, with no warning.
        rng = np.random.default_rng(11)
        xyz = np.vstack(
            [
                rng.normal(15, 0.5, size=(120_000, 3)),
                rng.uniform(0, 60, size=(8_000, 3)),
            ]
        )
        path = _write_las_file(tmp_path / "dense_fine.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 0.25
        cfg.tiling.overlap_margin_m = 0.0
        cfg.tiling.max_points_per_tile = 40_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        tiles = list(stream_tiles(path, "las", cfg))

        assert tiles
        largest = max(t.xyz.shape[0] for t in tiles)
        assert largest <= cfg.tiling.max_points_per_tile


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestHeaderIsUntrusted:
    """Header bounds decide which points are ever read, so they must be checked."""

    @staticmethod
    def _cfg() -> PipelineConfig:
        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 0.0
        cfg.tiling.max_points_per_tile = 5_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"
        return cfg

    def test_stale_header_does_not_silently_drop_points(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(7)
        xyz = rng.uniform(0, 40, size=(30_000, 3))
        path = _write_las_file(tmp_path / "stale.las", xyz)
        # Header claims half the true X/Y extent. Measured before the fix:
        # 9,966 of 40,000 points streamed, 75.1% silently dropped, exit 0.
        _patch_las_header_bounds(path, [0, 0, 0], [20, 20, 40])

        with pytest.warns(RuntimeWarning, match="stale|do not contain"):
            tiles = list(stream_tiles(path, "las", self._cfg()))

        streamed = sum(t.xyz.shape[0] for t in tiles)
        assert streamed >= 30_000 * 0.99

    def test_inverted_header_bounds_raise(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(8)
        xyz = rng.uniform(0, 40, size=(5_000, 3))
        path = _write_las_file(tmp_path / "inverted.las", xyz)
        _patch_las_header_bounds(path, [40, 40, 40], [0, 0, 0])

        # Previously: zero tiles, empty detections file, exit code 0.
        with pytest.raises(ValueError, match="inverted"):
            list(stream_tiles(path, "las", self._cfg()))

    def test_non_finite_header_bounds_raise(self, tmp_path: Path) -> None:
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(9)
        xyz = rng.uniform(0, 40, size=(5_000, 3))
        path = _write_las_file(tmp_path / "nan.las", xyz)
        _patch_las_header_bounds(path, [float("nan")] * 3, [40, 40, 40])

        with pytest.raises(ValueError, match="NaN or inf"):
            list(stream_tiles(path, "las", self._cfg()))


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestStreamedTileFrames:
    """A tile's extent and its points must describe the same coordinate frame."""

    @staticmethod
    def _stream(tmp_path: Path, frame: str):
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(9)
        xyz = rng.uniform(0, 30, size=(40_000, 3))
        path = _write_las_file(tmp_path / f"frame_{frame}.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        cfg.tiling.overlap_margin_m = 0.0
        cfg.tiling.max_points_per_tile = 8_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = frame
        return list(stream_tiles(path, "las", cfg))

    @pytest.mark.parametrize("frame", ["z-up", "y-up", "colmap", "x-up", "livox"])
    def test_points_lie_inside_their_own_extent(
        self, tmp_path: Path, frame: str
    ) -> None:
        # Extents were planned in file space while preprocess rotated the points, so
        # measured 4/4 tiles had their own points outside their own extent.
        tiles = self._stream(tmp_path, frame)

        assert tiles
        for tile in tiles:
            inside = np.all(
                (tile.xyz >= tile.extent_min - 1e-6)
                & (tile.xyz <= tile.extent_max + 1e-6),
                axis=1,
            )
            assert inside.all(), f"{frame}: points outside extent"

    def test_auto_frame_is_rejected(self, tmp_path: Path) -> None:
        # _auto_align estimates the frame from whatever points it is given, which in
        # streaming mode is one tile at a time — so every tile lands in a different
        # frame and global NMS compares incompatible boxes.
        with pytest.raises(ValueError, match="auto"):
            self._stream(tmp_path, "auto")


def _write_e57_file(path: Path, xyz: np.ndarray) -> Path:
    # Local import: the skipif mark above has already gated this helper.
    import pye57

    """Write a minimal single-scan E57 with the given cartesian coordinates."""
    e57 = pye57.E57(str(path), mode="w")
    e57.write_scan_raw(
        {
            "cartesianX": np.ascontiguousarray(xyz[:, 0], dtype=np.float64),
            "cartesianY": np.ascontiguousarray(xyz[:, 1], dtype=np.float64),
            "cartesianZ": np.ascontiguousarray(xyz[:, 2], dtype=np.float64),
        }
    )
    del e57  # pye57 writes the trailer on destruction
    return path


@pytest.mark.skipif(not PYE57, reason="pye57 not installed")
class TestE57ChunkedRead:
    """The E57 chunked read is the most destructive bug this module had.

    ``header.points.reader(buffers)`` was constructed inside the block loop, so each
    iteration got a fresh reader positioned at offset 0 while the loop counter
    advanced towards the declared total. A 50,000-point file read at chunk=10,000
    returned 50,000 points of which only 10,000 were unique — 40,000 real points
    were never read, and the caller saw a full-looking result. DEFAULT_READ_CHUNK is
    5,000,000, so every E57 large enough to stream was affected.

    Nothing guarded this: the fix shipped with no E57 test at all, on a module whose
    whole purpose is reading files too large to check by eye.
    """

    EXTENT = 30.0
    N = 50_000

    def _fixture(self, tmp_path: Path) -> tuple[Path, np.ndarray]:
        rng = np.random.default_rng(5)
        # Unique coordinates, so a duplicate is detectable rather than plausible.
        xyz = rng.uniform(0, self.EXTENT, size=(self.N, 3)).astype(np.float64)
        return _write_e57_file(tmp_path / "chunked.e57", xyz), xyz

    @pytest.mark.parametrize("chunk", [50_000, 20_000, 10_000, 5_000])
    def test_chunked_read_returns_every_point_exactly_once(
        self, tmp_path: Path, chunk: int
    ) -> None:
        from pointcloud_detect.streaming import read_region_e57

        path, _ = self._fixture(tmp_path)
        lo = np.array([-1.0, -1.0, -1.0])
        hi = np.full(3, self.EXTENT + 1.0)

        pc = read_region_e57(path, lo, hi, chunk=chunk)

        assert pc.num_points == self.N, (
            f"chunk={chunk}: read {pc.num_points} of {self.N} points"
        )
        unique = len(np.unique(pc.xyz, axis=0))
        assert unique == self.N, (
            f"chunk={chunk}: {self.N - unique} duplicate points — the reader is "
            "being re-created per block and re-reading from offset 0"
        )

    def test_chunk_size_does_not_change_the_result(self, tmp_path: Path) -> None:
        """Chunking is an I/O detail and must not be observable in the output."""
        from pointcloud_detect.streaming import read_region_e57

        path, _ = self._fixture(tmp_path)
        lo = np.array([-1.0, -1.0, -1.0])
        hi = np.full(3, self.EXTENT + 1.0)

        whole = read_region_e57(path, lo, hi, chunk=self.N)
        chunked = read_region_e57(path, lo, hi, chunk=4_096)

        assert np.array_equal(
            np.unique(whole.xyz, axis=0), np.unique(chunked.xyz, axis=0)
        )

    def test_header_bounds_span_the_whole_file(self, tmp_path: Path) -> None:
        # The header-bounds fast path was dead code: np.array([...FloatNode...])
        # raises TypeError, swallowed by a bare except, so bounds came from only the
        # first chunk and every point beyond it fell outside all planned extents.
        from pointcloud_detect.streaming import scan_bounds_e57

        path, xyz = self._fixture(tmp_path)

        mins, maxs, count = scan_bounds_e57(path)

        assert count == self.N
        assert np.all(mins <= xyz.min(axis=0) + 1e-6)
        assert np.all(maxs >= xyz.max(axis=0) - 1e-6)
        # And the span is the real extent, not one chunk's worth of it.
        assert np.all((maxs - mins) > self.EXTENT * 0.9)

    def test_region_read_excludes_points_outside_it(self, tmp_path: Path) -> None:
        """Guards against 'fixing' duplication by ignoring the region filter."""
        from pointcloud_detect.streaming import read_region_e57

        path, xyz = self._fixture(tmp_path)
        lo = np.zeros(3)
        hi = np.full(3, self.EXTENT / 2.0)

        pc = read_region_e57(path, lo, hi, chunk=7_000)

        expected = int(np.count_nonzero(np.all((xyz >= lo) & (xyz <= hi), axis=1)))
        assert pc.num_points == expected
        assert np.all(pc.xyz >= lo - 1e-6) and np.all(pc.xyz <= hi + 1e-6)


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestStreamingMatchesWholeLoad:
    """The streaming module's central claim: same tiles, without holding the cloud."""

    def test_streamed_points_match_the_whole_cloud(self, tmp_path: Path) -> None:
        from pointcloud_detect.io_readers import read_point_cloud
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(23)
        xyz = rng.uniform(0, 50, size=(50_000, 3))
        path = _write_las_file(tmp_path / "equiv.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 0.0  # a partition, so the sets are comparable
        cfg.tiling.max_points_per_tile = 8_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        whole = read_point_cloud(path)
        streamed = np.concatenate([t.xyz for t in stream_tiles(path, "las", cfg)])

        # Same point set, order-independent. Round to the LAS scale (0.001) so
        # quantisation does not make identical points compare unequal.
        a = {tuple(p) for p in np.round(whole.xyz, 3)}
        b = {tuple(p) for p in np.round(streamed, 3)}
        assert b == a

    def test_pipeline_scene_bounds_use_the_detection_frame(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Asserting on tile.xyz alone would prove nothing: tiles were always
        # transformed. The bug was that _run_streaming passed FILE-frame header
        # bounds into postprocess alongside Z-up detections, so
        # normalize_detections computed (center - scene_min) / extent across two
        # frames and metadata.scene_bounds described a different space than the
        # detections beside it. Drive the real path with a stub backend.
        from pointcloud_detect import pipeline as pipeline_mod

        class _NoDetections:
            def detect(self, tile):  # noqa: ANN001, ANN202
                return []

        monkeypatch.setattr(
            pipeline_mod,
            "create_detection_backend",
            lambda *a, **k: _NoDetections(),
        )

        rng = np.random.default_rng(29)
        xyz = rng.uniform(0, 30, size=(30_000, 3))
        path = _write_las_file(tmp_path / "bounds.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        cfg.tiling.overlap_margin_m = 0.0
        cfg.tiling.max_points_per_tile = 8_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "y-up"  # a real transform
        cfg.output.directory = tmp_path / "out"

        # Header bounds are in the FILE frame: Z in [0, 30].
        header_min = np.array([0.0, 0.0, 0.0])
        header_max = np.array([30.0, 30.0, 30.0])

        result = pipeline_mod._run_streaming(
            path, cfg, "cpu", header_min, header_max, 0.0
        )

        # Under y-up -> Z-up, Z becomes negative. If the header bounds were passed
        # through unchanged, scene_bounds_min[2] would be 0.0.
        assert result.scene_bounds_min[2] < -1.0
        assert result.scene_bounds_max[2] <= 1e-6
        assert result.tile_count > 1


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestCellLeafRouting:
    """The vectorised cell->leaf routing in Pass 2 must produce the SAME tile
    membership as the original per-leaf box scan — including guard-band overlap,
    where a boundary point legitimately belongs to several tiles. These tests pin
    the routing invariant so the optimisation cannot silently change which points
    land in which tile.
    """

    def test_each_tile_holds_exactly_its_expanded_box(self, tmp_path: Path) -> None:
        # With coord_frame z-up (no transform) a tile's points must be EXACTLY the
        # source points inside its margin-expanded extent — the predicate the old
        # per-leaf scan applied. Run WITH a non-zero margin, the case routing is most
        # likely to get wrong.
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(101)
        xyz = rng.uniform(0, 80, size=(60_000, 3))
        path = _write_las_file(tmp_path / "routing.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 3.0  # non-zero: exercises the overlap band
        cfg.tiling.max_points_per_tile = 8_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        margin = cfg.tiling.overlap_margin_m
        for tile in stream_tiles(path, "las", cfg):
            lo, hi = tile.extent_min, tile.extent_max
            elo, ehi = lo - margin, hi + margin
            # No WRONG points: every point in the tile lies inside the tile's own
            # margin-expanded box. A small quantisation tolerance covers the LAS
            # 0.001 scale. Combined with test_no_source_point_is_lost (no MISSING
            # points), this pins routing correctness: right points, nothing dropped.
            pts = tile.xyz
            assert np.all(pts >= elo - 1e-3) and np.all(pts <= ehi + 1e-3), (
                f"tile at {lo.tolist()}..{hi.tolist()} contains a point outside its "
                "own expanded box — routing assigned a point to the wrong tile"
            )

    def test_no_source_point_is_lost(self, tmp_path: Path) -> None:
        # Whatever the per-tile boundary behaviour, the UNION of all emitted tiles
        # must contain every source point at least once — routing must never drop a
        # point. (Overlap can include a boundary point in more than one tile; that is
        # allowed and separately covered.)
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(103)
        xyz = rng.uniform(0, 80, size=(40_000, 3))
        path = _write_las_file(tmp_path / "no_loss.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 2.0
        cfg.tiling.max_points_per_tile = 6_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        # Compare coverage on a coarse voxel key, computed with numpy (not Python
        # tuple sets, which are too slow for tens of thousands of points). A 0.1 m
        # voxel is far finer than any tile boundary, so it cannot mask a real dropped
        # point, but is robust to the last-digit jitter that preprocess/float paths
        # introduce (the reason the whole-cloud equivalence test rounds too).
        def vox_rows(a: np.ndarray) -> np.ndarray:
            keys = np.floor(a / 0.1).astype(np.int64)
            return np.unique(keys, axis=0)

        tile_keys = [
            np.floor(t.xyz / 0.1).astype(np.int64)
            for t in stream_tiles(path, "las", cfg)
        ]
        covered = np.unique(np.concatenate(tile_keys), axis=0)
        source = vox_rows(xyz)
        # Every source voxel is covered by some tile.
        assert covered.shape[0] == source.shape[0], (
            f"voxel coverage mismatch: {covered.shape[0]} covered vs "
            f"{source.shape[0]} source — routing may have dropped points"
        )


@pytest.mark.skipif(not LASPY, reason="laspy not installed")
class TestOnDiskExtractEquivalence:
    """Option C routes points to on-disk per-tile extracts in a single file walk
    instead of accumulating per batch in RAM. The output must be BYTE-IDENTICAL to
    the routed predicate: each tile's points must equal the source points inside its
    margin-expanded box, IN FILE ORDER. Order matters because voxel_downsample keeps
    the first point per voxel by input order — the exact invariant the file-order
    fix protects — so a test that only checked set membership would miss a reordering
    regression. These tests compare against an independent brute-force reference, so
    they do not depend on any prior implementation being correct.
    """

    @staticmethod
    def _reference_tile_points(
        xyz: np.ndarray, lo: np.ndarray, hi: np.ndarray, margin: float
    ) -> np.ndarray:
        """Points inside [lo-margin, hi+margin], in FILE order (ascending index)."""
        elo, ehi = lo - margin, hi + margin
        mask = np.all((xyz >= elo) & (xyz <= ehi), axis=1)
        # np.where returns ascending indices == file order; this is the reference the
        # extract path must reproduce byte-for-byte after LAS quantisation.
        return xyz[np.where(mask)[0]]

    @pytest.mark.parametrize("margin", [0.0, 3.0])
    def test_tile_points_match_expanded_box_in_file_order(
        self, tmp_path: Path, margin: float
    ) -> None:
        from pointcloud_detect.io_readers import read_point_cloud
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(202)
        xyz = rng.uniform(0, 80, size=(50_000, 3))
        path = _write_las_file(tmp_path / f"extract_m{margin}.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = margin
        cfg.tiling.max_points_per_tile = 7_000
        cfg.preprocessing.downsample = False  # so tile.xyz is the raw selection
        cfg.preprocessing.coord_frame = "z-up"  # no transform: tile.xyz == file coords

        # The source as the reader quantises it (LAS scale 0.001), so the comparison
        # is against the same bytes the extract holds, not the pre-write floats.
        src = read_point_cloud(path).xyz

        n_tiles = 0
        for tile in stream_tiles(path, "las", cfg):
            n_tiles += 1
            lo, hi = tile.extent_min, tile.extent_max
            ref = self._reference_tile_points(src, lo, hi, margin)
            # Same count AND same order. Round to the LAS scale so quantisation does
            # not make identical points compare unequal, but do NOT sort — order is
            # exactly what this test exists to pin.
            assert tile.xyz.shape[0] == ref.shape[0], (
                f"tile {lo.tolist()}..{hi.tolist()}: {tile.xyz.shape[0]} points vs "
                f"reference {ref.shape[0]} — extract dropped or duplicated points"
            )
            assert np.allclose(tile.xyz, ref, atol=1e-3), (
                f"tile {lo.tolist()}..{hi.tolist()}: extract point ORDER differs "
                "from file order — voxel_downsample would keep different points"
            )
        assert n_tiles > 1

    def test_channels_roundtrip_through_extracts(self, tmp_path: Path) -> None:
        # rgb + intensity must survive the raw-binary extract round-trip, aligned to
        # their points and in the same order. Write a LAS with colour + intensity.
        import laspy

        rng = np.random.default_rng(204)
        xyz = rng.uniform(0, 60, size=(30_000, 3))
        rgb = rng.integers(0, 65535, size=(30_000, 3), dtype=np.uint16)
        intensity = rng.integers(0, 65535, size=30_000, dtype=np.uint16)

        header = laspy.LasHeader(point_format=3, version="1.2")
        header.offsets = xyz.min(axis=0)
        header.scales = np.array([0.001, 0.001, 0.001])
        las = laspy.LasData(header)
        las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        las.red, las.green, las.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
        las.intensity = intensity
        path = tmp_path / "channels.las"
        las.write(str(path))

        from pointcloud_detect.streaming import stream_tiles

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 0.0
        cfg.tiling.max_points_per_tile = 6_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        # Whatever channels the READER surfaces must round-trip through the extracts,
        # aligned to their points and same row count as xyz. We assert on rgb because
        # the LAS reader surfaces it; intensity is NOT asserted here because of a
        # SEPARATE, pre-existing reader bug (iter_chunks_las derives has_rgb and
        # has_intensity by iterating header.point_format.dimension_names, which is a
        # GENERATOR — the has_rgb check consumes it past the "intensity" entry, so
        # has_intensity is always False for LAS). That bug is unrelated to Option C
        # (it predates this branch and lives in the reader, not the extract path) and
        # should be fixed on its own with `dims = list(...)`. This test only pins that
        # the extract path does not drop channels the reader DOES surface.
        saw_rgb = False
        for tile in stream_tiles(path, "las", cfg):
            if tile.xyz.shape[0] == 0:
                continue
            if tile.rgb is not None:
                saw_rgb = True
                assert tile.rgb.shape[0] == tile.xyz.shape[0]
            if tile.intensity is not None:
                assert tile.intensity.shape[0] == tile.xyz.shape[0]
        assert saw_rgb, "rgb channel was dropped by the extract round-trip"

    def test_pcd_extract_dir_env_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PCD_EXTRACT_DIR redirects the on-disk extracts to a chosen volume (e.g. a
        # large EFS/EBS mount) instead of the system temp dir. Point it at a subdir
        # and assert the extract temp dir is created there, not under system temp.
        from pointcloud_detect.streaming import stream_tiles

        extract_root = tmp_path / "extract_root"
        extract_root.mkdir()
        monkeypatch.setenv("PCD_EXTRACT_DIR", str(extract_root))

        rng = np.random.default_rng(208)
        xyz = rng.uniform(0, 50, size=(20_000, 3))
        path = _write_las_file(tmp_path / "envdir.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.max_points_per_tile = 5_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        # Iterate the tiles; while the walk is live the store's dir exists under
        # extract_root. Capture that it appeared there (not under system temp), then
        # let the generator run to completion so its finally-block cleans up.
        seen_under_root = False
        for _tile in stream_tiles(path, "las", cfg):
            if list(extract_root.glob("pcd_extracts_*")):
                seen_under_root = True
        assert seen_under_root, (
            "PCD_EXTRACT_DIR was set but no extract dir was created under it"
        )

    def test_pcd_extract_dir_missing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-existent PCD_EXTRACT_DIR must fail with a clear message up front, not
        # silently fall back or crash deep in the walk.
        from pointcloud_detect.streaming import stream_tiles

        monkeypatch.setenv("PCD_EXTRACT_DIR", str(tmp_path / "does_not_exist"))
        rng = np.random.default_rng(209)
        xyz = rng.uniform(0, 50, size=(10_000, 3))
        path = _write_las_file(tmp_path / "badenv.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.max_points_per_tile = 5_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        with pytest.raises(RuntimeError, match="PCD_EXTRACT_DIR"):
            list(stream_tiles(path, "las", cfg))

    def test_unmeasurable_disk_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The headroom check used to be skipped when disk_usage raised, so a volume
        # that could not be measured was written to without any check at all. That
        # is the one case where filling a co-tenant's disk is least predictable.
        import shutil

        from pointcloud_detect.streaming import stream_tiles

        def unmeasurable(_path: str) -> None:
            raise OSError("statfs failed")

        monkeypatch.setattr(shutil, "disk_usage", unmeasurable)
        rng = np.random.default_rng(211)
        xyz = rng.uniform(0, 50, size=(10_000, 3))
        path = _write_las_file(tmp_path / "nodisk.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.max_points_per_tile = 5_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        with pytest.raises(RuntimeError, match="disk-headroom"):
            list(stream_tiles(path, "las", cfg))

    def test_full_coverage_and_overlap_duplication_preserved(
        self, tmp_path: Path
    ) -> None:
        # Two properties the store-once + neighbor-read path must still satisfy:
        # (1) every source point appears in at least one tile (no loss), and
        # (2) with a non-zero margin, boundary points still appear in MORE than one
        #     tile (the guard-band overlap the duplicating path provided — store-once
        #     must reconstruct it, not drop it).
        from pointcloud_detect.io_readers import read_point_cloud
        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(210)
        xyz = rng.uniform(0, 80, size=(40_000, 3))
        path = _write_las_file(tmp_path / "coverage.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.overlap_margin_m = 3.0
        cfg.tiling.max_points_per_tile = 6_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        src = read_point_cloud(path).xyz
        tiles = list(stream_tiles(path, "las", cfg))

        # Coverage: union of tile voxels covers every source voxel.
        def vox(a: np.ndarray) -> np.ndarray:
            return np.unique(np.floor(a / 0.1).astype(np.int64), axis=0)

        covered = np.unique(
            np.concatenate([np.floor(t.xyz / 0.1).astype(np.int64) for t in tiles]),
            axis=0,
        )
        assert covered.shape[0] == vox(src).shape[0], "store-once dropped points"

        # Overlap: total points across tiles EXCEEDS the source count, i.e. boundary
        # points are legitimately duplicated INTO the output tiles (the guard band),
        # even though each point is STORED once on disk.
        total_in_tiles = sum(t.xyz.shape[0] for t in tiles)
        assert total_in_tiles > src.shape[0], (
            "no overlap duplication in output tiles — guard band was lost"
        )

    def test_store_once_footprint_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of store-once: peak on-disk extract bytes must be near the
        # single-copy footprint, NOT inflated by guard-band duplication (the 50 GB
        # run wrote >2x the source because every overlap point was duplicated on
        # disk). Measure peak extract-dir size during a run with a real margin.
        from pointcloud_detect.streaming import stream_tiles

        extract_root = tmp_path / "extract_root"
        extract_root.mkdir()
        monkeypatch.setenv("PCD_EXTRACT_DIR", str(extract_root))

        rng = np.random.default_rng(212)
        n = 60_000
        xyz = rng.uniform(0, 80, size=(n, 3))
        path = _write_las_file(tmp_path / "footprint.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 8.0
        cfg.tiling.overlap_margin_m = 3.0  # non-trivial overlap
        cfg.tiling.max_points_per_tile = 5_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        peak = 0
        for _tile in stream_tiles(path, "las", cfg):
            size = sum(
                p.stat().st_size
                for d in extract_root.glob("pcd_extracts_*")
                for p in d.iterdir()
            )
            peak = max(peak, size)

        # Store-once footprint ceiling: xyz(24) + idx(8) = 32 B/pt, one copy. Allow a
        # generous 1.5x for buffering/filesystem overhead — the OLD duplicating path
        # would have been 2-4x the SOURCE (well above this), so this cleanly
        # distinguishes store-once from the duplicating design.
        store_once_ceiling = int(n * 32 * 1.5)
        assert peak <= store_once_ceiling, (
            f"peak extract footprint {peak} exceeds store-once ceiling "
            f"{store_once_ceiling} — points are being duplicated on disk"
        )

    def test_read_cache_is_byte_bounded(self, tmp_path: Path) -> None:
        # The Pass-2b read cache must be bounded by BYTES, not a core count — a fixed
        # count let a few huge cores push peak RSS to ~9.9 GB on the 50 GB scene. Fill
        # the store with many large-ish cores, read them all, and assert the cache's
        # tracked bytes never exceed its budget (plus at most one just-inserted core).
        from pointcloud_detect.streaming import _TileExtractStore

        n_cores = 40
        store = _TileExtractStore(
            range(n_cores), has_rgb=False, has_intensity=False, tmp_dir=tmp_path
        )
        # ~4 MB per core (50k points x 3 float64 xyz + int64 idx).
        rng = np.random.default_rng(301)
        per_core = 50_000
        for i in range(n_cores):
            idx = np.arange(i * per_core, (i + 1) * per_core, dtype=np.int64)
            xyz = rng.uniform(0, 100, size=(per_core, 3))
            store.append(i, idx, xyz, None, None)
        store.flush_all()

        # Shrink the budget so eviction must kick in, then read every core.
        store._read_cache_max_bytes = 8 * 1024 * 1024  # 8 MB
        xd, rd, idd = np.dtype(np.float64), np.dtype(np.uint8), np.dtype(np.float32)
        max_seen = 0
        for i in range(n_cores):
            store.read_core(i, xd, rd, idd)
            max_seen = max(max_seen, store._read_cache_bytes)

        # Cache bytes stay near the budget (allow one just-inserted core's overshoot),
        # NOT growing to hold all 40 cores (~160 MB).
        one_core_bytes = per_core * (24 + 8)
        assert max_seen <= 8 * 1024 * 1024 + one_core_bytes, (
            f"read cache grew to {max_seen} bytes — not byte-bounded"
        )
        store.cleanup()

    def test_no_orphaned_extract_temp_dirs(self, tmp_path: Path) -> None:
        # The temp dir must be cleaned up on the normal exit path (a finally block
        # also covers crashes). After fully draining stream_tiles, no pcd_extracts_*
        # dir should remain under the system temp root.
        import tempfile as _tempfile

        from pointcloud_detect.streaming import stream_tiles

        rng = np.random.default_rng(206)
        xyz = rng.uniform(0, 50, size=(20_000, 3))
        path = _write_las_file(tmp_path / "cleanup.las", xyz)

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 10.0
        cfg.tiling.max_points_per_tile = 5_000
        cfg.preprocessing.downsample = False
        cfg.preprocessing.coord_frame = "z-up"

        tmp_root = Path(_tempfile.gettempdir())
        before = set(tmp_root.glob("pcd_extracts_*"))
        list(stream_tiles(path, "las", cfg))  # fully drain
        after = set(tmp_root.glob("pcd_extracts_*"))
        assert after <= before, (
            f"stream_tiles left orphaned extract temp dirs: {after - before}"
        )


class TestOccupancyMedianSplit:
    """The streaming planner places splits at the occupancy-weighted median, not the
    geometric midpoint, so it cuts through dense clusters instead of bisecting the
    empty volume around them. These tests build a ScenePass1 with a hand-crafted
    occupancy grid so the density skew is known exactly, then assert the fix.
    """

    @staticmethod
    def _scene(occupancy: np.ndarray, origin, cell_size) -> "ScenePass1":
        from pointcloud_detect.streaming import ScenePass1

        occ = occupancy.astype(np.int64)
        origin = np.asarray(origin, float)
        cell_size = np.asarray(cell_size, float)
        extent = np.array(occ.shape) * cell_size
        return ScenePass1(
            data_min=origin,
            data_max=origin + extent,
            points_read=int(occ.sum()),
            header_count=int(occ.sum()),
            occupancy=occ,
            grid_origin=origin,
            cell_size=cell_size,
        )

    def test_split_lands_on_dense_side_not_midpoint(self) -> None:
        # 100 cells along X (each 1 m), all points packed into cells 0-9 (the first
        # 10 m). The geometric midpoint is x=50; the occupancy median must land near
        # x=10, cutting through the cluster instead of the empty 50 m mark.
        from pointcloud_detect.config import PipelineConfig
        from pointcloud_detect.streaming import plan_tile_extents

        occ = np.zeros((100, 4, 4), dtype=np.int64)
        occ[0:10, :, :] = 5000  # dense slab in the first 10 m of X
        scene = self._scene(occ, origin=[0, 0, 0], cell_size=[1.0, 1.0, 1.0])

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 2.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([100.0, 4.0, 4.0])
        extents = plan_tile_extents(bmin, bmax, cfg, scene=scene, max_points=50_000)

        # Find the X boundary the top-level split produced: the smallest emax[0]
        # among leaves whose xmin is 0 (the low-X child's right edge).
        low_x_edges = sorted(
            {round(float(emax[0]), 2) for emin, emax in extents if emin[0] == 0.0}
        )
        first_cut = low_x_edges[0]
        # Occupancy median is ~10 m; geometric midpoint would be 50 m. Assert the cut
        # is far below the midpoint — decisively on the dense side.
        assert first_cut < 30.0, (
            f"first X cut at {first_cut}, expected near the 10 m cluster, not 50"
        )

    def test_occupancy_median_beats_midpoint_on_clustered_scene(self) -> None:
        # A dense blob spread across a large sparse scene is the shape that stranded
        # 558k-point tiles under midpoint splitting. The occupancy median cuts INTO
        # the blob, so it produces far fewer over-budget tiles than the midpoint
        # would. This compares the two placements directly on identical occupancy by
        # counting tiles whose point total exceeds the budget.
        from pointcloud_detect.config import PipelineConfig
        from pointcloud_detect.streaming import plan_tile_extents

        # 64 x 64 x 2 grid at 4 m cells => 256 x 256 x 8 m. A moderately dense blob
        # (16 x 16 cells = 64 x 64 m) that IS splittable above min_tile_size, so a
        # good split can bring tiles within budget where a bad one cannot.
        occ = np.zeros((64, 64, 2), dtype=np.int64)
        occ[:] = 1
        occ[0:16, 0:16, :] = 400  # blob: 16*16*2*400 = 204,800 points, splittable
        scene = self._scene(occ, origin=[0, 0, 0], cell_size=[4.0, 4.0, 4.0])

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([256.0, 256.0, 8.0])
        max_points = 50_000

        extents = plan_tile_extents(bmin, bmax, cfg, scene=scene, max_points=max_points)

        # Count how many planned tiles are actually over budget, using the same
        # occupancy the planner used. The blob (204,800 pts) split well should yield
        # per-tile counts under 50k; split badly (midpoint bisecting empty space) it
        # strands the blob in one child repeatedly.
        def region_count(emin, emax):
            shape = np.array(scene.occupancy.shape)
            i0 = np.clip(
                np.floor((emin - scene.grid_origin) / scene.cell_size).astype(int),
                0,
                shape - 1,
            )
            i1 = np.clip(
                np.ceil((emax - scene.grid_origin) / scene.cell_size).astype(int),
                0,
                shape,
            )
            sl = tuple(slice(int(a), max(int(b), int(a) + 1)) for a, b in zip(i0, i1))
            return int(scene.occupancy[sl].sum())

        over = sum(1 for emin, emax in extents if region_count(emin, emax) > max_points)
        # The blob is splittable, so a density-median plane should bring every tile
        # within budget. Allow a small margin, but it must be near zero — decisively
        # better than the dozens midpoint stranding would produce.
        assert over <= 1, (
            f"{over} tiles over budget; occupancy median should reach the blob"
        )

    def test_extents_still_tile_the_space_with_occupancy(self) -> None:
        # The median split must not create gaps or overlaps: child extents still
        # partition the parent exactly.
        from pointcloud_detect.config import PipelineConfig
        from pointcloud_detect.streaming import plan_tile_extents

        occ = np.zeros((40, 40, 40), dtype=np.int64)
        occ[5:15, 5:15, 5:15] = 3000  # off-centre cluster to skew the median
        occ[:] += 1
        scene = self._scene(occ, origin=[0, 0, 0], cell_size=[1.0, 1.0, 1.0])

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([40.0, 40.0, 40.0])
        extents = plan_tile_extents(bmin, bmax, cfg, scene=scene, max_points=50_000)

        total_vol = sum(float(np.prod(emax - emin)) for emin, emax in extents)
        assert total_vol == pytest.approx(float(np.prod(bmax - bmin)), rel=1e-6)

    def test_geometry_only_still_uses_midpoint(self) -> None:
        # With no scene (geometry-only fallback), placement must stay the geometric
        # midpoint. Every leaf boundary should fall on a midpoint grid (multiples of
        # 5 m for a 40 m cube recursing to min_tile_size 5) — never on an
        # occupancy-derived plane, since there is no occupancy to derive from.
        from pointcloud_detect.config import PipelineConfig
        from pointcloud_detect.streaming import plan_tile_extents

        cfg = PipelineConfig()
        cfg.tiling.min_tile_size_m = 5.0
        bmin = np.array([0.0, 0.0, 0.0])
        bmax = np.array([40.0, 40.0, 40.0])
        extents = plan_tile_extents(bmin, bmax, cfg)  # no scene => midpoint fallback

        # Midpoint halving of [0,40] yields boundaries at multiples of 2.5 (40/2,
        # /4, /8...). Assert every extent edge is a clean midpoint-derived value,
        # i.e. divisible by 2.5, which occupancy-median planes would not generally be.
        edges = []
        for emin, emax in extents:
            edges.extend(float(emin[a]) for a in range(3))
            edges.extend(float(emax[a]) for a in range(3))
        assert all(abs((e / 2.5) - round(e / 2.5)) < 1e-6 for e in edges), (
            "geometry-only path produced a non-midpoint boundary"
        )
