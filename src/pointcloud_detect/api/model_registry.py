"""In-memory model registry with pretrained + fine-tuned models.

Stores model metadata for the UI model selector. Pretrained models are
seeded at startup. Fine-tuned models are registered after training completes,
by the training path in this process (api/routes/training.py). There is no public
registration route: POST /api/models let any holder register any checkpoint path,
under any id including a pretrained one (T34).

In per-user mode a fine-tuned model is its trainer's (threat model T31): only they see
or select it. Pretrained models are everyone's. The owner is kept apart from the
model's record, so it is never returned to a client.

Production note: Replace with DynamoDB or S3 metadata for persistence. Keep the
checks in ``register`` when doing so: once the registry is shared, the worker
unpickles whatever checkpoint a registered model names.
"""

from datetime import UTC, datetime
from threading import Lock

from pointcloud_detect.api.ownership import ANY_OWNER, OwnerFilter, owner_matches

# Pretrained models available out-of-the-box
PRETRAINED_MODELS = [
    {
        "model_id": "fcaf3d_scannet",
        "name": "FCAF3D (Indoor / ScanNet)",
        "type": "pretrained",
        "base_model": "fcaf3d_scannet",
        "mode": "indoor",
        "checkpoint_path": "pretrained",
        "classes": [
            "cabinet",
            "bed",
            "chair",
            "sofa",
            "table",
            "door",
            "window",
            "bookshelf",
            "picture",
            "counter",
            "desk",
            "curtain",
            "refrigerator",
            "shower curtain",
            "toilet",
            "sink",
            "bathtub",
            "other furniture",
        ],
        "description": "18-class indoor detection (anchor-free, ScanNet)",
        "created_at": "2024-01-01T00:00:00Z",
        "training_job_id": None,
    },
    {
        "model_id": "votenet_scannet",
        "name": "VoteNet (Indoor / ScanNet)",
        "type": "pretrained",
        "base_model": "votenet_scannet",
        "mode": "indoor",
        "checkpoint_path": "pretrained",
        "classes": [
            "cabinet",
            "bed",
            "chair",
            "sofa",
            "table",
            "door",
            "window",
            "bookshelf",
            "picture",
            "counter",
            "desk",
            "curtain",
            "refrigerator",
            "shower curtain",
            "toilet",
            "sink",
            "bathtub",
            "other furniture",
        ],
        "description": "18-class indoor detection (ScanNet pre-trained)",
        "created_at": "2024-01-01T00:00:00Z",
        "training_job_id": None,
    },
    {
        "model_id": "centerpoint_nuscenes",
        "name": "CenterPoint (Outdoor / nuScenes)",
        "type": "pretrained",
        "base_model": "centerpoint_nuscenes",
        "mode": "outdoor",
        "checkpoint_path": "pretrained",
        "classes": [
            "car",
            "truck",
            "bus",
            "trailer",
            "construction vehicle",
            "pedestrian",
            "motorcycle",
            "bicycle",
            "traffic cone",
            "barrier",
        ],
        "description": "10-class outdoor detection (nuScenes, CPU-compatible)",
        "created_at": "2024-01-01T00:00:00Z",
        "training_job_id": None,
    },
    {
        "model_id": "pointpillars_kitti",
        "name": "PointPillars (Outdoor / KITTI)",
        "type": "pretrained",
        "base_model": "pointpillars_kitti",
        "mode": "kitti",
        "checkpoint_path": "pretrained",
        "classes": ["car", "pedestrian", "cyclist"],
        "description": "3-class outdoor detection (KITTI, CPU-compatible)",
        "created_at": "2024-01-01T00:00:00Z",
        "training_job_id": None,
    },
]


PRETRAINED_MODEL_IDS = frozenset(m["model_id"] for m in PRETRAINED_MODELS)


def pretrained_weights_available() -> bool:
    """Whether pretrained models are selectable: opted in, or a stub image."""
    from pointcloud_detect.backends.checkpoints import pretrained_selection_allowed

    return pretrained_selection_allowed()


def _annotated(model: dict) -> dict:
    """A copy of ``model`` with ``available`` (and ``noncommercial`` if pretrained)."""
    out = dict(model)
    if out.get("type") == "pretrained":
        out["noncommercial"] = True
        out["available"] = pretrained_weights_available()
    else:
        out["available"] = True
    return out


# Fine-tuned checkpoints live under this prefix of the models bucket, and nowhere else.
CHECKPOINT_PREFIX = "models/"


class ModelRegistrationError(ValueError):
    """A registration the registry refuses: a taken id or a checkpoint outside."""


def checkpoint_uri(key: str) -> str:
    """The ``s3://`` URI of ``key`` in the models bucket (S3_MODELS_BUCKET)."""
    from pointcloud_detect.api.deps import get_settings

    bucket = get_settings().s3_models_bucket.strip()
    if not bucket:
        raise ModelRegistrationError(
            "S3_MODELS_BUCKET is not set, so no checkpoint location is acceptable."
        )
    return f"s3://{bucket}/{key}"


def validate_checkpoint_path(checkpoint_path: str) -> str:
    """Accept only ``s3://<models bucket>/models/<key>`` with a plain key.

    The worker loads the checkpoint a model names, and loading a checkpoint unpickles
    it, which runs code (T18). So the only acceptable location is the one the
    training path writes: the configured models bucket, under ``models/``. A path, a
    URL or another bucket is refused, and so is any ``.``, ``..`` or empty segment,
    which S3 would treat as a literal key but a local cache or a later copy might not.
    """
    prefix = checkpoint_uri(CHECKPOINT_PREFIX)
    key = checkpoint_path[len(prefix) :]
    if (
        not checkpoint_path.startswith(prefix)
        or not key
        or "\\" in key
        or any(seg in ("", ".", "..") for seg in key.split("/"))
    ):
        raise ModelRegistrationError(
            f"checkpoint_path must be {prefix}<key>, with no empty, '.' or '..' "
            "segments"
        )
    return checkpoint_path


class ModelRegistry:
    """Thread-safe in-memory model registry."""

    def __init__(self) -> None:
        self._models: dict[str, dict] = {}
        # Fine-tuned models' owners, by model_id.
        self._owners: dict[str, str | None] = {}
        self._lock = Lock()
        # Seed pretrained models
        for m in PRETRAINED_MODELS:
            self._models[m["model_id"]] = m.copy()

    def _visible(self, model_id: str, model: dict, owner: OwnerFilter) -> bool:
        return model.get("type") == "pretrained" or owner_matches(
            self._owners.get(model_id), owner
        )

    def list_all(self, owner: OwnerFilter = ANY_OWNER) -> list[dict]:
        """List the models ``owner`` may use, pretrained first then fine-tuned by date.

        Each entry is a copy carrying ``available``: a pretrained model is available
        only when ALLOW_NONCOMMERCIAL_WEIGHTS is set, since its weights are
        non-commercially trained and are otherwise never downloaded. Pretrained
        entries also carry ``noncommercial: True``. They stay listed when
        unavailable, because their architectures remain the base for training from
        scratch.
        """
        with self._lock:
            models = [
                _annotated(m)
                for model_id, m in self._models.items()
                if self._visible(model_id, m, owner)
            ]
        pretrained = [m for m in models if m["type"] == "pretrained"]
        finetuned = sorted(
            [m for m in models if m["type"] == "finetuned"],
            key=lambda m: m.get("created_at", ""),
            reverse=True,
        )
        return pretrained + finetuned

    def get(self, model_id: str, owner: OwnerFilter = ANY_OWNER) -> dict | None:
        """The model, if ``owner`` may use it: every pretrained one, their own tuned."""
        with self._lock:
            model = self._models.get(model_id)
            if model is None or not self._visible(model_id, model, owner):
                return None
            return model

    def unregister_by_training_job(self, training_job_id: str) -> list[str]:
        """Remove fine-tuned models produced by a training job.

        Called when a training job is deleted so its model stops appearing in
        the detection model selector. Pretrained models are never removed.
        Returns the model_ids that were removed.
        """
        removed: list[str] = []
        with self._lock:
            for model_id, model in list(self._models.items()):
                if (
                    model.get("type") == "finetuned"
                    and model.get("training_job_id") == training_job_id
                ):
                    del self._models[model_id]
                    self._owners.pop(model_id, None)
                    removed.append(model_id)
        return removed

    def register(
        self,
        model_id: str,
        name: str,
        base_model: str,
        checkpoint_path: str,
        classes: list[str],
        training_job_id: str | None = None,
        owner: str | None = None,
    ) -> dict:
        """Register a fine-tuned model, its trainer's in per-user mode.

        Raises ``ModelRegistrationError`` if ``model_id`` is taken, including by a
        pretrained model, or if ``checkpoint_path`` is not under the models bucket's
        ``models/`` prefix. An existing entry is never overwritten: a reused id would
        silently change what every job selecting it runs.
        """
        validate_checkpoint_path(checkpoint_path)
        model = {
            "model_id": model_id,
            "name": name,
            "type": "finetuned",
            "base_model": base_model,
            "mode": "custom",
            "checkpoint_path": checkpoint_path,
            "classes": classes,
            "description": f"Fine-tuned from {base_model} ({len(classes)} classes)",
            "created_at": datetime.now(UTC).isoformat(),
            "training_job_id": training_job_id,
        }
        with self._lock:
            if model_id in PRETRAINED_MODEL_IDS or model_id in self._models:
                raise ModelRegistrationError(
                    f"model_id {model_id!r} is already registered"
                )
            self._models[model_id] = model
            self._owners[model_id] = owner
        return model


# Singleton
model_registry = ModelRegistry()
