"""Main pipeline orchestration: ties together all stages."""

import time
import warnings
from pathlib import Path

import numpy as np

from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.inference import (
    create_detection_backend,
    create_segmentation_backend,
)
from pointcloud_detect.io_readers import (
    AUTO_CAP_THRESHOLD_POINTS,
    detect_format,
    read_point_cloud,
)
from pointcloud_detect.memory import GB, exceeds_budget
from pointcloud_detect.models import (
    Detection,
    PipelineResult,
    SegmentationResult,
)
from pointcloud_detect.output import write_result
from pointcloud_detect.postprocessing import postprocess
from pointcloud_detect.preprocessing import preprocess
from pointcloud_detect.streaming import is_streamable, scan_bounds, stream_tiles
from pointcloud_detect.tiling import adaptive_tile


def _resolve_device(device_config: str) -> str:
    """Resolve 'auto' device to actual device string."""
    if device_config != "auto":
        return device_config

    try:
        import torch  # pyright: ignore[reportMissingImports]

        if torch.cuda.is_available():
            return "cuda:0"
    except ImportError:
        pass

    return "cpu"


def check_whole_load_file_size(
    input_path: Path, fmt: str, memory_budget_gb: float
) -> None:
    """Refuse a whole-load-only file that is larger than the memory budget (T53).

    PLY and KITTI .bin cannot be streamed, so the budget can only be enforced
    before they are read. Held in memory, a point takes at least as many bytes as it
    does in either file (float64 coordinates against float32 or text), and the
    measured whole-load peak is several times that, so a file larger than the whole
    budget cannot fit and would only exhaust the process partway through.
    """
    if is_streamable(fmt):
        return
    size = Path(input_path).stat().st_size
    budget = int(memory_budget_gb * GB)
    if size > budget:
        raise ValueError(
            f"{input_path} is {size:,} bytes, more than tiling.memory_budget_gb "
            f"({memory_budget_gb:g} GB) allows, and {fmt} cannot be streamed. "
            "Convert it to LAS/LAZ or E57, or raise the budget."
        )


def _plan_ingest(
    input_path: Path, config: PipelineConfig
) -> tuple[bool, np.ndarray, np.ndarray]:
    """Decide whole-cloud vs streaming ingest.

    Streaming is used when the format is streamable (LAS/LAZ/E57) and either the
    whole cloud would exceed the memory budget or the file is large enough that the
    reader would silently subsample it. Returns (use_streaming, scene_min,
    scene_max); the bounds come from the cheap header scan and are zeros otherwise.
    """
    fmt = detect_format(input_path)
    if not is_streamable(fmt):
        check_whole_load_file_size(input_path, fmt, config.tiling.memory_budget_gb)
        # The budget simply does not apply to these formats, and a large file will
        # OOM with nothing telling the operator why. The design doc's own flow
        # diagram specifies "whole-cloud read + WARN" for this branch; the warning
        # was never emitted.
        warnings.warn(
            f"{fmt} is not a streamable format, so tiling.memory_budget_gb cannot "
            f"be enforced for {input_path}. The whole cloud will be loaded and a "
            "large file may exhaust memory. Convert to LAS/LAZ or E57 to use the "
            "streaming path.",
            RuntimeWarning,
            stacklevel=2,
        )
        return False, np.zeros(3), np.zeros(3)

    try:
        bounds_min, bounds_max, count = scan_bounds(input_path, fmt)
    except Exception as exc:
        # Deliberately broad, but it RE-RAISES rather than swallowing. The problem
        # with the previous `except Exception: return False, ...` was not its
        # breadth — it was that it discarded the error and downgraded to whole-load.
        # Reading a header is the cheapest thing this pipeline does; if it fails on a
        # file big enough to need streaming, whole-loading it is the exact wrong
        # direction, and the operator saw a confusing second failure from the
        # whole-cloud reader instead of the actual cause. A narrow tuple is not
        # workable here either: laspy and pye57 raise their own exception types
        # (LaspyException, E57Exception) that this module would have to import to
        # name.
        raise RuntimeError(
            f"Could not scan the header of {input_path} "
            f"({type(exc).__name__}: {exc}). Refusing to fall back to a whole-cloud "
            "load, which would risk exhausting memory on a large file."
        ) from exc

    # Channel presence is unknown before reading; assume the maximal footprint (rgb +
    # intensity) so the decision is conservative — we would rather stream unnecessarily
    # than under-estimate and OOM.
    over = exceeds_budget(
        count,
        config.tiling.memory_budget_gb,
        has_rgb=True,
        has_intensity=True,
    )

    # Close the silent-decimation band. The readers auto-subsample above
    # AUTO_CAP_THRESHOLD_POINTS, discarding ~90% of the file behind a
    # ResourceWarning, and that threshold sat BELOW the streaming threshold — so a
    # 200 M-point LAS was whole-loaded and then cut to 20 M. Stream at the reader's
    # cap regardless of the budget verdict.
    over_reader_cap = (
        config.preprocessing.max_points is None and count > AUTO_CAP_THRESHOLD_POINTS
    )
    if over_reader_cap and not over:
        warnings.warn(
            f"{input_path} has {count:,} points, above the reader's auto-subsample "
            f"threshold of {AUTO_CAP_THRESHOLD_POINTS:,}. Streaming instead of "
            "whole-loading so the points are not silently decimated, even though "
            "the memory budget alone would have permitted a whole-cloud read.",
            RuntimeWarning,
            stacklevel=2,
        )

    use_streaming = over or over_reader_cap

    # Warn about the segmentation limitation BEFORE any inference runs. This used to
    # fire after every tile had been processed, so on the multi-hour scene this
    # feature exists to support, the operator learned segmentation was unavailable
    # only once it was far too late to act on it.
    if use_streaming and config.model.segmentation.enabled:
        warnings.warn(
            "Segmentation will NOT be produced for this scene: it exceeds the "
            "memory budget and the streaming path cannot arbitrate per-point "
            "labels across tile overlaps without a resident, globally-indexed "
            "point array. Detections are unaffected. To get segmentation, raise "
            "tiling.memory_budget_gb above the scene footprint or crop the scene.",
            RuntimeWarning,
            stacklevel=2,
        )

    return use_streaming, bounds_min, bounds_max


def _run_streaming(
    input_path: Path,
    config: PipelineConfig,
    device: str,
    scene_min: np.ndarray,
    scene_max: np.ndarray,
    start_time: float,
) -> PipelineResult:
    """Streaming detection path: infer tile-by-tile without holding the whole cloud.

    Peak memory is bounded by one guard-band tile region, not the scene (NFR-1.1/1.2).

    LIMITATION: scene-wide per-point segmentation is NOT produced in streaming mode. It
    requires a resident, globally-indexed point array to arbitrate overlap labels, which
    is exactly what streaming avoids holding. Detection (the scale claim) is fully
    supported; segmentation on a >budget scene needs the whole-cloud path (raise the
    budget or crop). This is surfaced rather than silently returning wrong labels —
    and surfaced in ``_plan_ingest``, before inference starts, rather than after
    every tile has already been processed.
    """
    if config.preprocessing.max_points is not None:
        # This caps a whole-cloud read. Streaming never holds the whole cloud, so the
        # value has nowhere to apply and was silently ignored — measured 30,000
        # points streamed with max_points=100. Rejecting is honest; quietly
        # disregarding an explicit limit is not.
        raise ValueError(
            "preprocessing.max_points cannot be combined with the streaming ingest "
            f"path (scene {input_path} exceeds tiling.memory_budget_gb). It caps a "
            "whole-cloud read, so it would be silently ignored here. Remove it, or "
            "raise tiling.memory_budget_gb to use the whole-cloud path."
        )

    fmt = detect_format(input_path)
    detection_backend = create_detection_backend(config.model, device=device)

    all_detections: list[Detection] = []
    tile_count = 0
    # Scene bounds must be in the same frame as the detections. The bounds handed in
    # come from the file header, i.e. the FILE frame, while preprocess rotates each
    # tile to Z-up — so normalize_detections computed (center - scene_min) / extent
    # across two different frames, yielding values outside [0,1], and
    # metadata.scene_bounds described a different space than the detections it
    # shipped alongside. Accumulate the real bounds from the tiles as they stream.
    observed_min = np.full(3, np.inf, dtype=np.float64)
    observed_max = np.full(3, -np.inf, dtype=np.float64)

    for tile in stream_tiles(input_path, fmt, config):
        tile_count += 1
        if tile.xyz.shape[0] > 0:
            observed_min = np.minimum(observed_min, tile.xyz.min(axis=0))
            observed_max = np.maximum(observed_max, tile.xyz.max(axis=0))
        all_detections.extend(detection_backend.detect(tile))

    if np.all(np.isfinite(observed_min)) and np.all(np.isfinite(observed_max)):
        scene_min, scene_max = observed_min, observed_max

    detections = postprocess(
        detections=all_detections,
        config=config.postprocessing,
        include_aabb=config.output.include_aabb,
        normalize=config.output.normalize_coordinates,
        scene_min=scene_min,
        scene_max=scene_max,
    )

    result = PipelineResult(
        source_file=str(input_path),
        detections=detections,
        segmentation=None,
        scene_bounds_min=scene_min,
        scene_bounds_max=scene_max,
        tile_count=tile_count,
        processing_time_seconds=time.perf_counter() - start_time,
        model_name=config.model.detection.config,
        model_version=config.version,
        device=device,
    )
    write_result(result, config)
    return result


def run_pipeline(
    input_path: Path,
    config: PipelineConfig,
) -> PipelineResult:
    """Execute the full detection pipeline on a single input file."""
    start_time = time.perf_counter()
    device = _resolve_device(config.inference.device)

    # Decide ingest strategy: stream large streamable scenes to honor the memory budget
    # (NFR-1.1/1.2); otherwise load the whole cloud (small scenes, or non-streamable
    # formats like PLY / .bin).
    use_streaming, scene_min, scene_max = _plan_ingest(input_path, config)

    if use_streaming:
        return _run_streaming(
            input_path, config, device, scene_min, scene_max, start_time
        )

    cloud = read_point_cloud(input_path, max_points=config.preprocessing.max_points)
    cloud = preprocess(cloud, config.preprocessing)

    scene_min = cloud.bounds_min.copy()
    scene_max = cloud.bounds_max.copy()

    tiles = adaptive_tile(cloud, config.tiling)

    detection_backend = create_detection_backend(config.model, device=device)
    all_detections: list[Detection] = []
    for tile in tiles:
        tile_dets = detection_backend.detect(tile)
        all_detections.extend(tile_dets)

    segmentation_result: SegmentationResult | None = None
    if config.model.segmentation.enabled:
        seg_backend = create_segmentation_backend(config.model, device=device)
        scene_labels = np.full(cloud.num_points, -1, dtype=np.int32)
        scene_confidences = np.zeros(cloud.num_points, dtype=np.float32)
        class_names: list[str] = []

        for tile in tiles:
            tile_labels, class_names, tile_confidences = seg_backend.segment(tile)
            if tile.point_indices is not None:
                for i, idx in enumerate(tile.point_indices):
                    if (
                        scene_labels[idx] == -1
                        or tile_confidences[i] > scene_confidences[idx]
                    ):
                        scene_labels[idx] = tile_labels[i]
                        scene_confidences[idx] = tile_confidences[i]

        scene_labels = np.where(scene_labels == -1, 0, scene_labels)
        segmentation_result = SegmentationResult(
            labels=scene_labels,
            class_names=class_names,
            xyz=cloud.xyz,
            confidences=scene_confidences,
        )

    detections = postprocess(
        detections=all_detections,
        config=config.postprocessing,
        include_aabb=config.output.include_aabb,
        normalize=config.output.normalize_coordinates,
        scene_min=scene_min,
        scene_max=scene_max,
    )

    elapsed = time.perf_counter() - start_time

    result = PipelineResult(
        source_file=str(input_path),
        detections=detections,
        segmentation=segmentation_result,
        scene_bounds_min=scene_min,
        scene_bounds_max=scene_max,
        tile_count=len(tiles),
        processing_time_seconds=elapsed,
        model_name=config.model.detection.config,
        model_version=config.version,
        device=device,
    )

    write_result(result, config)

    return result
