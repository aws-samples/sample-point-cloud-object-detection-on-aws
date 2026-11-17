"""Unit tests for the FastAPI backend API routes."""

import pytest
from fastapi.testclient import TestClient

from pointcloud_detect.api.local_backend import app
from pointcloud_detect.api.store import job_store


@pytest.fixture(autouse=True)
def clear_store():
    """Clear the job store before each test."""
    job_store.clear()
    yield
    job_store.clear()


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


class TestHealth:
    def test_health_endpoint(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


class TestUpload:
    def test_initiate_upload_valid(self, client: TestClient) -> None:
        response = client.post(
            "/api/upload/initiate",
            json={
                "filename": "scan.ply",
                "file_size_bytes": 50_000_000,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "upload_id" in data
        assert "s3_key" in data
        assert data["s3_key"].endswith("scan.ply")
        assert data["total_parts"] == 1
        assert len(data["part_urls"]) == 1

    def test_part_plan_allows_exactly_s3s_limit(self) -> None:
        """10,000 parts is legal, so the guard must not be off by one."""
        from pointcloud_detect.api.routes.upload import (
            S3_MAX_MULTIPART_PARTS,
            plan_part_count,
        )

        assert S3_MAX_MULTIPART_PARTS == 10_000
        # Exactly at the ceiling.
        assert plan_part_count("site.laz", 10_000 * 1_000_000, 1_000_000) == 10_000
        # And a partial final part that still lands on the ceiling.
        assert plan_part_count("site.laz", 9_999 * 1_000_000 + 1, 1_000_000) == 10_000

    def test_part_plan_rejects_a_plan_exceeding_s3s_limit(self) -> None:
        """A plan S3 cannot accept must fail up front, not partway through the upload.

        upload_part_size_bytes is configurable, so a small part size plus a large file
        plans an upload S3 will refuse. Rejecting it here costs nothing; discovering it
        from S3 costs however many parts the client had already sent.
        """
        from fastapi import HTTPException

        from pointcloud_detect.api.routes.upload import plan_part_count

        with pytest.raises(HTTPException) as exc:
            # One byte past the ceiling.
            plan_part_count("site.laz", 10_000 * 1_000_000 + 1, 1_000_000)

        assert exc.value.status_code == 400
        detail = str(exc.value.detail)
        assert "10001" in detail.replace(",", "")
        # Actionable: names the setting and a part size that would work.
        assert "upload_part_size_bytes" in detail

    def test_part_plan_is_validated_before_s3_is_touched(self) -> None:
        """The guard must run before create_multipart_upload.

        Rejecting afterwards would leave an initiated upload in the bucket that no
        client will ever complete or abort, accruing storage for its parts. Compared
        over the AST rather than the source text, so a mention of either name in a
        comment cannot satisfy or break the assertion.
        """
        import ast
        import inspect
        import textwrap

        from pointcloud_detect.api.routes import upload as upload_mod

        tree = ast.parse(textwrap.dedent(inspect.getsource(upload_mod.initiate_upload)))
        called_at: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in {"plan_part_count", "create_multipart_upload"}:
                called_at.setdefault(name, node.lineno)

        assert "plan_part_count" in called_at, "the part-count guard is not called"
        assert "create_multipart_upload" in called_at
        assert called_at["plan_part_count"] < called_at["create_multipart_upload"]

    def test_initiate_upload_preserves_filename(self, client: TestClient) -> None:
        response = client.post(
            "/api/upload/initiate",
            json={
                "filename": "my_building.e57",
                "file_size_bytes": 1_000_000,
            },
        )
        assert response.status_code == 200
        assert "my_building.e57" in response.json()["s3_key"]

    def test_complete_upload(self, client: TestClient) -> None:
        response = client.post(
            "/api/upload/complete",
            json={
                "upload_id": "test-id",
                "s3_key": "uploads/test-id/scan.ply",
                "parts": [{"part_number": 1, "etag": "abc123"}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "scan.ply"
        assert data["s3_key"] == "uploads/test-id/scan.ply"


class TestJobs:
    def test_create_job(self, client: TestClient) -> None:
        response = client.post(
            "/api/jobs",
            json={
                "s3_key": "uploads/test-id/scan.ply",
                "filename": "scan.ply",
                "config": {"model_mode": "indoor", "confidence_threshold": 0.7},
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "queued"
        assert data["filename"] == "scan.ply"
        assert data["config"]["model_mode"] == "indoor"
        assert data["config"]["confidence_threshold"] == 0.7
        assert "job_id" in data

    def test_list_jobs_empty(self, client: TestClient) -> None:
        response = client.get("/api/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["jobs"] == []
        assert data["total"] == 0

    def test_list_jobs_returns_created(self, client: TestClient) -> None:
        client.post(
            "/api/jobs",
            json={
                "s3_key": "uploads/id1/a.ply",
                "filename": "a.ply",
            },
        )
        client.post(
            "/api/jobs",
            json={
                "s3_key": "uploads/id2/b.ply",
                "filename": "b.ply",
            },
        )
        response = client.get("/api/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["jobs"]) == 2

    def test_get_job_not_found(self, client: TestClient) -> None:
        response = client.get("/api/jobs/nonexistent-id")
        assert response.status_code == 404

    def test_get_job_by_id(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/jobs",
            json={
                "s3_key": "uploads/id1/scan.ply",
                "filename": "scan.ply",
            },
        )
        job_id = create_resp.json()["job_id"]

        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        assert response.json()["job_id"] == job_id
        assert response.json()["filename"] == "scan.ply"

    def test_update_job_status(self, client: TestClient) -> None:
        create_resp = client.post(
            "/api/jobs",
            json={
                "s3_key": "uploads/id1/scan.ply",
                "filename": "scan.ply",
            },
        )
        job_id = create_resp.json()["job_id"]

        # Update to complete
        response = client.put(
            f"/api/jobs/{job_id}/status",
            params={
                "status": "complete",
                "detection_count": 5,
                "processing_time_s": 2.3,
            },
        )
        assert response.status_code == 200

        # Verify
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "complete"
        assert job["detection_count"] == 5
        assert job["processing_time_s"] == 2.3


class TestResults:
    MISSING = "7f3e9a52-6c1d-4b8e-9f0a-2d4c6e8a0b1c"

    def test_detections_not_found(self, client: TestClient) -> None:
        response = client.get(f"/api/results/{self.MISSING}/detections")
        assert response.status_code == 404

    def test_tile_index_not_found(self, client: TestClient) -> None:
        response = client.get(f"/api/results/{self.MISSING}/tiles/index")
        assert response.status_code == 404

    def test_tile_not_found(self, client: TestClient) -> None:
        response = client.get(f"/api/results/{self.MISSING}/tiles/overview")
        assert response.status_code == 404

    def test_tile_segmentation_not_found(self, client: TestClient) -> None:
        response = client.get(
            f"/api/results/{self.MISSING}/tiles/overview/segmentation"
        )
        assert response.status_code == 404


class TestLocalBackendExposure:
    """T33: the local backend is reachable by pages the developer visits."""

    @pytest.fixture
    def data_root(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        """Point the backend at a scratch tree with a file one level above tiles/."""
        from pointcloud_detect.api import local_backend

        tiles = tmp_path / "tiles"
        output = tmp_path / "output"
        upload = tmp_path / "input"
        tiles.mkdir()
        output.mkdir()
        upload.mkdir()
        # What `..` reached: fixed names in local_dev_data/ itself.
        (tmp_path / "index.json").write_text('{"secret": true}')
        (tmp_path / "detections.json").write_text('{"secret": true}')
        monkeypatch.setattr(local_backend, "TILES_DIR", tiles)
        monkeypatch.setattr(local_backend, "OUTPUT_DIR", output)
        monkeypatch.setattr(local_backend, "INPUT_DIR", upload)
        return tmp_path

    @pytest.mark.parametrize(
        "path",
        [
            "/api/results/../tiles/index",
            "/api/results/%2E%2E/tiles/index",
            "/api/results/%2e%2e/detections",
            "/api/results/..%5C..%5Cx/tiles/index",
            "/api/results/not-a-uuid/tiles/index",
        ],
    )
    def test_job_id_traversal_is_refused(
        self, client: TestClient, data_root, path: str
    ) -> None:
        r = client.get(path)
        assert r.status_code in (404, 422)
        assert "secret" not in r.text

    @pytest.mark.parametrize("tile_id", ["..", "%2E%2E", "..%5Cindex", "a.b", "x y"])
    def test_tile_id_traversal_is_refused(
        self, client: TestClient, data_root, tile_id: str
    ) -> None:
        job = TestResults.MISSING
        (data_root / "tiles" / job).mkdir()
        for path in (
            f"/api/results/{job}/tiles/{tile_id}",
            f"/api/results/{job}/tiles/{tile_id}/segmentation",
        ):
            r = client.get(path)
            assert r.status_code in (404, 422), path
            assert "secret" not in r.text

    def test_a_real_tile_is_served(self, client: TestClient, data_root) -> None:
        job = TestResults.MISSING
        (data_root / "tiles" / job).mkdir()
        (data_root / "tiles" / job / "detail_0001.bin").write_bytes(b"\x01\x02")
        r = client.get(f"/api/results/{job}/tiles/detail_0001")
        assert r.status_code == 200
        assert r.content == b"\x01\x02"

    @pytest.mark.parametrize(
        "path",
        [
            "/api/upload/file/%2E%2E/x.las",
            "/api/upload/file/not-a-uuid/x.las",
            f"/api/upload/file/{TestResults.MISSING}/%2E%2E",
            f"/api/upload/file/{TestResults.MISSING}/..%5Cx.las",
        ],
    )
    def test_upload_path_traversal_is_refused(
        self, client: TestClient, data_root, path: str
    ) -> None:
        r = client.put(path)
        assert r.status_code in (404, 422), path
        # Nothing was created outside, or beside, a job's own directory.
        assert sorted(p.name for p in data_root.iterdir()) == [
            "detections.json",
            "index.json",
            "input",
            "output",
            "tiles",
        ]
        assert list((data_root / "input").iterdir()) == []

    def test_a_real_upload_is_accepted(self, client: TestClient, data_root) -> None:
        job = TestResults.MISSING
        r = client.put(f"/api/upload/file/{job}/scan.las")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert (data_root / "input" / job).is_dir()

    def test_no_wildcard_origin(self) -> None:
        from fastapi.middleware.cors import CORSMiddleware

        from pointcloud_detect.api.local_backend import LOCAL_DEV_ORIGINS

        (cors,) = [m for m in app.user_middleware if m.cls is CORSMiddleware]
        assert cors.kwargs["allow_origins"] is LOCAL_DEV_ORIGINS
        origins = LOCAL_DEV_ORIGINS
        assert "*" not in origins
        assert "http://localhost:5173" in origins
        assert all(
            o.startswith(("http://localhost:", "http://127.0.0.1:")) for o in origins
        )

    def test_a_foreign_page_gets_no_cors_grant(self, client: TestClient) -> None:
        r = client.get("/api/jobs", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers

    def test_the_dev_server_gets_one(self, client: TestClient) -> None:
        r = client.get("/api/jobs", headers={"Origin": "http://localhost:5173"})
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
