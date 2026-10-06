"""Connector CRUD and MCP synchronization endpoints."""

import json
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .. import mcp
from ..db import get_connection
from ..models import Connector, ConnectorCreate, ConnectorUpdate
from ..mappers import parse_json, parse_json_list
from ..services.mcp_catalog import mcp_sync
from ..services.mcp_connectors import save_mcp_connectors
from ._common import make_connector, new_id, now, owner_id, write_fields

router = APIRouter()


class MCPConnectorPayload(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    config: Optional[str] = Field(default=None, max_length=100_000)


class MCPConnectorPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)
    headers: Optional[dict[str, str]] = None
    tools: Optional[dict[str, bool]] = None


_SECRET_CONFIG_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key)", re.I)


def validate_connector_config(config: dict[str, Any]) -> dict[str, Any]:
    if any(_SECRET_CONFIG_KEY.search(str(key)) for key in config):
        raise HTTPException(status_code=422, detail="Connector config cannot contain credentials; use the server credential proxy")
    return config


def connector_from_id(connector_id: str, user_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return dict(row)


@router.get("/api/v1/connectors", response_model=list[Connector])
def list_connectors(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> list[Connector]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM connectors WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (owner_id(request), limit)).fetchall()
    return [make_connector(dict(row)) for row in rows]


@router.post("/api/v1/connectors", response_model=Connector, status_code=status.HTTP_201_CREATED)
def create_connector(request: Request, payload: ConnectorCreate) -> Connector:
    user_id = owner_id(request)
    if payload.kind.lower() == "mcp":
        raise HTTPException(status_code=422, detail="MCP 请使用 /api/v1/connectors/mcp")
    validate_connector_config(payload.config)
    timestamp = now()
    data = {"id": new_id("connector"), "user_id": user_id, "name": payload.name, "kind": payload.kind, "endpoint": payload.endpoint, "capabilities_json": json.dumps(payload.capabilities, ensure_ascii=False), "config_json": json.dumps(payload.config, ensure_ascii=False), "enabled": int(payload.enabled), "created_at": timestamp, "updated_at": timestamp, "metadata_json": json.dumps(payload.metadata, ensure_ascii=False)}
    with get_connection() as conn:
        conn.execute("INSERT INTO connectors(id,user_id,name,kind,endpoint,capabilities_json,config_json,enabled,created_at,updated_at,metadata_json) VALUES (:id,:user_id,:name,:kind,:endpoint,:capabilities_json,:config_json,:enabled,:created_at,:updated_at,:metadata_json)", data)
    return make_connector(data.copy())


@router.patch("/api/v1/connectors/{connector_id}", response_model=Connector)
def update_connector(request: Request, connector_id: str, payload: ConnectorUpdate) -> Connector:
    user_id = owner_id(request)
    existing = connector_from_id(connector_id, user_id)
    if existing["kind"] == "mcp" and any(value is not None for value in (payload.kind, payload.endpoint, payload.capabilities, payload.config, payload.metadata)):
        raise HTTPException(status_code=422, detail="MCP 连接器请使用专用接口")
    if payload.config is not None:
        validate_connector_config(payload.config)
    updates, args = write_fields(payload, ("name", "kind", "endpoint", "capabilities", "config", "enabled", "metadata"), json_fields=("capabilities", "config", "metadata"))
    field_map = {"capabilities": "capabilities_json", "config": "config_json", "metadata": "metadata_json"}
    for index, item in enumerate(updates):
        field = item.split(" = ", 1)[0]
        if field == "enabled":
            args[index] = int(args[index])
        elif field in field_map:
            updates[index] = f"{field_map[field]} = ?"
    with get_connection() as conn:
        if updates:
            updates.append("updated_at = ?")
            args.extend([now(), connector_id, user_id])
            cursor = conn.execute(f'UPDATE connectors SET {", ".join(updates)} WHERE id = ? AND user_id = ?', tuple(args))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Connector not found")
        row = conn.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return make_connector(dict(row))


@router.delete("/api/v1/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(request: Request, connector_id: str) -> None:
    user_id = owner_id(request)
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Connector not found")
        conn.execute("DELETE FROM connector_secrets WHERE connector_id = ? AND user_id = ?", (connector_id, user_id))


@router.post("/api/v1/connectors/mcp", status_code=status.HTTP_201_CREATED)
def create_mcp_connectors(request: Request, payload: MCPConnectorPayload) -> dict[str, Any]:
    user_id = owner_id(request)
    try:
        mcp.encrypt_headers({})
        if payload.config is not None:
            entries = mcp.parse_config(payload.config)
        elif payload.url:
            entries = [{"name": payload.name or (mcp.urlparse(payload.url).hostname or "mcp"), "url": mcp.validate_url(payload.url), "headers": mcp.validate_headers(payload.headers)}]
        else:
            raise mcp.MCPError("缺少 MCP 地址或配置")
    except mcp.SecretConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except mcp.MCPError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        connectors, errors = save_mcp_connectors(user_id, entries)
    except mcp.SecretConfigError as exc:
        raise HTTPException(status_code=503, detail=mcp.safe_error(exc)) from exc
    if not connectors:
        raise HTTPException(status_code=422, detail=errors[0]["detail"] if errors else "连接失败")
    return {"connectors": connectors, "errors": errors}


@router.patch("/api/v1/connectors/{connector_id}/mcp", response_model=Connector)
def patch_mcp_connector(request: Request, connector_id: str, payload: MCPConnectorPatch) -> Connector:
    user_id = owner_id(request)
    row = connector_from_id(connector_id, user_id)
    if row["kind"] != "mcp":
        raise HTTPException(status_code=422, detail="不是 MCP 连接器")
    try:
        with get_connection() as conn:
            secret = conn.execute("SELECT ciphertext FROM connector_secrets WHERE connector_id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
        headers = mcp.decrypt_headers(secret["ciphertext"]) if secret else {}
        endpoint = payload.url or row["endpoint"]
        if payload.headers is not None:
            headers = mcp.validate_headers(payload.headers)
        changed = payload.url is not None or payload.headers is not None
        metadata = parse_json(row["metadata_json"])
        if changed:
            metadata = mcp_sync(endpoint, headers, metadata.get("tools", []))
        if payload.tools:
            for tool in metadata.get("tools", []):
                if tool.get("name") in payload.tools:
                    tool["enabled"] = bool(payload.tools[tool["name"]])
        timestamp = now()
        with get_connection() as conn:
            conn.execute("UPDATE connectors SET name = ?, endpoint = ?, capabilities_json = ?, updated_at = ?, metadata_json = ? WHERE id = ? AND user_id = ?", (payload.name or row["name"], endpoint, json.dumps([t["name"] for t in metadata.get("tools", []) if t.get("enabled")], ensure_ascii=False), timestamp, json.dumps(metadata, ensure_ascii=False), connector_id, user_id))
            if changed:
                conn.execute("INSERT INTO connector_secrets(connector_id,user_id,ciphertext,updated_at) VALUES (?,?,?,?) ON CONFLICT(connector_id) DO UPDATE SET ciphertext=excluded.ciphertext,updated_at=excluded.updated_at", (connector_id, user_id, mcp.encrypt_headers(headers), timestamp))
            fresh = conn.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
        return make_connector(dict(fresh))
    except mcp.SecretConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=mcp.safe_error(exc)) from exc


@router.post("/api/v1/connectors/{connector_id}/sync", response_model=Connector)
def sync_mcp_connector(request: Request, connector_id: str) -> Connector:
    user_id = owner_id(request)
    row = connector_from_id(connector_id, user_id)
    if row["kind"] != "mcp":
        raise HTTPException(status_code=422, detail="不是 MCP 连接器")
    try:
        with get_connection() as conn:
            secret = conn.execute("SELECT ciphertext FROM connector_secrets WHERE connector_id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
        metadata = mcp_sync(row["endpoint"], mcp.decrypt_headers(secret["ciphertext"]), parse_json(row["metadata_json"]).get("tools", []))
    except Exception as exc:
        metadata = parse_json(row["metadata_json"])
        metadata.update(status="error", last_error=mcp.safe_error(exc), synced_at=now())
    with get_connection() as conn:
        conn.execute("UPDATE connectors SET capabilities_json = ?, metadata_json = ?, updated_at = ? WHERE id = ? AND user_id = ?", (json.dumps([t["name"] for t in metadata.get("tools", []) if t.get("enabled")], ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), now(), connector_id, user_id))
        fresh = conn.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
    return make_connector(dict(fresh))
