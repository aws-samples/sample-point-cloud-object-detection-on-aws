"""JSON output writer matching the defined schema."""

from pathlib import Path
from typing import Any

import numpy as np
import orjson

from pointcloud_detect.config import PipelineConfig
from pointcloud_detect.models import PipelineResult


def _numpy_to_list(obj: Any) -> Any:
    """Convert numpy arrays to lists for JSON serialisation."""
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def format_result(
    result: PipelineResult,
    config: PipelineConfig,
) -> dict[str, Any]:
    """Format pipeline result into the output JSON schema."""
    output: dict[str, Any] = {
        "metadata": {
            "source_file": result.source_file,
            "model": result.model_name,
            "model_version": result.model_version,
            "confidence_threshold": config.postprocessing.confidence_threshold,
            "raw_mode": config.postprocessing.raw_mode,
            "tile_count": result.tile_count,
            "processing_time_seconds": round(result.processing_time_seconds, 2),
            "device": result.device,
            "scene_bounds": {
                "min": _numpy_to_list(result.scene_bounds_min),
                "max": _numpy_to_list(result.scene_bounds_max),
            },
            "coordinates_normalized": config.output.normalize_coordinates,
        },
        "detections": [],
    }

    for i, det in enumerate(result.detections):
        det_dict: dict[str, Any] = {
            "id": f"det_{i + 1:04d}",
            "label": det.label,
            "confidence": round(det.confidence, 4),
            "bbox_obb": {
                "center": _numpy_to_list(det.center),
                "size": _numpy_to_list(det.size),
                "heading": round(float(det.heading), 4),
            },
        }

        if config.output.include_aabb and det.aabb_min is not None:
            det_dict["bbox_aabb"] = {
                "min": _numpy_to_list(det.aabb_min),
                "max": _numpy_to_list(det.aabb_max),
            }

        output["detections"].append(det_dict)

    if result.segmentation is not None and config.output.include_segmentation:
        input_stem = Path(result.source_file).stem
        seg_filename = f"{input_stem}_segmentation.json"
        output["segmentation"] = {
            "format": "point_index_map",
            "classes": result.segmentation.class_names,
            "num_points": int(result.segmentation.labels.shape[0]),
            "file": seg_filename,
        }

    return output


def write_result(
    result: PipelineResult,
    config: PipelineConfig,
) -> Path:
    """Write pipeline result to JSON file. Returns the output path."""
    output_dir = config.output.directory
    output_dir.mkdir(parents=True, exist_ok=True)

    input_stem = Path(result.source_file).stem
    output_name = config.output.naming.format(input_stem=input_stem)
    output_path = output_dir / f"{output_name}.json"

    formatted = format_result(result, config)

    tmp_path = output_path.with_suffix(".json.tmp")
    tmp_path.write_bytes(orjson.dumps(formatted, option=orjson.OPT_INDENT_2))
    tmp_path.replace(output_path)

    if result.segmentation is not None and config.output.include_segmentation:
        seg_path = output_dir / f"{input_stem}_segmentation.json"
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
        seg_tmp = seg_path.with_suffix(".json.tmp")
        seg_tmp.write_bytes(orjson.dumps(seg_data, option=orjson.OPT_INDENT_2))
        seg_tmp.replace(seg_path)

    return output_path
