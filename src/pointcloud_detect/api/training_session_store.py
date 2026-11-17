"""In-memory training session store.

A training session groups multiple annotation jobs that will be used
together for fine-tuning. Each session tracks:
- Configuration (base model, classes, coord frame)
- List of annotation job IDs
- Status: "collecting" on creation, then "complete" or "failed" once a linked
  training job finishes. The intermediate "ready" and "training" states are not
  currently reachable — nothing advances a session before its run completes.

In per-user mode each session also records who created it (threat model T31), apart
from the session itself so that it is never returned to a client.

Production note: Replace with DynamoDB for persistence.
"""

import uuid
from datetime import UTC, datetime
from threading import Lock

from pointcloud_detect.api.ownership import ANY_OWNER, OwnerFilter, owner_matches


class TrainingSessionStore:
    """Thread-safe in-memory training session store."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._owners: dict[str, str | None] = {}
        self._lock = Lock()

    def create(
        self,
        name: str,
        base_model: str,
        coord_frame: str,
        use_color: bool,
        use_intensity: bool,
        classes: list[str],
        owner: str | None = None,
    ) -> dict:
        session_id = str(uuid.uuid4())
        session = {
            "session_id": session_id,
            "name": name,
            "base_model": base_model,
            "coord_frame": coord_frame,
            "use_color": use_color,
            "use_intensity": use_intensity,
            "classes": classes,
            "job_ids": [],
            "status": "collecting",  # collecting -> complete | failed
            "created_at": datetime.now(UTC).isoformat(),
            "training_job_id": None,
            "model_artifact": None,
        }
        with self._lock:
            self._sessions[session_id] = session
            self._owners[session_id] = owner
        return session

    def list_all(self, owner: OwnerFilter = ANY_OWNER) -> list[dict]:
        with self._lock:
            return sorted(
                (
                    s
                    for sid, s in self._sessions.items()
                    if owner_matches(self._owners.get(sid), owner)
                ),
                key=lambda s: s["created_at"],
                reverse=True,
            )

    def get(self, session_id: str, owner: OwnerFilter = ANY_OWNER) -> dict | None:
        with self._lock:
            if not owner_matches(self._owners.get(session_id), owner):
                return None
            return self._sessions.get(session_id)

    def add_job(self, session_id: str, job_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                if job_id not in self._sessions[session_id]["job_ids"]:
                    self._sessions[session_id]["job_ids"].append(job_id)

    def update_status(self, session_id: str, status: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["status"] = status

    def set_training_result(
        self, session_id: str, training_job_id: str, model_artifact: str | None
    ) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["training_job_id"] = training_job_id
                self._sessions[session_id]["model_artifact"] = model_artifact
                self._sessions[session_id]["status"] = (
                    "complete" if model_artifact else "failed"
                )


# Singleton
session_store = TrainingSessionStore()
