"""Model registry routes — list available models (pretrained + fine-tuned).

Read-only. POST /api/models, which registered a model from any client, is gone (T34):
it accepted any checkpoint path, including a URL, and any id, including a pretrained
one, so one request changed what everyone's jobs ran. Fine-tuned models are
registered only by the training path in this process (api/routes/training.py), which
the registry holds to keys under the models bucket (api/model_registry.py).
"""

from fastapi import APIRouter, Depends, HTTPException

from pointcloud_detect.api.identity import current_principal
from pointcloud_detect.api.model_registry import model_registry
from pointcloud_detect.api.ownership import Principal

router = APIRouter()


@router.get("")
async def list_models(principal: Principal = Depends(current_principal)):
    """List the models the caller may use: pretrained, and their own fine-tuned."""
    return {"models": model_registry.list_all(owner=principal.owner_filter)}


@router.get("/{model_id}")
async def get_model(model_id: str, principal: Principal = Depends(current_principal)):
    """Get a single model by ID."""
    model = model_registry.get(model_id, owner=principal.owner_filter)
    if model is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    return model
