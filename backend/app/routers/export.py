"""Portable user data export endpoint."""
from typing import Any
from fastapi import APIRouter, Request
from ..services.export import export_data as export_snapshot

router = APIRouter()

@router.get("/api/v1/export")
def export_data(request: Request) -> dict[str, Any]:
    return export_snapshot(request)
