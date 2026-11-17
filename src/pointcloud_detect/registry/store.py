"""Model registry backed by S3 + local JSON metadata."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_FILE = "model_registry.json"


class ModelRegistry:
    """Simple file-based model registry.

    For local use, stores metadata in a JSON file.
    For EKS, stores metadata in S3 alongside model weights.
    """

    def __init__(self, registry_path: str = "./models") -> None:
        self._path = Path(registry_path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._registry_file = self._path / REGISTRY_FILE
        self._models: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self._registry_file.exists():
            return json.loads(self._registry_file.read_text())
        return []

    def _save(self) -> None:
        self._registry_file.write_text(json.dumps(self._models, indent=2))

    def register(
        self,
        version: str,
        checkpoint_path: str,
        base_model: str,
        eval_metrics: dict,
        training_config: dict | None = None,
        dataset_info: str = "",
    ) -> dict:
        """Register a new model version."""
        entry = {
            "version": version,
            "checkpoint_path": checkpoint_path,
            "base_model": base_model,
            "eval_metrics": eval_metrics,
            "training_config": training_config or {},
            "dataset_info": dataset_info,
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._models.append(entry)
        self._save()
        logger.info("Registered model version: %s", version)
        return entry

    def get(self, version: str | None = None) -> dict | None:
        """Get a model by version, or latest if version is None."""
        if not self._models:
            return None
        if version is None:
            return self._models[-1]  # Latest
        for m in self._models:
            if m["version"] == version:
                return m
        return None

    def list(self, min_map: float | None = None) -> list[dict]:
        """List all registered models, optionally filtered by minimum mAP."""
        if min_map is None:
            return self._models
        return [
            m
            for m in self._models
            if m.get("eval_metrics", {}).get("mAP@0.25", 0) >= min_map
        ]

    def get_latest_version(self) -> str:
        """Get the next version string (auto-increment)."""
        if not self._models:
            return "v1.0.0"
        latest = self._models[-1]["version"]
        # Simple increment: v1.0.0 -> v1.0.1
        parts = latest.lstrip("v").split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return "v" + ".".join(parts)
