"""Widget interaction endpoint."""
from typing import Any
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from ..widgets import apply_event
from ._common import owner_id

router = APIRouter()

class WidgetEventPayload(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    value: Any = None

@router.post("/api/v1/widgets/{widget_id}/events")
def widget_event(request: Request, widget_id: str, payload: WidgetEventPayload) -> dict[str, Any]:
    widget, message = apply_event(owner_id(request), widget_id, payload.action, payload.value)
    return {"widget": widget, "message": message}
