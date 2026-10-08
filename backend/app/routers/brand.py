"""Public brand profile and built-in logo files."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..brand import get_brand, resolve_brand_asset
from ..client_config import browser_live_host_suffixes

router = APIRouter()

_PUBLIC_FIELDS = (
    "product_name",
    "assistant_name",
    "tagline",
    "company_name",
    "support_url",
    "logo_url",
    "primary_color",
)


@router.get("/api/v1/brand")
def read_brand() -> Dict[str, Any]:
    """Return the display brand. No session is required.

    ``logo_url`` is null when the deployment did not configure one.
    """
    payload = get_brand().public_dict()
    return {field: payload[field] for field in _PUBLIC_FIELDS}


@router.get("/api/v1/client-config")
def read_client_config() -> Dict[str, object]:
    """Return public client settings. No session is required."""
    return {"browser_live_host_suffixes": browser_live_host_suffixes()}


@router.get("/brand/{asset_name}", include_in_schema=False)
def brand_asset(asset_name: str) -> FileResponse:
    """Serve logo.svg and favicon.svg without falling through to the SPA shell."""
    path = resolve_brand_asset(asset_name)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        str(path),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )
