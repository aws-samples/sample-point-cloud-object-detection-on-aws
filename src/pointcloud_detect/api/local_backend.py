"""Local filesystem backend for development without AWS/Docker.

Replaces S3 with local directories and SQS with an in-process queue.
Run with:
    uv run uvicorn pointcloud_detect.api.local_backend:app --host 127.0.0.1 \
        --port 8000 --reload

Keep it on 127.0.0.1. Like the AWS-mode API it has no authentication of its own, and
here nothing sits in front of it, so any machine that can reach the port can read and
rewrite every local job (T33).
"""

import json
import logging
import re
import shutil
import uuid
from collections import deque
from datetime import UTC
from pathlib import Path
from threading import Thread

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from pointcloud_detect.api.models import (
    TILE_ID_PATTERN,
    JobCreateRequest,
    JobListResponse,
    JobResponse,
    JobStatus,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitiateRequest,
    UploadInitiateResponse,
)
from pointcloud_detect.api.store import job_store

logger = logging.getLogger(__name__)

# Local storage directories
LOCAL_DATA = Path("./local_dev_data")
INPUT_DIR = LOCAL_DATA / "input"
OUTPUT_DIR = LOCAL_DATA / "output"
TILES_DIR = LOCAL_DATA / "tiles"

for d in [INPUT_DIR, OUTPUT_DIR, TILES_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# In-process job queue (replaces SQS)
_job_queue: deque[dict] = deque()

# The dev servers that call this backend from a browser: Vite's (npm run dev, :5173)
# and the compose frontend's port (:3000). Vite proxies /api, so only direct calls
# such as the local upload URL below are cross-origin at all. This was "*", which let
# any page the developer visited drive the backend (T33).
LOCAL_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app = FastAPI(title="Point Cloud Detection API (Local Dev)", version="0.1.0-local")
app.add_middleware(
    CORSMiddleware,
    allow_origins=LOCAL_DEV_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    # The SPA's headers; X-PCD-Client is api/app.py CLIENT_HEADER.
    allow_headers=["Content-Type", "X-PCD-Client"],
)

_TILE_ID = re.compile(TILE_ID_PATTERN)


def _job_dir(root: Path, job_id: str) -> Path:
    """``root/job_id``, refusing an id that is not a single plain path segment.

    These routes join path parameters onto a directory. A job id of ``..`` reached
    ``local_dev_data/`` itself (T33). Job ids are UUIDs, so anything else is refused.
    """
    try:
        canonical = str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(422, "job_id must be a UUID") from None
    return root / canonical


def _file_name(filename: str) -> str:
    """A bare file name, refusing ``..``, ``.`` and separators (as models.py does)."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(422, "filename must be a bare file name with no path")
    return filename


def _local_input_path(s3_key: str) -> Path:
    """The file an input key names: ``INPUT_DIR/<upload id>/<file name>``, nothing else.

    The API refuses keys with traversal segments (validate_input_key) before a job is
    queued. The worker checks again where the key becomes a path, so whatever reaches
    the queue it reads nothing outside INPUT_DIR: one UUID and one plain file name,
    resolved, symlinks included, and required to stay inside INPUT_DIR.
    """
    parts = s3_key.split("/")
    try:
        if len(parts) != 3 or parts[0] != "uploads":
            raise ValueError
        upload_id = str(uuid.UUID(parts[1]))
        name = parts[2]
        if not name or "\\" in name or name in (".", ".."):
            raise ValueError
    except ValueError:
        raise ValueError(f"not a local upload key: {s3_key!r}") from None
    root = INPUT_DIR.resolve()
    path = (root / upload_id / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"not a local upload key: {s3_key!r}")
    return path


def _tile_name(tile_id: str) -> str:
    """A tile id, refusing path separators, ``..`` and anything else unusual."""
    if not _TILE_ID.fullmatch(tile_id):
        raise HTTPException(422, "tile_id must be a plain tile name")
    return tile_id


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "local_dev"}


# --- Upload (simplified: direct file write, no multipart needed locally) ---


@app.post("/api/upload/initiate", response_model=UploadInitiateResponse)
async def initiate_upload(request: UploadInitiateRequest):
    """For local dev, returns a dummy upload session. Use /api/upload/file instead."""
    job_id = str(uuid.uuid4())
    s3_key = f"uploads/{job_id}/{request.filename}"
    # In local mode, frontend can POST the file directly to /api/upload/file
    return UploadInitiateResponse(
        upload_id=job_id,
        s3_key=s3_key,
        part_size_bytes=request.file_size_bytes,  # Single part
        total_parts=1,
        part_urls=[
            {
                "part_number": 1,
                "presigned_url": f"http://localhost:8000/api/upload/file/{job_id}/{request.filename}",
            }
        ],
    )


@app.put("/api/upload/file/{job_id}/{filename}")
async def upload_file_local(job_id: str, filename: str, request: bytes = b""):
    """Local dev: receive file bytes directly."""
    # This is a simplified handler — in practice, use request body. Both parameters
    # are joined onto INPUT_DIR, so they are checked like the result routes' (T33).
    dest = _job_dir(INPUT_DIR, job_id) / _file_name(filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # For local testing, copy a file manually or use the CLI upload below
    return {"status": "ok"}


@app.post("/api/upload/complete", response_model=UploadCompleteResponse)
async def complete_upload(request: UploadCompleteRequest):
    filename = request.s3_key.split("/")[-1]
    return UploadCompleteResponse(s3_key=request.s3_key, filename=filename)


# --- Jobs ---


@app.post("/api/jobs", response_model=JobResponse, status_code=201)
async def create_job(request: JobCreateRequest):
    """Create job and queue for local processing."""
    from datetime import datetime

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    job = JobResponse(
        job_id=job_id,
        filename=request.filename,
        s3_input_key=request.s3_key,
        config=request.config,
        status=JobStatus.queued,
        created_at=now,
    )
    job_store.create(job)

    # Queue for local worker
    _job_queue.append(
        {
            "job_id": job_id,
            "s3_input_key": request.s3_key,
            "config": request.config.model_dump(),
        }
    )
    logger.info("Job queued: %s (%s)", job_id, request.filename)

    return job


@app.get("/api/jobs", response_model=JobListResponse)
async def list_jobs(status: str | None = None):
    jobs = job_store.list(status=status)
    return JobListResponse(jobs=jobs, total=len(jobs))


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job


@app.put("/api/jobs/{job_id}/status")
async def update_job_status(
    job_id: str,
    status: JobStatus,
    detection_count: int | None = None,
    tile_count: int | None = None,
    processing_time_s: float | None = None,
    error_message: str | None = None,
):
    job_store.update_status(
        job_id,
        status,
        detection_count=detection_count,
        tile_count=tile_count,
        processing_time_s=processing_time_s,
        error_message=error_message,
    )
    return {"status": "updated"}


# --- Results ---


@app.get("/api/results/{job_id}/detections")
async def get_detections(job_id: str):
    path = _job_dir(OUTPUT_DIR, job_id) / "detections.json"
    if not path.exists():
        raise HTTPException(404, "Detection results not found")
    return Response(content=path.read_bytes(), media_type="application/json")


@app.get("/api/results/{job_id}/segmentation")
async def get_segmentation(job_id: str):
    path = _job_dir(OUTPUT_DIR, job_id) / "segmentation.json"
    if not path.exists():
        raise HTTPException(404, "Segmentation not found")
    return Response(content=path.read_bytes(), media_type="application/json")


@app.get("/api/results/{job_id}/tiles/index")
async def get_tile_index(job_id: str):
    path = _job_dir(TILES_DIR, job_id) / "index.json"
    if not path.exists():
        raise HTTPException(404, "Tile index not found")
    return Response(content=path.read_bytes(), media_type="application/json")


@app.get("/api/results/{job_id}/tiles/{tile_id}")
async def get_tile(job_id: str, tile_id: str):
    path = _job_dir(TILES_DIR, job_id) / f"{_tile_name(tile_id)}.bin"
    if not path.exists():
        raise HTTPException(404, f"Tile not found: {tile_id}")
    return Response(content=path.read_bytes(), media_type="application/octet-stream")


@app.get("/api/results/{job_id}/tiles/{tile_id}/segmentation")
async def get_tile_segmentation(job_id: str, tile_id: str):
    path = _job_dir(TILES_DIR, job_id) / f"{_tile_name(tile_id)}_seg.bin"
    if not path.exists():
        raise HTTPException(404, f"Segmentation not found for tile: {tile_id}")
    return Response(content=path.read_bytes(), media_type="application/octet-stream")


# --- Local Worker (runs in background thread) ---


def _local_worker_loop():
    """Background thread that processes jobs from the in-memory queue."""
    import time

    from pointcloud_detect.config import PipelineConfig
    from pointcloud_detect.pipeline import run_pipeline
    from pointcloud_detect.tiles.generator import (
        MAX_OVERVIEW_POINTS,
        _determine_point_format,
        _pack_points,
    )

    print("[WORKER] Local worker thread started, waiting for jobs...")

    while True:
        if not _job_queue:
            time.sleep(1)  # nosemgrep: arbitrary-sleep
            continue

        msg = _job_queue.popleft()
        job_id = msg["job_id"]
        s3_input_key = msg["s3_input_key"]
        config = msg["config"]

        print(f"[WORKER] Processing job: {job_id}")
        print(f"[WORKER] Input: {s3_input_key}")
        job_store.update_status(job_id, JobStatus.processing)

        try:
            # The file the key names, inside INPUT_DIR or refused (job failed).
            local_file = _local_input_path(s3_input_key)

            if not local_file.exists():
                raise FileNotFoundError(
                    f"Input file not found: {s3_input_key} (expected at {local_file})"
                )

            # Build pipeline config
            pipeline_config = PipelineConfig()
            pipeline_config.model.mode = config.get("model_mode", "indoor")
            pipeline_config.postprocessing.confidence_threshold = config.get(
                "confidence_threshold", 0.5
            )
            pipeline_config.preprocessing.use_color = config.get("use_color", True)
            pipeline_config.preprocessing.use_intensity = config.get(
                "use_intensity", False
            )
            pipeline_config.preprocessing.coord_frame = config.get(
                "coord_frame", "z-up"
            )
            pipeline_config.output.directory = OUTPUT_DIR / job_id
            pipeline_config.output.directory.mkdir(parents=True, exist_ok=True)

            start = time.perf_counter()
            result = run_pipeline(local_file, pipeline_config)
            elapsed = time.perf_counter() - start

            # Move output to expected location
            det_src = (
                pipeline_config.output.directory / f"{local_file.stem}_detections.json"
            )
            det_dst = OUTPUT_DIR / job_id / "detections.json"
            det_dst.parent.mkdir(parents=True, exist_ok=True)
            if det_src.exists():
                shutil.move(str(det_src), str(det_dst))

            # Generate LOD tiles (local filesystem version)
            # Use the segmentation's xyz if available (matches label indices exactly)
            import numpy as np

            from pointcloud_detect.io_readers import read_point_cloud

            if result.segmentation is not None and result.segmentation.xyz is not None:
                # Use preprocessed cloud from segmentation (indices align with labels)
                from pointcloud_detect.models import PointCloud as PC

                cloud = PC(xyz=result.segmentation.xyz)
            else:
                cloud = read_point_cloud(
                    local_file, max_points=pipeline_config.preprocessing.max_points
                )

            tiles_out = TILES_DIR / job_id
            tiles_out.mkdir(parents=True, exist_ok=True)

            # Generate tiles to local filesystem
            point_format = _determine_point_format(cloud)
            n_points = cloud.num_points

            # Overview
            if n_points <= MAX_OVERVIEW_POINTS:
                overview_indices = None
            else:
                rng = np.random.default_rng(42)
                overview_indices = rng.choice(
                    n_points, MAX_OVERVIEW_POINTS, replace=False
                )

            overview_bytes = _pack_points(
                cloud.xyz, cloud.rgb, cloud.intensity, point_format, overview_indices
            )
            (tiles_out / "overview.bin").write_bytes(overview_bytes)

            # Segmentation tiles
            # Bound to a narrowed local: `has_seg` alone loses the None-ness of
            # result.segmentation for both a reader and the type checker.
            seg = result.segmentation
            has_seg = seg is not None
            if seg is not None:
                seg_labels = seg.labels
                if overview_indices is not None:
                    # Subsample labels with same indices as points
                    safe_indices = overview_indices[overview_indices < len(seg_labels)]
                    (tiles_out / "overview_seg.bin").write_bytes(
                        seg_labels[safe_indices].astype(np.uint16).tobytes()
                    )
                else:
                    (tiles_out / "overview_seg.bin").write_bytes(
                        seg_labels.astype(np.uint16).tobytes()
                    )

            # Tile index
            seg_labels_meta = None
            if seg is not None:
                seg_labels_meta = [
                    {"id": i, "name": n} for i, n in enumerate(seg.class_names)
                ]

            tile_index = {
                "job_id": job_id,
                "total_points": n_points,
                "bounds_min": cloud.bounds_min.tolist(),
                "bounds_max": cloud.bounds_max.tolist(),
                "point_format": point_format,
                "has_segmentation": has_seg,
                "segmentation_labels": seg_labels_meta,
                "levels": 1,
                "tiles": [
                    {
                        "tile_id": "overview",
                        "level": 0,
                        "bounds_min": cloud.bounds_min.tolist(),
                        "bounds_max": cloud.bounds_max.tolist(),
                        "point_count": n_points
                        if overview_indices is None
                        else len(overview_indices),
                    }
                ],
            }
            (tiles_out / "index.json").write_bytes(
                json.dumps(tile_index, indent=2).encode()
            )

            job_store.update_status(
                job_id,
                JobStatus.complete,
                detection_count=len(result.detections),
                tile_count=1,
                processing_time_s=round(elapsed, 2),
            )
            print(
                f"[WORKER] Job {job_id} COMPLETE: "
                f"{len(result.detections)} detections in {elapsed:.1f}s"
            )

        except Exception as e:
            print(f"[WORKER] Job {job_id} FAILED: {e}")
            import traceback

            traceback.print_exc()
            job_store.update_status(job_id, JobStatus.failed, error_message=str(e))


@app.on_event("startup")
async def start_local_worker():
    """Start the background worker thread on API startup."""
    thread = Thread(target=_local_worker_loop, daemon=True)
    thread.start()
    logger.info("Local dev mode: in-process worker thread started")


# --- CLI helper to copy a file into the local input directory ---


def copy_file_for_testing(file_path: str) -> str:
    """Copy a point cloud file into local_dev_data/input for testing.

    Returns the s3_key to use when creating a job.
    """
    src = Path(file_path)
    job_id = str(uuid.uuid4())
    dest = INPUT_DIR / job_id / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    s3_key = f"uploads/{job_id}/{src.name}"
    print(f"File copied. Create job with s3_key: {s3_key}")
    return s3_key
