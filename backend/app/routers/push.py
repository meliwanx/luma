"""Authenticated registration of APNs and FCM device tokens."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from ..db import get_connection
from ..models import PushDevice, PushDeviceCreate
from ._common import new_id, owner_id

router = APIRouter()

_PLATFORM_ALIASES = {
    "apns": "apns",
    "ios": "apns",
    "apple": "apns",
    "fcm": "fcm",
    "android": "fcm",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _platform(value: str) -> str:
    normalized = str(value or "").strip().lower()
    result = _PLATFORM_ALIASES.get(normalized)
    if result is None:
        raise HTTPException(status_code=422, detail="platform 必须是 apns 或 fcm")
    return result


@router.post("/api/v1/push/devices", response_model=PushDevice, status_code=status.HTTP_201_CREATED)
def register_push_device(request: Request, payload: PushDeviceCreate) -> PushDevice:
    user_id = owner_id(request)
    platform = _platform(payload.platform)
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=422, detail="token 不能为空")
    timestamp = _timestamp()
    with get_connection() as conn:
        # A physical device may move between accounts.  Remove an old owner
        # before the unique (platform, token) insert/update in this transaction.
        conn.execute(
            "DELETE FROM push_devices WHERE platform = ? AND token = ? AND user_id <> ?",
            (platform, token, user_id),
        )
        existing = conn.execute(
            "SELECT id FROM push_devices WHERE user_id = ? AND platform = ? AND token = ?",
            (user_id, platform, token),
        ).fetchone()
        if existing is None:
            device_id = new_id("push_device")
            conn.execute(
                "INSERT INTO push_devices(id,user_id,platform,token,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (device_id, user_id, platform, token, timestamp, timestamp),
            )
        else:
            device_id = str(existing["id"])
            conn.execute("UPDATE push_devices SET updated_at = ? WHERE id = ? AND user_id = ?", (timestamp, device_id, user_id))
        row = conn.execute(
            "SELECT id, platform, created_at, updated_at FROM push_devices WHERE id = ? AND user_id = ?",
            (device_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="设备注册失败")
    data = dict(row)
    data["token_suffix"] = token[-6:]
    return PushDevice.model_validate(data)


@router.delete("/api/v1/push/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_push_device(request: Request, device_id: str) -> None:
    user_id = owner_id(request)
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM push_devices WHERE id = ? AND user_id = ?", (device_id, user_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="设备不存在")
