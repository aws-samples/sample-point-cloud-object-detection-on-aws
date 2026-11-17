"""Job processor — downloads input, runs pipeline, uploads results + tiles."""

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.tiles.generator import generate_lod_tiles
from pointcloud_detect.worker.parser_ipc import (
    SidecarJob,
    parser_mode,
    parser_spool,
    parser_timeout_seconds,
)
from pointcloud_detect.worker.s3_io import download_from_s3, upload_json_to_s3

logger = logging.getLogger(__name__)

S3_INPUT_BUCKET = os.environ.get("S3_INPUT_BUCKET", "")
S3_OUTPUT_BUCKET = os.environ.get("S3_OUTPUT_BUCKET", "")
S3_TILES_BUCKET = os.environ.get("S3_TILES_BUCKET", "")
API_BASE_URL = os.environ.get(
    "API_BASE_URL", "http://api.pointcloud-detection.svc.cluster.local"
)

# The states in which a job may still be worked on. A message for a job in any
# other state is a replay or a forgery (T19), and so is a write for one.
ACTIVE_JOB_STATUSES = frozenset({"queued", "processing"})


class JobNoLongerActiveError(RuntimeError):
    """The job's record left queued/processing while this worker held it."""


def process_job(
    job_id: str,
    s3_input_key: str,
    config: dict,
    s3_client,
) -> None:
    """Execute full detection pipeline for a job."""
    start_time = time.perf_counter()

    # Re-checked here, not only at the API: anything holding sqs:SendMessage can put a
    # message on the queue, and the key's last segment becomes a local filename below.
    # Checked before anything is written, so a bad key cannot even mark the job.
    from pointcloud_detect.api.models import validate_input_key

    validate_input_key(s3_input_key)
    # Read before any work too, so a misconfigured mode fails every job loudly
    # instead of falling back to parsing in this process.
    mode = parser_mode()

    # Update status to processing
    _update_status(job_id, "processing")

    job_type = config.get("job_type", "detection")
    filename = s3_input_key.split("/")[-1]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Build pipeline config from job config
        pipeline_config = _build_pipeline_config(config, tmpdir)

        if mode == "sidecar":
            # T11: the input is downloaded into the spool and parsed by the
            # credential-free sidecar. This process only ever reads the canonical
            # file it hands back, never the upload itself.
            with SidecarJob(parser_spool(), job_id, filename) as job:
                logger.info("Downloading %s for the parser sidecar", s3_input_key)
                download_from_s3(
                    s3_client, S3_INPUT_BUCKET, s3_input_key, job.input_path
                )
                cloud_path = job.parse(
                    max_points=pipeline_config.preprocessing.max_points,
                    timeout_seconds=parser_timeout_seconds(),
                )
                detection_count, tile_count = _run(
                    job_id,
                    job_type,
                    cloud_path,
                    job.input_path,
                    pipeline_config,
                    s3_client,
                )
        else:
            # Download input file from S3
            local_path = Path(tmpdir) / filename
            logger.info("Downloading %s to %s", s3_input_key, local_path)
            download_from_s3(s3_client, S3_INPUT_BUCKET, s3_input_key, local_path)
            detection_count, tile_count = _run(
                job_id, job_type, local_path, local_path, pipeline_config, s3_client
            )

    elapsed = time.perf_counter() - start_time

    # Update status to complete
    _assert_job_active(job_id)
    _update_status(
        job_id,
        "complete",
        detection_count=detection_count,
        tile_count=tile_count,
        processing_time_s=round(elapsed, 2),
    )


def _run(
    job_id: str,
    job_type: str,
    cloud_path: Path,
    source_path: Path,
    pipeline_config: PipelineConfig,
    s3_client,
) -> tuple[int, int]:
    """Run the job on a local point file and upload its outputs.

    ``cloud_path`` is what is read: the download itself in-process, the parser's
    canonical file in sidecar mode. ``source_path`` is the download, which is what
    the outputs name as their source either way.
    """
    from pointcloud_detect.io_readers import read_point_cloud
    from pointcloud_detect.preprocessing import preprocess

    det_key = f"results/{job_id}/detections.json"

    if job_type == "annotation":
        # Annotation-only: skip detection/segmentation, just generate tiles. This
        # whole-loads, so a file too large for the budget is refused first (T53).
        from pointcloud_detect.io_readers import detect_format
        from pointcloud_detect.pipeline import check_whole_load_file_size

        check_whole_load_file_size(
            cloud_path,
            detect_format(cloud_path),
            pipeline_config.tiling.memory_budget_gb,
        )
        cloud = read_point_cloud(
            cloud_path, max_points=pipeline_config.preprocessing.max_points
        )
        cloud = preprocess(cloud, pipeline_config.preprocessing)

        # Write empty detections so viewer doesn't error
        det_data = {"source_file": str(source_path), "detections": [], "total": 0}
        _assert_job_active(job_id)
        upload_json_to_s3(s3_client, S3_OUTPUT_BUCKET, det_key, det_data)

        tile_count = generate_lod_tiles(
            cloud=cloud,
            job_id=job_id,
            s3_client=s3_client,
            bucket=S3_TILES_BUCKET,
            segmentation_labels=None,
            segmentation_class_names=None,
        )
        return 0, tile_count

    # Run detection pipeline
    from pointcloud_detect.pipeline import run_pipeline

    result = run_pipeline(cloud_path, pipeline_config)
    result.source_file = str(source_path)

    # Upload detection results
    from pointcloud_detect.output import format_result

    det_data = format_result(result, pipeline_config)
    # Inference can take hours. Recheck the record before writing, so a job that
    # was completed or failed meanwhile is not overwritten (T19).
    _assert_job_active(job_id)
    upload_json_to_s3(s3_client, S3_OUTPUT_BUCKET, det_key, det_data)

    # Upload segmentation results
    if result.segmentation is not None:
        seg_key = f"results/{job_id}/segmentation.json"
        seg_data = {
            "classes": result.segmentation.class_names,
            "labels": result.segmentation.labels.tolist(),
            "xyz": result.segmentation.xyz.tolist()
            if result.segmentation.xyz is not None
            else None,
            "confidences": result.segmentation.confidences.tolist()
            if result.segmentation.confidences is not None
            else None,
        }
        upload_json_to_s3(s3_client, S3_OUTPUT_BUCKET, seg_key, seg_data)

    # Generate and upload LOD tiles for 3D viewer
    cloud = read_point_cloud(
        cloud_path, max_points=pipeline_config.preprocessing.max_points
    )
    cloud = preprocess(cloud, pipeline_config.preprocessing)
    seg_labels = result.segmentation.labels if result.segmentation else None
    seg_classes = result.segmentation.class_names if result.segmentation else None

    tile_count = generate_lod_tiles(
        cloud=cloud,
        job_id=job_id,
        s3_client=s3_client,
        bucket=S3_TILES_BUCKET,
        segmentation_labels=seg_labels,
        segmentation_class_names=seg_classes,
    )
    return len(result.detections), tile_count


def _build_pipeline_config(config: dict, output_dir: str) -> PipelineConfig:
    """Convert job config dict to PipelineConfig."""
    pipeline_config = PipelineConfig()
    pipeline_config.model.mode = config.get("model_mode", "indoor")
    pipeline_config.postprocessing.confidence_threshold = config.get(
        "confidence_threshold", 0.5
    )
    pipeline_config.preprocessing.use_color = config.get("use_color", True)
    pipeline_config.preprocessing.use_intensity = config.get("use_intensity", False)
    pipeline_config.preprocessing.use_normals = config.get("use_normals", False)
    pipeline_config.preprocessing.coord_frame = config.get("coord_frame", "z-up")
    pipeline_config.preprocessing.voxel_size_m = config.get("downsample_voxel_m", 0.02)
    pipeline_config.postprocessing.raw_mode = config.get("raw_mode", False)

    # Device preference: 'auto', 'cpu', or 'gpu' → maps to inference config
    device_pref = config.get("device_preference", "auto")
    if device_pref == "gpu":
        pipeline_config.inference.device = "cuda:0"
    elif device_pref == "cpu":
        pipeline_config.inference.device = "cpu"
    else:
        pipeline_config.inference.device = "auto"

    # Segmentation has only the pretrained PointNet++ weights, which are
    # non-commercially trained, so it runs only where the operator has opted in to
    # them (ALLOW_NONCOMMERCIAL_WEIGHTS, backends/checkpoints.py). Without the opt-in
    # an enabled segmentation would fail every job, customer-trained detector or not.
    from pointcloud_detect.backends.checkpoints import noncommercial_allowed

    pipeline_config.model.segmentation.enabled = noncommercial_allowed()

    # Explicit detection architecture selection (e.g. "fcaf3d_scannet",
    # "votenet_scannet"). Set from the UI model selector. This picks which
    # pretrained model runs; without it the PipelineConfig default is used.
    detection_config = config.get("detection_config")
    if detection_config:
        pipeline_config.model.detection.config = detection_config

    # Custom weights: if a model_id is specified, resolve it to a checkpoint path
    # via the model registry. This enables fine-tuned models to be selected
    # for inference.
    #
    # An id the registry does not know is rejected. It used to be treated as a direct
    # path to weights, which let any job submitter choose the file -- or, since the
    # checkpoint loader also accepts URLs, the remote host -- that the worker unpickles
    # into a process holding its IRSA credentials. Unpickling runs code.
    custom_weights = config.get("custom_weights_version")
    if custom_weights and custom_weights != "pretrained":
        from pointcloud_detect.api.model_registry import model_registry

        model = model_registry.get(custom_weights)
        if model is None:
            raise ValueError(
                f"custom_weights_version={custom_weights!r} is not a registered model "
                "id. Register the model first; paths and URLs are not accepted."
            )
        if model["checkpoint_path"] != "pretrained":
            pipeline_config.model.detection.weights = model["checkpoint_path"]
            # Use the base model's config (architecture) but custom weights
            pipeline_config.model.detection.config = model["base_model"]
        else:
            # A pretrained id: run that architecture with its published weights.
            pipeline_config.model.detection.config = model["base_model"]

    if config.get("max_points"):
        pipeline_config.preprocessing.max_points = config["max_points"]

    pipeline_config.output.directory = Path(output_dir) / "output"
    return pipeline_config


def _uses_shared_store() -> bool:
    return os.environ.get("JOB_STORE_BACKEND", "memory").lower() == "dynamodb"


def _api_job_url(job_id: str, suffix: str = "") -> str:
    """The API's URL for one job, for the in-memory store's HTTP fallback.

    API_BASE_URL is operator configuration, but it reaches urlopen, which would
    also open file: and other schemes, so anything but http(s) is refused. The job
    id is percent-encoded as one path segment: it comes from a queue message, and a
    "/" or "?" in it would otherwise address a different route.
    """
    scheme = urllib.parse.urlsplit(API_BASE_URL).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"API_BASE_URL must be http or https, not {scheme!r}")
    return f"{API_BASE_URL}/api/jobs/{urllib.parse.quote(job_id, safe='')}{suffix}"


def fetch_job_record(job_id: str) -> dict | None:
    """The job's record as the API wrote it, or None if there is no such job.

    Read from the shared store when there is one, otherwise from the API that holds
    the in-memory store. Raises on a transient failure, so the caller can leave the
    message for redelivery rather than mistake an outage for a missing job.
    """
    if _uses_shared_store():
        from pointcloud_detect.api.store import job_store

        job = job_store.get(job_id)
        return None if job is None else job.model_dump(mode="json")

    req = urllib.request.Request(_api_job_url(job_id), method="GET")
    try:
        # Scheme restricted to http(s) by _api_job_url.
        # nosemgrep: dynamic-urllib-use-detected, python.lang.security.audit.dynamic-urllib-use-detected, python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # noqa: E501
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
            body = json.loads(resp.read(1024 * 1024))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return body if isinstance(body, dict) else None


def _assert_job_active(job_id: str) -> None:
    """Raise unless the job is still queued or processing.

    The store's update is conditional on its record version, not on the job's
    status, so this re-read is the check: a job that another worker completed or
    that was failed while this one ran is left as it is rather than overwritten.
    There is a window between the read and the write; a status-conditioned write
    in the store would close it.
    """
    record = fetch_job_record(job_id)
    status = record.get("status") if record else None
    if status not in ACTIVE_JOB_STATUSES:
        raise JobNoLongerActiveError(
            f"Job {job_id} is {status or 'missing'}, no longer queued or processing; "
            "not writing its results."
        )


def _update_status(job_id: str, status: str, **kwargs) -> None:
    """Update job status in the shared store.

    When a shared backend is configured (JOB_STORE_BACKEND=dynamodb), write directly
    to it. This is the reliable path: the previous HTTP callback to the API failed
    whenever the request load-balanced to an API replica that did not hold the job in
    its process-local store, leaving jobs stuck in "processing". Fall back to the HTTP
    callback only for the in-memory backend (single-replica / local dev), where the
    worker shares no store with the API.
    """
    if _uses_shared_store():
        try:
            from pointcloud_detect.api.models import JobStatus
            from pointcloud_detect.api.store import job_store

            job_store.update_status(job_id, status=JobStatus(status), **kwargs)
            return
        except Exception:
            logger.warning(
                "Failed to update job status via store (non-fatal): %s", job_id
            )
            return

    params = {"status": status, **{k: v for k, v in kwargs.items() if v is not None}}
    # urlencode rather than f-string interpolation: error_message carries exception
    # text, and every Python exception message contains spaces. Interpolating it raw
    # produced an invalid URL, and a message containing & or = silently truncated the
    # value or injected extra query parameters.
    query = urllib.parse.urlencode(params)

    try:
        url = _api_job_url(job_id, f"/status?{query}")
        req = urllib.request.Request(url, method="PUT")
        # Scheme restricted to http(s) by _api_job_url.
        # nosemgrep: dynamic-urllib-use-detected, python.lang.security.audit.dynamic-urllib-use-detected, python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # noqa: E501
        urllib.request.urlopen(req, timeout=5)  # nosec B310
    except Exception:
        logger.warning("Failed to update job status (non-fatal): %s", job_id)
