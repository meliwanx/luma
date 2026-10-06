"""MCP connector catalog and synchronization helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from fastapi import HTTPException

from .. import mcp
from ..db import get_connection
from ..mappers import parse_json
from .seed import now

_SECRET_CONFIG_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|access[_-]?key)", re.I)

def mcp_catalog(user_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    definitions: list[dict[str, Any]] = []
    mapping: dict[str, dict[str, Any]] = {}
    runtimes: list[dict[str, Any]] = []
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM connectors WHERE user_id = ? AND kind = 'mcp' AND enabled = 1 ORDER BY created_at", (user_id,)).fetchall()
    for row in rows:
        record = dict(row)
        metadata = parse_json(record.get("metadata_json", "{}"))
        tools = metadata.get("tools", []) if isinstance(metadata, dict) else []
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict) or not tool.get("enabled", True):
                continue
            if len(definitions) >= 48:
                break
            base = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{record['name']}_{tool.get('name', '')}")[:64] or "mcp_tool"
            function_name = base
            suffix = 2
            while function_name in mapping:
                tail = f"_{suffix}"
                function_name = base[:64 - len(tail)] + tail
                suffix += 1
            definition = {"type": "function", "function": {"name": function_name, "description": f"[{record['name']}] {str(tool.get('description') or tool.get('title') or tool.get('name'))[:mcp.MCP_TOOL_DESCRIPTION_MAX_CHARS]}", "parameters": tool.get("input_schema") or {"type": "object"}}}
            definitions.append(definition)
            mapping[function_name] = {"connector_id": record["id"], "connector": record["name"], "endpoint": record["endpoint"], "tool": tool.get("name"), "read_only": mcp.tool_risk(tool) == "read", "annotations": tool.get("annotations", {}), "annotations_declared": bool(tool.get("annotations_declared")), "title": tool.get("title") or tool.get("name")}
        if len(definitions) >= 48:
            break
    return definitions, mapping, runtimes

def validate_connector_config(config: dict[str, Any]) -> dict[str, Any]:
    if any(_SECRET_CONFIG_KEY.search(str(key)) for key in config):
        raise HTTPException(status_code=422, detail="Connector config cannot contain credentials; use the server credential proxy")
    return config

def mcp_sync(endpoint: str, headers: dict[str, str], old_tools: Optional[list[dict[str, Any]]] = None, *, redaction_headers: Optional[dict[str, str]] = None) -> dict[str, Any]:
    endpoint = mcp.validate_url(endpoint)
    headers = mcp.validate_headers(headers)
    with mcp.MCPClient(endpoint, headers) as client:
        raw_tools, server = client.list_tools()
        instructions = getattr(client, "instructions", "")
    # Scrub raw data before normalize_tools and server fields truncate it;
    # otherwise a truncated credential echo would survive exact redaction.
    sensitive_headers = {str(index): value for index, value in enumerate(
        list(headers.values()) + list((redaction_headers or {}).values())
    )}
    raw_tools = mcp.redact_header_values(raw_tools, sensitive_headers)
    server = mcp.redact_header_values(server, sensitive_headers)
    instructions = mcp.sanitize_instructions(instructions, sensitive_headers)
    enabled = {item.get("name"): bool(item.get("enabled", True)) for item in (old_tools or []) if isinstance(item, dict)}
    tools = mcp.normalize_tools(raw_tools)
    for tool in tools:
        tool["enabled"] = enabled.get(tool["name"], True)
    return {"header_hints": mcp.redact_header_values(mcp.header_hints(headers), sensitive_headers), "tools": tools, "server": {"name": str(server.get("name", ""))[:200], "version": str(server.get("version", ""))[:80]}, "instructions": instructions, "status": "ok", "last_error": None, "synced_at": now()}

def connector_from_id(connector_id: str, user_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return dict(row)


# Preserve the private names used by the pre-split implementation while
# callers move to the service's public names.
_mcp_catalog = mcp_catalog
_mcp_sync = mcp_sync
_connector_from_id = connector_from_id
