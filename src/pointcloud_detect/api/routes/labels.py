"""Labels route — serves annotation labels for the viewer.

Returns labels from the training session that owns the job, or defaults. In per-user
mode only the caller's sessions are searched (threat model T31).
"""

import uuid

from fastapi import APIRouter, Depends, Query

from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.ownership import Principal
from pointcloud_detect.api.training_session_store import session_store

router = APIRouter()

# Default labels when no session context is available
DEFAULT_LABELS = [
    {"name": "door", "color": "#00cc66", "default_size": [1.0, 0.2, 2.1]},
    {"name": "window", "color": "#44ccff", "default_size": [1.2, 0.2, 1.2]},
    {"name": "wall", "color": "#aaaaaa", "default_size": [3.0, 0.3, 2.8]},
    {"name": "column", "color": "#ff8800", "default_size": [0.6, 0.6, 3.0]},
    {"name": "chair", "color": "#ff4444", "default_size": [0.5, 0.5, 0.9]},
    {"name": "table", "color": "#8844ff", "default_size": [1.2, 0.8, 0.75]},
    {"name": "sofa", "color": "#44ff88", "default_size": [2.0, 0.9, 0.85]},
    {"name": "cabinet", "color": "#ff44cc", "default_size": [0.8, 0.4, 1.8]},
]


def _color_for_class(name: str) -> str:
    """Generate a deterministic color hex for a class name."""
    h = 0
    for c in name:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    hue = (h % 360) / 360.0
    # HSL to hex (simplified, s=0.65, l=0.55)
    import colorsys

    r, g, b = colorsys.hls_to_rgb(hue, 0.55, 0.65)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


@router.get("")
async def get_labels(
    job_id: uuid.UUID | None = Query(None),
    principal: Principal = Depends(current_principal),
):
    """Get annotation labels.

    If job_id belongs to a training session, returns that session's classes.
    Otherwise returns defaults.
    """
    if job_id:
        # Find the session that owns this job
        for session in session_store.list_all(owner=principal.owner_filter):
            if str(job_id) in session.get("job_ids", []):
                classes = session.get("classes", [])
                if classes:
                    labels = [
                        {
                            "name": c,
                            "color": _color_for_class(c),
                            "default_size": [1.0, 1.0, 1.0],
                        }
                        for c in classes
                    ]
                    return {"labels": labels}

    return {"labels": DEFAULT_LABELS}
