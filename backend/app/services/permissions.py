"""Long-lived, user-scoped permissions for model-visible tools."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from ..db import get_connection
from .. import mcp
from ..runtime import append_activity
from ..mappers import parse_json


# The values here are descriptions for the settings screen.  Authorization is
# still performed by ``agent.policy`` for every individual tool call.
BUILTIN_PERMISSION_SPECS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "luma.routines.create",
        "label": "创建例程",
        "description": "允许助手创建会持续运行的定时例程。",
        "category": "luma",
        "allow_always": True,
    },
    {
        "key": "luma.tasks.update",
        "label": "修改任务",
        "description": "允许助手修改你的任务内容或状态。",
        "category": "luma",
        "allow_always": True,
    },
    {
        "key": "luma.notifications.create",
        "label": "创建通知",
        "description": "允许助手向你的通知中心写入通知。",
        "category": "luma",
        "allow_always": True,
    },
    {
        "key": "sandbox.preview",
        "label": "生成沙箱预览链接",
        "description": "允许助手生成一个指向沙箱端口的临时公网预览链接。",
        "category": "sandbox",
        "allow_always": True,
    },
    {
        "key": "browser.submit",
        "label": "浏览器提交表单",
        "description": "允许助手在云端浏览器提交表单；支付和下单仍需每次确认。",
        "category": "browser",
        "allow_always": True,
        "default_mode": "ask",
    },
)


def is_always_forbidden(key_or_tool: str) -> bool:
    """Return whether a permission must be confirmed on every invocation."""

    lowered = str(key_or_tool or "").lower()
    # Connector ids are opaque and may themselves contain words such as
    # ``order``; the destructive-name rule applies to the remote tool part.
    if lowered.startswith("mcp:") and ":" in lowered[4:]:
        lowered = lowered.rsplit(":", 1)[-1]
    return any(part in lowered for part in mcp.MCP_CONFIRM_NAME_PARTS)


def _mcp_specs(user_id: str) -> Iterable[Dict[str, Any]]:
    try:
        # Read the enabled connector metadata directly. The model tool
        # catalog intentionally caps definitions for prompt size; settings
        # must still expose every enabled modification tool.
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT id,name,metadata_json FROM connectors "
                "WHERE user_id = ? AND kind = 'mcp' AND enabled = 1 ORDER BY created_at",
                (user_id,),
            ).fetchall()
    except Exception:
        return ()
    result = []
    seen = set()
    for row in rows:
        record = dict(row)
        metadata = parse_json(record.get("metadata_json", "{}"))
        tools = metadata.get("tools", []) if isinstance(metadata, dict) else []
        for info in tools if isinstance(tools, list) else []:
            if not isinstance(info, dict) or not info.get("enabled", True) or mcp.tool_risk(info) == "read":
                continue
            connector_id = str(record.get("id") or "").strip()
            tool_name = str(info.get("name") or "").strip()
            if not connector_id or not tool_name:
                continue
            key = "mcp:%s:%s" % (connector_id, tool_name)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "key": key,
                    "label": str(info.get("title") or tool_name)[:200],
                    "description": "允许调用连接器中的修改工具。",
                    "category": "mcp",
                    "allow_always": not mcp.tool_requires_confirmation(info),
                }
            )
    return result


def permission_specs(user_id: str) -> list[Dict[str, Any]]:
    """Return all currently valid built-in and enabled MCP permission keys."""

    # MCP tools are user data and may change independently of this process;
    # preserve their catalog order, then append stable built-in entries.
    specs = list(_mcp_specs(user_id))
    specs.extend(dict(item) for item in BUILTIN_PERMISSION_SPECS)
    return specs


def permission_spec(user_id: str, key: str) -> Optional[Dict[str, Any]]:
    for item in permission_specs(user_id):
        if item["key"] == key:
            return item
    return None


def permission_mode(user_id: str, key: str) -> str:
    """Read one mode, failing closed to ``ask`` on an unavailable table."""

    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT mode FROM tool_permissions WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
    except Exception:
        return "ask"
    if row is None:
        return _default_mode(key)
    value = str(row["mode"] if hasattr(row, "__getitem__") else row[0])
    return value if value in {"ask", "always"} else "ask"


def _default_mode(key: str) -> str:
    if is_always_forbidden(key):
        return "ask"
    for spec in BUILTIN_PERMISSION_SPECS:
        if spec["key"] == key:
            return str(spec.get("default_mode", "always"))
    return "always"


def permission_items(user_id: str) -> list[Dict[str, Any]]:
    """Build the settings response without exposing connector credentials."""

    modes: Dict[str, str] = {}
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT key, mode FROM tool_permissions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        for row in rows:
            value = str(row["mode"])
            if value in {"ask", "always"}:
                modes[str(row["key"])] = value
    except Exception:
        # A rolling deployment can serve settings before 0011 has run.  The
        # route remains useful while policy reads still fail closed.
        modes = {}
    result = []
    for spec in permission_specs(user_id):
        item = dict(spec)
        item["mode"] = modes.get(item["key"], _default_mode(item["key"]))
        if not item.get("allow_always", True) or is_always_forbidden(item["key"]):
            item["allow_always"] = False
            item["mode"] = "ask"
        result.append(item)
    return result


def set_permission_mode(user_id: str, key: str, mode: str) -> Dict[str, Any]:
    """Persist one mode and write an audit event containing only key/mode."""

    spec = permission_spec(user_id, key)
    if spec is None:
        raise KeyError(key)
    if mode not in {"ask", "always"}:
        raise ValueError("mode must be ask or always")
    allow_always = bool(spec.get("allow_always", True)) and not is_always_forbidden(key)
    if mode == "always" and not allow_always:
        raise PermissionError("该工具必须逐次确认")
    from .seed import now

    timestamp = now()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tool_permissions(user_id,key,mode,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT (user_id,key) DO UPDATE SET mode = EXCLUDED.mode, updated_at = EXCLUDED.updated_at",
            (user_id, key, mode, timestamp),
        )
    append_activity(
        "permission_updated",
        "工具权限已更新",
        "%s: %s" % (key, mode),
        user_id=user_id,
        notify=False,
    )
    result = dict(spec)
    result["mode"] = mode
    result["allow_always"] = allow_always
    return result


__all__ = [
    "BUILTIN_PERMISSION_SPECS",
    "is_always_forbidden",
    "permission_items",
    "permission_key_for_tool",
    "permission_mode",
    "permission_spec",
    "permission_specs",
    "set_permission_mode",
]


def permission_key_for_tool(tool: Any) -> Tuple[Optional[str], bool]:
    """Compute a permission key from a Tool-like object without arguments."""

    metadata = tool.get("metadata", {}) if isinstance(tool, dict) else getattr(tool, "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    name = str(tool.get("name", "") if isinstance(tool, dict) else getattr(tool, "name", ""))
    risk = tool.get("risk", "") if isinstance(tool, dict) else getattr(tool, "risk", "")
    if risk == "local":
        return None, False
    explicit_key = str(metadata.get("permission_key") or "").strip()
    if explicit_key:
        return explicit_key, not is_always_forbidden(explicit_key) and not mcp.tool_requires_confirmation(metadata)
    connector_id = str(metadata.get("connector_id") or "").strip()
    if connector_id:
        remote_name = str(metadata.get("tool") or metadata.get("mcp_name") or name).strip()
        key = "mcp:%s:%s" % (connector_id, remote_name)
        return key, not mcp.tool_requires_confirmation({**metadata, "tool": remote_name})
    if name == "sandbox.preview":
        return name, True
    if name in {item["key"] for item in BUILTIN_PERMISSION_SPECS}:
        return name, not is_always_forbidden(name)
    return None, False
