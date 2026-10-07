"""The single Sentinel policy gate for model-visible tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .tools import AgentContext, Tool


# These controlled writes are safe without a second approval step. Connector
# creation independently checks the address against the user's chat messages.
# Keep this list shared with the runtime routes so the API description
# and the agent policy cannot drift apart. Configurable writes use the saved
# permission mode, whose default is always.
AUTO_APPROVED_WRITE_TOOLS = frozenset({
    "luma.tasks.create", "luma.memory.create",
    "luma.connectors.add_mcp", "luma.connectors.set_enabled",
})


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str
    permission_key: Optional[str] = None
    allow_always: bool = False

    @property
    def action(self) -> str:
        """Compatibility alias used by callers that call this an action."""
        return self.decision


def _value(ctx: Any, name: str, default: Any = None) -> Any:
    if isinstance(ctx, dict):
        return ctx.get(name, default)
    return getattr(ctx, name, default)


def _audit(ctx: Any, tool: Tool, decision: Decision) -> None:
    # Import lazily to avoid runtime -> agent -> runtime import cycles.  The
    # activity row stores names and outcomes only; arguments never cross this
    # boundary.
    try:
        from ..runtime import append_activity

        append_activity(
            "tool_decision",
            "工具权限判定",
            "%s: %s" % (tool.name, decision.reason),
            job_id=_value(ctx, "job_id"),
            user_id=str(_value(ctx, "user_id", "local")),
        )
    except Exception:
        # Audit persistence must not make a safe denial/confirmation fail open.
        pass


def _approved_background_call(ctx: Any, tool: Tool, args: Dict[str, Any]) -> bool:
    """Match one server-persisted approval without trusting model arguments."""
    if str(_value(ctx, "mode", "interactive")) != "background":
        return False
    approved = _value(ctx, "approved_calls", ())
    if not isinstance(approved, (list, tuple)):
        return False
    for index, item in enumerate(approved):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        payload = item.get("payload")
        if action == tool.name and isinstance(payload, dict) and payload == args:
            if tool.metadata.get("capability_id"):
                guard = item.get("capability_guard")
                if not isinstance(guard, dict) or guard.get("id") != tool.metadata.get("capability_id") or guard.get("version") != tool.metadata.get("version"):
                    continue  # A prior schema approval cannot authorize a revision.
                if not _consume_capability_approval(ctx, tool, item):
                    continue
            browser_allowed = True
            if action in {"browser.click", "browser.submit"}:
                browser_allowed = _consume_browser_approval(ctx, tool, item)
            # One persisted approval authorizes one invocation in this run.
            # The model cannot reuse it by repeating the same arguments.
            if isinstance(approved, list):
                del approved[index]
            else:
                remaining = approved[:index] + approved[index + 1:]
                try:
                    if isinstance(ctx, dict):
                        ctx["approved_calls"] = remaining
                    else:
                        setattr(ctx, "approved_calls", remaining)
                except (AttributeError, TypeError):
                    return False
            return browser_allowed
    return False


def _consume_capability_approval(ctx: Any, tool: Tool, approval: Dict[str, Any]) -> bool:
    """Spend a durable approval before an uncertain external write starts."""
    approval_id = str(approval.get("id") or "")
    job_id = _value(ctx, "job_id")
    raw_payload = approval.get("approval_payload_json")
    if not approval_id or not job_id or not isinstance(raw_payload, str):
        return False
    try:
        from ..db import get_connection
        with get_connection() as conn:
            changed = conn.execute(
                "UPDATE runtime_approvals SET status = 'consumed' WHERE id = ? AND user_id = ? AND job_id = ? "
                "AND action = ? AND status = 'approved' AND payload_json = ?",
                (approval_id, str(_value(ctx, "user_id", "")), job_id, tool.name, raw_payload),
            ).rowcount
        return changed == 1
    except Exception:
        return False


def _consume_browser_approval(ctx: Any, tool: Tool, approval: Dict[str, Any]) -> bool:
    """Atomically spend one persisted approval across workers and resumes."""

    inspection = _value(ctx, "browser_inspection", {})
    fingerprint = inspection.get("fingerprint") if isinstance(inspection, dict) else None
    approved_fingerprint = approval.get("browser_fingerprint")
    matches = bool(fingerprint and approved_fingerprint and fingerprint == approved_fingerprint)
    approval_id = str(approval.get("id") or "")
    job_id = _value(ctx, "job_id")
    raw_payload = approval.get("approval_payload_json")
    if not approval_id or not job_id or not isinstance(raw_payload, str):
        return False
    try:
        from ..db import get_connection

        with get_connection() as conn:
            cursor = conn.execute(
                "UPDATE runtime_approvals SET status = ? WHERE id = ? AND user_id = ? "
                "AND job_id = ? AND action = ? AND status = ? AND payload_json = ?",
                ("consumed" if matches else "expired", approval_id, str(_value(ctx, "user_id", "")),
                 job_id, tool.name, "approved", raw_payload),
            )
            changed = cursor.rowcount == 1
        # Spend before touching the browser. A timeout or crash can require
        # another confirmation, but cannot replay an uncertain payment.
        return changed and matches
    except Exception:
        return False


async def mark_code_approved(ctx: Any, ttl_seconds: int = 3600) -> None:
    """Compatibility no-op; sandbox code is always allowed by policy."""

    return None


def permission_key_for(tool: Any) -> tuple[Optional[str], bool]:
    """Return ``(permission_key, allow_always)`` for a Tool-like object."""

    try:
        from ..services.permissions import permission_key_for_tool

        return permission_key_for_tool(tool)
    except Exception:
        return None, False


def _permission_always(ctx: Any, key: Optional[str]) -> bool:
    if not key:
        return False
    try:
        from ..services.permissions import permission_mode

        return permission_mode(str(_value(ctx, "user_id", "local") or "local"), key) == "always"
    except Exception:
        return False


def _always_forbidden(tool: Any, permission_key: Optional[str]) -> bool:
    try:
        from ..services.permissions import is_always_forbidden

        from .. import mcp

        metadata = getattr(tool, "metadata", {})
        if getattr(tool, "risk", "") == "local":
            return True
        if isinstance(metadata, dict):
            if mcp.tool_requires_confirmation(metadata):
                return True
            if metadata.get("connector_id"):
                return False
        name = str(getattr(tool, "name", "") or "")
        return is_always_forbidden(name) or is_always_forbidden(permission_key or "")
    except Exception:
        return False


async def decide(ctx: Any, tool: Tool, args: Optional[Dict[str, Any]] = None) -> Decision:
    """Evaluate one call using tool metadata and server-observed browser state.

    The returned action is one of ``allow``, ``confirm`` or ``deny``.  This
    function is intentionally the only permission entry point used by the
    agent loop and widget approval path.
    """
    args = args if isinstance(args, dict) else {}
    if not isinstance(tool, Tool):
        result = Decision("deny", "工具未注册")
        _audit(ctx, Tool("invalid", "", {"type": "object"}, "read", lambda *_: None), result)  # type: ignore[arg-type]
        return result
    if _value(ctx, "capability_sensitive", False) and tool.name in {"luma.memory.create", "luma.notifications.create"}:
        result = Decision("deny", "敏感业务流程内容不能复制到长期记忆或无关通知")
        _audit(ctx, tool, result)
        return result
    mode = str(_value(ctx, "mode", "interactive") or "interactive")
    risk = tool.risk
    permission_key, allow_always = permission_key_for(tool)
    forbidden_always = _always_forbidden(tool, permission_key)
    browser_inspection: Optional[Dict[str, Any]] = None
    if tool.name in {"browser.click", "browser.submit"}:
        # Classification comes from the actual element in the cloud browser,
        # not a model-supplied risk flag. The executor consumes this marker and
        # checks the element again under its lease lock before interacting.
        if isinstance(ctx, dict):
            ctx.pop("browser_approval", None)
            ctx.pop("browser_inspection", None)
        else:
            for name in ("browser_approval", "browser_inspection"):
                if hasattr(ctx, name):
                    setattr(ctx, name, None)
        try:
            from ..services.browser import inspect_click

            browser_inspection = await inspect_click(str(_value(ctx, "user_id", "")), args)
            if not isinstance(browser_inspection, dict):
                raise ValueError("invalid browser inspection")
        except Exception:
            result = Decision("deny", "无法安全检查浏览器操作")
            _audit(ctx, tool, result)
            return result
        if isinstance(ctx, dict):
            ctx["browser_inspection"] = browser_inspection
        else:
            setattr(ctx, "browser_inspection", browser_inspection)
        expected_fingerprint = _value(ctx, "browser_expected_fingerprint")
        if expected_fingerprint and expected_fingerprint != browser_inspection.get("fingerprint"):
            result = Decision("deny", "页面已变化，请重新发起浏览器操作")
            _audit(ctx, tool, result)
            return result
        if tool.name == "browser.submit" or browser_inspection.get("submits") or browser_inspection.get("always_confirm"):
            risk = "external_write"
            permission_key, allow_always = "browser.submit", True
        forbidden_always = forbidden_always or bool(browser_inspection.get("always_confirm"))
    context_confirmed = bool(_value(ctx, "confirmed", False))
    if mode == "background" and tool.name in {"browser.click", "browser.submit"}:
        # A background context is reused by the model loop; it is never a
        # substitute for consuming one durable, owner-bound browser approval.
        context_confirmed = False
    confirmed = context_confirmed or _approved_background_call(ctx, tool, args)
    requires_confirmation = bool(tool.metadata.get("requires_confirmation"))
    if requires_confirmation:
        result = Decision("allow" if confirmed else "confirm", "本次业务写操作需逐次确认" if not confirmed else "已通过本次调用确认", permission_key, False)
    elif risk == "local" or forbidden_always:
        if confirmed:
            result = Decision("allow", "已通过本次调用确认", permission_key, False)
        else:
            reason = "本地设备操作必须逐次确认" if risk == "local" else "删除、批量或支付类工具必须逐次确认"
            result = Decision("confirm", reason, permission_key, False)
    elif risk == "read":
        result = Decision("allow", "只读工具", permission_key, allow_always)
    elif risk == "external_write":
        if confirmed:
            result = Decision("allow", "已通过本次调用确认", permission_key, allow_always)
        elif not permission_key or _permission_always(ctx, permission_key):
            result = Decision("allow", "远端写操作，按默认策略直接执行", permission_key, allow_always)
        elif mode == "background" and permission_key != "browser.submit":
            result = Decision("deny", "后台调用未获得该权限", permission_key, allow_always)
        else:
            result = Decision("confirm", "已设置为每次询问", permission_key, allow_always)
    elif risk == "code":
        result = Decision("allow", "沙箱内操作", permission_key, False)
    elif risk == "write":
        if tool.name in AUTO_APPROVED_WRITE_TOOLS:
            result = Decision("allow", "受控写入操作", permission_key, allow_always)
        elif confirmed:
            result = Decision("allow", "已通过本次调用确认", permission_key, allow_always)
        elif not permission_key or _permission_always(ctx, permission_key):
            result = Decision("allow", "受控写入操作", permission_key, allow_always)
        elif mode == "background":
            result = Decision("deny", "后台调用未获得该权限", permission_key, allow_always)
        else:
            result = Decision("confirm", "已设置为每次询问", permission_key, allow_always)
    else:
        result = Decision("deny", "未知风险等级")
    if browser_inspection is not None and result.decision == "allow":
        marker = {
            "action": tool.name,
            "arguments": dict(args),
            "fingerprint": browser_inspection.get("fingerprint"),
            "always_confirm": bool(browser_inspection.get("always_confirm")),
            "confirmed": confirmed,
            "allow_submission": risk == "external_write",
        }
        if isinstance(ctx, dict):
            ctx["browser_approval"] = marker
        else:
            setattr(ctx, "browser_approval", marker)
    _audit(ctx, tool, result)
    return result


__all__ = ["AUTO_APPROVED_WRITE_TOOLS", "Decision", "decide", "mark_code_approved", "permission_key_for"]
