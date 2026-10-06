"""Shared MCP creation for settings and safe chat intake."""

from __future__ import annotations

import json
from typing import Any, Optional

from .. import mcp
from ..mcp import redact_header_values
from ..db import get_connection
from ..mappers import make_connector, parse_json
from ..models import Connector
from .mcp_catalog import mcp_sync
from .seed import new_id, now


def save_mcp_connectors(
    user_id: str, entries: list[dict[str, Any]], redaction_headers: Optional[dict[str, str]] = None,
) -> tuple[list[Connector], list[dict[str, str]]]:
    """Sync and upsert by owner/endpoint; only encrypted headers are stored."""
    # Fail before contacting a remote server if secret storage is unavailable.
    mcp.encrypt_headers({})
    batch_headers = {str(index): value for index, value in enumerate(
        value for entry in entries for value in entry.get("headers", {}).values() if isinstance(value, str)
    )}
    for value in (redaction_headers or {}).values():
        batch_headers[str(len(batch_headers))] = value
    connectors: list[Connector] = []
    errors: list[dict[str, str]] = []
    for entry in entries:
        headers = entry.get("headers", {})
        name = redact_header_values(entry.get("name", "mcp"), batch_headers)
        try:
            endpoint = mcp.validate_url(entry["url"])
            headers = mcp.validate_headers(headers)
            if redact_header_values(endpoint, batch_headers) != endpoint:
                raise mcp.MCPError("请将凭据放在请求头中，不要放在 MCP 地址里")
            metadata = mcp_sync(endpoint, headers, redaction_headers=batch_headers)
            metadata["instructions"] = mcp.sanitize_instructions(metadata.get("instructions"), batch_headers)
            metadata["header_hints"] = {key: "***" for key in headers}
            metadata = redact_header_values(metadata, batch_headers)
            ciphertext = mcp.encrypt_headers(headers)
            timestamp = now()
            with get_connection() as conn:
                # Serialize the upsert across uvicorn workers without holding
                # a database connection during remote MCP synchronization.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (user_id + ":" + endpoint,))
                old = conn.execute("SELECT * FROM connectors WHERE user_id = ? AND kind = 'mcp' AND endpoint = ?", (user_id, endpoint)).fetchone()
                if old:
                    old_metadata = parse_json(old["metadata_json"])
                    old_enabled = {item.get("name"): item.get("enabled", True) for item in old_metadata.get("tools", []) if isinstance(item, dict)}
                    for tool in metadata["tools"]:
                        tool["enabled"] = bool(old_enabled.get(tool["name"], tool["enabled"]))
                    conn.execute("UPDATE connectors SET name = ?, capabilities_json = ?, config_json = ?, updated_at = ?, metadata_json = ? WHERE id = ? AND user_id = ?", (name, json.dumps([t["name"] for t in metadata["tools"] if t["enabled"]], ensure_ascii=False), json.dumps({"transport": "http"}), timestamp, json.dumps(metadata, ensure_ascii=False), old["id"], user_id))
                    connector_id = old["id"]
                else:
                    connector_id = new_id("connector")
                    conn.execute("INSERT INTO connectors(id,user_id,name,kind,endpoint,capabilities_json,config_json,enabled,created_at,updated_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (connector_id, user_id, name, "mcp", endpoint, json.dumps([t["name"] for t in metadata["tools"] if t["enabled"]], ensure_ascii=False), json.dumps({"transport": "http"}), 1, timestamp, timestamp, json.dumps(metadata, ensure_ascii=False)))
                conn.execute("INSERT INTO connector_secrets(connector_id,user_id,ciphertext,updated_at) VALUES (?,?,?,?) ON CONFLICT(connector_id) DO UPDATE SET user_id=excluded.user_id,ciphertext=excluded.ciphertext,updated_at=excluded.updated_at", (connector_id, user_id, ciphertext, timestamp))
                row = conn.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
            connectors.append(make_connector(dict(row)))
        except mcp.SecretConfigError:
            raise
        except Exception as exc:
            errors.append({"name": name, "detail": redact_header_values(mcp.safe_error(exc), batch_headers)})
    return connectors, errors


def backfill_mcp_instructions(
    user_id: str, connector_id: str, endpoint: str, ciphertext: str,
    instructions: Any, headers: dict[str, str],
) -> bool:
    """Persist instructions from an existing initialize without re-syncing tools."""
    instructions = mcp.sanitize_instructions(instructions, headers)
    if not instructions.strip():
        return False
    with get_connection() as conn:
        # Match sync's lock, then inspect current rows. No connection is held
        # while contacting the server; concurrent workers only fill once.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (user_id + ":" + endpoint,))
        connector = conn.execute(
            "SELECT * FROM connectors WHERE id = ? AND user_id = ? FOR UPDATE",
            (connector_id, user_id),
        ).fetchone()
        if connector is None or connector["kind"] != "mcp" or not connector["enabled"] or connector["endpoint"] != endpoint:
            return False
        secret = conn.execute(
            "SELECT ciphertext FROM connector_secrets WHERE connector_id = ? AND user_id = ? FOR UPDATE",
            (connector_id, user_id),
        ).fetchone()
        if secret is None or secret["ciphertext"] != ciphertext:
            return False
        metadata = parse_json(connector["metadata_json"])
        metadata = metadata if isinstance(metadata, dict) else {}
        if isinstance(metadata.get("instructions"), str) and metadata["instructions"].strip():
            return False
        timestamp = now()
        metadata["instructions"] = instructions
        metadata["instructions_synced_at"] = timestamp
        conn.execute(
            "UPDATE connectors SET metadata_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (json.dumps(metadata, ensure_ascii=False), timestamp, connector_id, user_id),
        )
    return True
