"""Property-based tests using Hypothesis.

Tests invariants that must hold for all valid inputs:
- Serialization round-trips
- NMS never adds detections
- AABB always contains center
- Downsample never adds points
- Train/val split preserves all data
"""

import numpy as np
import pytest

# Must be importorskip, not a try/except that sets a flag for pytest.mark.skipif.
# `@given(...)` below is evaluated at class-body scope during import, so a missing
# hypothesis raised NameError at collection time and errored the module out before any
# skip mark was consulted -- the guard looked like it degraded gracefully and did not.
# importorskip raises Skipped during collection, which pytest honours.
pytest.importorskip("hypothesis", reason="hypothesis not installed")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.extra.numpy import arrays  # noqa: E402

from pointcloud_detect.models import Detection  # noqa: E402
from pointcloud_detect.postprocessing import (  # noqa: E402
    compute_aabb,
    filter_by_confidence,
    nms_3d,
)
from pointcloud_detect.preprocessing import voxel_downsample  # noqa: E402

# --- Strategies ---


def detection_strategy():
    """Generate random valid Detection objects."""
    return st.builds(
        Detection,
        label=st.sampled_from(["door", "window", "wall", "chair", "table"]),
        confidence=st.floats(min_value=0.0, max_value=1.0),
        center=arrays(
            np.float64,
            (3,),
            elements=st.floats(-100, 100, allow_nan=False, allow_infinity=False),
        ),
        size=arrays(
            np.float64,
            (3,),
            elements=st.floats(0.01, 10.0, allow_nan=False, allow_infinity=False),
        ),
        heading=st.floats(min_value=0.0, max_value=2 * np.pi),
    )


# --- Property Tests ---


class TestNMSProperties:
    @given(st.lists(detection_strategy(), min_size=0, max_size=20))
    @settings(max_examples=50, deadline=None)
    def test_nms_never_adds_detections(self, detections: list) -> None:
        """NMS output count <= input count."""
        # Compute AABBs first (required for NMS)
        for det in detections:
            compute_aabb(det)
        result = nms_3d(detections, iou_threshold=0.3)
        assert len(result) <= len(detections)

    @given(st.lists(detection_strategy(), min_size=0, max_size=20))
    @settings(max_examples=50, deadline=None)
    def test_nms_preserves_highest_confidence(self, detections: list) -> None:
        """The highest-confidence detection is always kept."""
        if not detections:
            return
        for det in detections:
            compute_aabb(det)
        result = nms_3d(detections, iou_threshold=0.3)
        max_conf = max(d.confidence for d in detections)
        assert any(d.confidence == max_conf for d in result)


class TestAABBProperties:
    @given(detection_strategy())
    @settings(max_examples=100, deadline=None)
    def test_aabb_contains_center(self, det: Detection) -> None:
        """AABB min <= center <= AABB max for all axes."""
        det = compute_aabb(det)
        assert det.aabb_min is not None
        assert det.aabb_max is not None
        for i in range(3):
            assert det.aabb_min[i] <= det.center[i] + 1e-6
            assert det.aabb_max[i] >= det.center[i] - 1e-6

    @given(detection_strategy())
    @settings(max_examples=100, deadline=None)
    def test_aabb_non_negative_volume(self, det: Detection) -> None:
        """AABB always has non-negative volume."""
        det = compute_aabb(det)
        assert det.aabb_min is not None
        assert det.aabb_max is not None
        volume = np.prod(det.aabb_max - det.aabb_min)
        assert volume >= 0


class TestDownsampleProperties:
    @given(
        arrays(
            np.float64,
            st.tuples(st.integers(10, 1000), st.just(3)),
            elements=st.floats(-50, 50, allow_nan=False, allow_infinity=False),
        ),
        st.floats(min_value=0.01, max_value=5.0),
    )
    @settings(max_examples=30, deadline=None)
    def test_downsample_never_adds_points(
        self, xyz: np.ndarray, voxel_size: float
    ) -> None:
        """Downsampling always reduces or maintains point count."""
        indices = voxel_downsample(xyz, voxel_size)
        assert len(indices) <= len(xyz)
        assert len(indices) > 0

    @given(
        arrays(
            np.float64,
            st.tuples(st.integers(10, 500), st.just(3)),
            elements=st.floats(-50, 50, allow_nan=False, allow_infinity=False),
        ),
        st.floats(min_value=0.01, max_value=5.0),
    )
    @settings(max_examples=30, deadline=None)
    def test_downsample_indices_valid(self, xyz: np.ndarray, voxel_size: float) -> None:
        """All returned indices are valid for the input array."""
        indices = voxel_downsample(xyz, voxel_size)
        assert indices.max() < len(xyz)
        assert indices.min() >= 0


class TestConfidenceFilterProperties:
    @given(
        st.lists(detection_strategy(), min_size=0, max_size=30),
        st.floats(min_value=0.0, max_value=1.0),
    )
    @settings(max_examples=50, deadline=None)
    def test_filter_respects_threshold(
        self, detections: list, threshold: float
    ) -> None:
        """All detections in output have confidence >= threshold."""
        result = filter_by_confidence(detections, threshold)
        assert all(d.confidence >= threshold for d in result)
        assert len(result) <= len(detections)


class TestSplitProperties:
    @given(
        st.integers(min_value=2, max_value=50), st.floats(min_value=0.05, max_value=0.5)
    )
    @settings(max_examples=30, deadline=None)
    def test_split_preserves_all_data(self, n: int, val_split: float) -> None:
        """Train + val == total (no data loss)."""
        from pointcloud_detect.training.data_loader import split_train_val

        data = [{"scene_id": f"s{i}"} for i in range(n)]
        train, val = split_train_val(data, val_split=val_split, seed=42)
        assert len(train) + len(val) == n

    @given(st.integers(min_value=5, max_value=50))
    @settings(max_examples=20, deadline=None)
    def test_split_deterministic(self, n: int) -> None:
        """Same seed produces same split."""
        from pointcloud_detect.training.data_loader import split_train_val

        data = [{"scene_id": f"s{i}"} for i in range(n)]
        t1, v1 = split_train_val(data, val_split=0.2, seed=123)
        t2, v2 = split_train_val(data, val_split=0.2, seed=123)
        assert [d["scene_id"] for d in t1] == [d["scene_id"] for d in t2]
