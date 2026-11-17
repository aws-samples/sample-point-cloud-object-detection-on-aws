"""Tests for job-store backend selection and the colour-pipeline introspection.

These pin two failure modes that are silent rather than loud:

1. An unrecognised ``JOB_STORE_BACKEND`` used to fall through to the in-memory store.
   In a multi-replica deployment that is the exact bug the DynamoDB store exists to
   fix — the dashboard flickers and jobs stick in "processing" — so a typo must fail
   loudly instead of quietly degrading.
2. Colour scaling is decided by inspecting the model's test pipeline. Getting the
   introspection wrong double-normalizes colour to ~0 (or leaves it 255x too large),
   and in both cases the model still runs and simply returns poor or zero detections,
   which no exception would reveal.
"""

from __future__ import annotations

import numpy as np
import pytest

from pointcloud_detect.api.store import (
    _DEFAULT_JOBS_TTL_DAYS,
    InMemoryJobStore,
    _build_store,
    _jobs_ttl_days,
)
from pointcloud_detect.backends.mmdet3d import (
    _pipeline_has,
    _prepare_points_for_detection,
)
from pointcloud_detect.models import Tile


class TestJobStoreBackendSelection:
    def test_default_is_in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOB_STORE_BACKEND", raising=False)
        assert isinstance(_build_store(), InMemoryJobStore)

    def test_memory_is_explicitly_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JOB_STORE_BACKEND", "memory")
        assert isinstance(_build_store(), InMemoryJobStore)

    @pytest.mark.parametrize("value", ["dynamo", "ddb", "sqlite", "none", "dynamo db"])
    def test_unknown_backend_raises_instead_of_falling_back(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # "dynamo" and "ddb" are the interesting ones: they are near-misses for a real
        # backend, so a silent fallback would look like a working dynamodb deployment
        # while actually running the process-local dict.
        monkeypatch.setenv("JOB_STORE_BACKEND", value)
        with pytest.raises(RuntimeError, match="not a recognised job store backend"):
            _build_store()

    def test_case_and_whitespace_are_tolerated_for_valid_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JOB_STORE_BACKEND", "  MEMORY  ")
        assert isinstance(_build_store(), InMemoryJobStore)

    @pytest.mark.parametrize("value", ["DynamoDB ", " dynamodb", "DYNAMODB"])
    def test_case_and_whitespace_normalize_onto_dynamodb(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # These must normalize onto the real backend rather than being rejected as
        # unknown. Asserted via the table-name error because reaching it proves the
        # backend was recognised, without constructing a boto3 resource in CI.
        monkeypatch.setenv("JOB_STORE_BACKEND", value)
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        with pytest.raises(RuntimeError, match="JOBS_TABLE_NAME"):
            _build_store()

    def test_dynamodb_without_table_name_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JOB_STORE_BACKEND", "dynamodb")
        monkeypatch.delenv("JOBS_TABLE_NAME", raising=False)
        with pytest.raises(RuntimeError, match="JOBS_TABLE_NAME"):
            _build_store()


class TestInMemoryDelete:
    """T60: the store interface gained delete; the in-memory store implements it."""

    @staticmethod
    def _job(job_id: str):
        from datetime import UTC, datetime

        from pointcloud_detect.api.models import JobConfigRequest, JobResponse

        return JobResponse(
            job_id=job_id,
            filename="scene.las",
            s3_input_key=f"uploads/{job_id}/scene.las",
            config=JobConfigRequest(),
            status="complete",  # pyright: ignore[reportArgumentType]
            created_at=datetime.now(UTC),
        )

    def test_delete_removes_only_that_job(self) -> None:
        store = InMemoryJobStore()
        store.create(self._job("a"))
        store.create(self._job("b"))
        store.delete("a")
        assert store.get("a") is None
        assert [j.job_id for j in store.list()] == ["b"]

    def test_deleting_an_unknown_id_is_a_noop(self) -> None:
        store = InMemoryJobStore()
        store.delete("missing")
        assert store.list() == []


class TestJobsTtlDays:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOBS_TTL_DAYS", raising=False)
        assert _jobs_ttl_days() == _DEFAULT_JOBS_TTL_DAYS

    def test_explicit_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBS_TTL_DAYS", "7")
        assert _jobs_ttl_days() == 7

    def test_zero_disables_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBS_TTL_DAYS", "0")
        assert _jobs_ttl_days() == 0

    @pytest.mark.parametrize("bad", ["", "   ", "ninety", "-5"])
    def test_invalid_falls_back_to_default_not_to_disabled(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        # Falling back to 0 would disable expiry, which is the unbounded-growth
        # failure this setting exists to prevent — so bad input keeps the default.
        monkeypatch.setenv("JOBS_TTL_DAYS", bad)
        assert _jobs_ttl_days() == _DEFAULT_JOBS_TTL_DAYS


class TestPipelineIntrospection:
    def test_finds_top_level_transform(self) -> None:
        pipeline = [
            {"type": "LoadPointsFromFile", "load_dim": 6},
            {"type": "NormalizePointsColor", "color_mean": None},
        ]
        assert _pipeline_has(pipeline, "NormalizePointsColor") is True

    def test_finds_nested_transform(self) -> None:
        # MultiScaleFlipAug3D nests its stages, so a top-level-only scan would miss
        # this and colour would be divided twice.
        pipeline = [
            {"type": "LoadPointsFromFile", "load_dim": 6},
            {
                "type": "MultiScaleFlipAug3D",
                "transforms": [
                    {"type": "GlobalRotScaleTrans"},
                    {"type": "NormalizePointsColor", "color_mean": None},
                ],
            },
        ]
        assert _pipeline_has(pipeline, "NormalizePointsColor") is True

    def test_absent_transform_reports_false(self) -> None:
        pipeline = [
            {"type": "LoadPointsFromFile", "load_dim": 4},
            {"type": "Pack3DDetInputs"},
        ]
        assert _pipeline_has(pipeline, "NormalizePointsColor") is False

    @pytest.mark.parametrize("empty", [None, [], {}, ()])
    def test_tolerates_empty_or_missing_pipeline(self, empty) -> None:
        assert _pipeline_has(empty, "NormalizePointsColor") is False


class TestPointPreparationColorScaling:
    """Both inference paths must take the same color-scaling decision.

    ``detect()`` builds its own points array; ``segment()`` goes through
    ``_prepare_points_for_detection``. The helper previously hardcoded raw 0-255,
    so a fix applied only to ``detect()`` would leave segmentation wrong.
    """

    @staticmethod
    def _tile() -> Tile:
        return Tile(
            xyz=np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
            rgb=np.array([[255, 128, 0], [0, 64, 255]], dtype=np.uint8),
        )

    def test_pipeline_normalizes_so_color_is_passed_raw(self) -> None:
        points = _prepare_points_for_detection(
            self._tile(), num_dims=6, has_rgb=True, pipeline_normalizes_color=True
        )
        np.testing.assert_allclose(points[:, 3:6], [[255, 128, 0], [0, 64, 255]])

    def test_pipeline_does_not_normalize_so_color_is_scaled_here(self) -> None:
        points = _prepare_points_for_detection(
            self._tile(), num_dims=6, has_rgb=True, pipeline_normalizes_color=False
        )
        np.testing.assert_allclose(
            points[:, 3:6],
            np.array([[255, 128, 0], [0, 64, 255]], dtype=np.float32) / 255.0,
        )

    def test_color_is_never_double_normalized_to_near_zero(self) -> None:
        # The FCAF3D regression: dividing in both places drove color to ~0.5/255.
        points = _prepare_points_for_detection(
            self._tile(), num_dims=6, has_rgb=True, pipeline_normalizes_color=True
        )
        assert points[:, 3:6].max() > 1.0

    def test_no_rgb_leaves_color_columns_zero(self) -> None:
        tile = Tile(xyz=np.array([[1.0, 2.0, 3.0]]))
        points = _prepare_points_for_detection(
            tile, num_dims=6, has_rgb=False, pipeline_normalizes_color=False
        )
        assert points.shape == (1, 6)
        np.testing.assert_allclose(points[:, 3:6], 0.0)

    def test_xyz_only_model_gets_three_columns(self) -> None:
        points = _prepare_points_for_detection(
            self._tile(), num_dims=3, has_rgb=True, pipeline_normalizes_color=False
        )
        assert points.shape == (2, 3)
