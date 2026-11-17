"""Load and validate annotated training data in unified format."""

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def load_annotations(data_path: str) -> list[dict]:
    """Load all annotation JSON files from a directory.

    Expected format per file:
        {
            "scene_id": "...",
            "point_cloud_path": "...",
            "num_annotations": N,
            "annotations": [
                {"center_xyz": [x,y,z], "size_wlh": [w,l,h],
                 "heading": float, "class_name": "..."}
            ]
        }
    """
    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"Data path does not exist: {data_path}")

    annotations = []
    json_files = sorted(path.glob("*.json"))

    if not json_files:
        raise ValueError(f"No annotation JSON files found in: {data_path}")

    for f in json_files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        _validate_annotation(data, f.name)
        annotations.append(data)

    logger.info("Loaded %d annotation files from %s", len(annotations), data_path)
    return annotations


def _validate_annotation(data: dict, filename: str) -> None:
    """Validate annotation format."""
    required_keys = {"scene_id", "point_cloud_path", "annotations"}
    missing = required_keys - set(data.keys())
    if missing:
        raise ValueError(f"{filename}: missing keys: {missing}")

    for i, ann in enumerate(data["annotations"]):
        ann_keys = {"center_xyz", "size_wlh", "heading", "class_name"}
        ann_missing = ann_keys - set(ann.keys())
        if ann_missing:
            raise ValueError(f"{filename}: annotation {i} missing keys: {ann_missing}")
        if len(ann["center_xyz"]) != 3:
            raise ValueError(
                f"{filename}: annotation {i} center_xyz must have 3 values"
            )
        if len(ann["size_wlh"]) != 3:
            raise ValueError(f"{filename}: annotation {i} size_wlh must have 3 values")


def split_train_val(
    annotations: list[dict], val_split: float = 0.2, seed: int = 42
) -> tuple[list[dict], list[dict]]:
    """Split annotations into training and validation sets."""
    rng = np.random.default_rng(seed)
    indices = rng.permutation(len(annotations))
    val_count = max(1, int(len(annotations) * val_split))

    val_indices = set(indices[:val_count].tolist())
    train = [a for i, a in enumerate(annotations) if i not in val_indices]
    val = [a for i, a in enumerate(annotations) if i in val_indices]

    logger.info(
        "Split: %d train, %d val (%.0f%% val)", len(train), len(val), val_split * 100
    )
    return train, val


def get_class_names(annotations: list[dict]) -> list[str]:
    """Extract unique class names from annotations."""
    classes = set()
    for ann in annotations:
        for box in ann["annotations"]:
            classes.add(box["class_name"])
    return sorted(classes)
