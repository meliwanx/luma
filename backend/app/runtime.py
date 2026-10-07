"""Durable runtime jobs for safe, server-side assistant work.

This module is deliberately small and provider-independent.  It gives the
assistant a real execution boundary: jobs are persisted before they run,
results and failures survive a process restart, and every transition writes an
activity entry. Uploaded-file reads are bounded and shell execution is routed
through a per-user sandbox plus the approval table; network writes and other
irreversible tools remain disabled.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _runtime_monotonic
from typing import Any, Optional

from .db import get_connection
from .task_dates import normalize_due_at
from .agent_runtime import (
    AgentRuntimeUnavailable,
    config as agent_runtime_config,  # compatibility seam for older embeddings
    ensure_user_runtime,  # compatibility export; shell uses connect_user_sandbox
    touch_user_runtime,
)
from . import agent_runtime
from .services.notifications import create_notification


logger = logging.getLogger(__name__)


SAFE_JOB_TYPES = {"create_task", "create_memory", "briefing", "agent_run", "files", "shell", "sandbox_job"}
IDEMPOTENT_JOB_TYPES = {"briefing", "files"}
READ_ONLY_JOB_TYPES = {"briefing", "files", "list_tasks", "list_memories", "proactive", "feed"}
# Sandbox code is already inside the per-user isolation boundary.  The queue
# still re-checks MCP and business writes through the agent policy, but a
# direct shell or sandbox job never waits for a second runtime approval row.
APPROVAL_JOB_TYPES = {"create_memory", "create_task"}
_RUNTIME_EXECUTOR_LOCK = threading.Lock()
_RUNTIME_EXECUTOR: Optional[ThreadPoolExecutor] = None
_RUNTIME_EXECUTOR_SIZE = 0
_RUNTIME_FUTURES: set[Any] = set()
_RUNTIME_SHUTTING_DOWN = False
_RUNTIME_HEARTBEAT_LOCK = threading.Lock()
_RUNTIME_HEARTBEAT_STOPS: set[threading.Event] = set()

# The registry is deliberately explicit.  A model or client can discover the
# tools, but it cannot turn an arbitrary string into a shell/browser call.
# Files are limited to uploaded user-owned content. Browser interactions and
# shell commands run only inside the per-user Tencent Agent Runtime sandbox.
TOOL_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "name": "briefing",
        "description": "读取待办任务和近期记忆，生成一份本地简报。",
        "kind": "read",
        "requires_approval": False,
        "enabled": True,
        "safety": "只读数据库，不访问网络。",
    },
    {
        "name": "list_tasks",
        "description": "列出当前用户的待办任务。",
        "kind": "read",
        "requires_approval": False,
        "enabled": True,
        "safety": "只读数据库，不修改任务。",
    },
    {
        "name": "list_memories",
        "description": "列出已保存的记忆。",
        "kind": "read",
        "requires_approval": False,
        "enabled": True,
        "safety": "只读数据库，不修改记忆。",
    },
    {
        "name": "create_task",
        "description": "创建一个待办任务。",
        "kind": "write",
        "requires_approval": True,
        "enabled": True,
        "safety": "只写入任务表，可在客户端取消或编辑。",
    },
    {
        "name": "create_memory",
        "description": "保存一条长期记忆。",
        "kind": "write",
        "requires_approval": True,
        "enabled": True,
        "safety": "必须经过用户审批后写入长期记忆。",
    },
    {
        "name": "files",
        "description": "读取当前用户已上传文件的元数据和受限文本内容。",
        "kind": "read",
        "requires_approval": False,
        "enabled": True,
        "safety": "只读取当前用户上传的文件，不暴露服务器路径。",
    },
    {
        "name": "shell",
        "description": "在当前用户的隔离 Runtime 沙箱中运行一条受限命令。",
        "kind": "write",
        "requires_approval": False,
        "enabled": True,
        "safety": "命令只在每用户临时沙箱执行，并受危险命令策略限制。",
    },
    {
        "name": "browser",
        "description": "在隔离浏览器中打开网页并执行受控操作。",
        "kind": "read",
        "requires_approval": False,
        "enabled": True,
        "safety": "浏览器只在云端沙箱操作，表单提交通过 Sentinel 确认，支付和下单逐次确认。",
    },
)
TOOL_BY_NAME = {item["name"]: item for item in TOOL_REGISTRY}

# Persisted routine payloads and older clients may still use the pre-registry
# short names. Normalize them at the runtime boundary; model-visible tools
# remain namespaced entries from ``app.agent.tools``.
LEGACY_AGENT_TOOL_ALIASES = {
    "briefing": "luma.briefing",
    "list_tasks": "luma.tasks.list",
    "list_memories": "luma.memory.search",
    "create_task": "luma.tasks.create",
    "create_memory": "luma.memory.create",
    "files": "luma.files.list",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)


def _decode(value: Optional[str]) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _row(row: Any) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _job(row: Any) -> Optional[dict[str, Any]]:
    data = _row(row)
    if data is None:
        return None
    data["payload"] = _decode(data.pop("payload_json", "{}"))
    data["result"] = _decode(data.pop("result_json", "{}"))
    return data


def _approval(row: Any) -> Optional[dict[str, Any]]:
    data = _row(row)
    if data is None:
        return None
    data["payload"] = _decode(data.pop("payload_json", "{}"))
    if data.get("action") in {"browser.click", "browser.submit"}:
        # The DOM hash is private approval state, not part of the settings or
        # operation details shown to the user.
        data["payload"].pop("_browser_guard", None)
    return data


def _activity(row: Any) -> Optional[dict[str, Any]]:
    return _row(row)


def list_tools() -> list[dict[str, Any]]:
    """Return a copy so API callers cannot mutate the process registry."""

    return [dict(item) for item in TOOL_REGISTRY]


def tool_descriptor(name: str) -> Optional[dict[str, Any]]:
    item = TOOL_BY_NAME.get(name)
    return dict(item) if item else None


def tool_requires_approval(name: str) -> bool:
    item = TOOL_BY_NAME.get(name)
    return bool(item and item["requires_approval"])


def validate_tool(name: str) -> dict[str, Any]:
    item = tool_descriptor(name)
    if item is None:
        raise ValueError(f"Unknown runtime tool: {name}")
    if not item["enabled"]:
        raise RuntimeError(f"Runtime tool '{name}' is disabled: {item['safety']}")
    return item


def job_requires_approval(job_type: str, payload: Optional[dict[str, Any]] = None) -> bool:
    """Return the single approval decision used by both API and workers.

    Unknown job types are deliberately treated as requiring approval.  This
    keeps a newly introduced job from becoming an implicit write path while
    an older API process is still running.
    """

    if job_type in APPROVAL_JOB_TYPES:
        return True
    if job_type == "agent_run":
        if payload is not None and not isinstance(payload, dict):
            return True
        data = payload or {}
        allowed = data.get("allowed_tools") or []
        # A malformed allow-list is a fail-closed case.  The worker will
        # reject it during validation, but requiring approval here prevents an
        # older/missed API gate from treating untrusted payload data as a
        # read-only run.
        if not isinstance(allowed, (list, tuple, set)):
            return True
        # Agent calls are approved at the policy boundary, when the model
        # actually requests a write or external tool.  Do not create a broad
        # approval for the whole prompt before the loop has inspected it;
        # sandbox code is always allowed in background mode.
        legacy_approval_tools = {"create_memory", "create_task"}
        # ``engine`` is retained in persisted payloads for compatibility, but
        # the shared loop is the only engine and ignores old values.
        return any(str(item) in legacy_approval_tools for item in allowed)
    if job_type in READ_ONLY_JOB_TYPES or job_type == "shell":
        return False
    if job_type == "sandbox_job":
        return False
    return True


def _max_attempts(payload: Optional[dict[str, Any]]) -> int:
    """Clamp the client supplied retry count to the server policy."""

    try:
        return min(max(int((payload or {}).get("max_attempts", 3) or 3), 1), 3)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_attempts must be an integer between 1 and 3") from exc


def _job_is_idempotent(job: dict[str, Any]) -> bool:
    job_type = str(job.get("type") or "")
    if job_type in IDEMPOTENT_JOB_TYPES or job_type.startswith("list_"):
        return True
    if job_type != "agent_run":
        return False
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    if bool(payload.get("allow_code", False)):
        return False
    allowed = payload.get("allowed_tools") or []
    if not isinstance(allowed, (list, tuple, set)):
        return False
    for item in allowed:
        name = str(item)
        if name in APPROVAL_JOB_TYPES:
            return False
        # Namespaced registry calls are conservatively non-idempotent unless
        # their explicit read-only suffix is known.  This prevents a retry
        # from repeating an MCP write after a worker crash.
        if name.startswith(("mcp.", "sandbox.", "browser.")) or name == "browser":
            return False
        if name.startswith("luma.") and not name.rsplit(".", 1)[-1] in {"list", "search", "read"}:
            return False
    return True


def append_activity(
    kind: str,
    title: str,
    detail: str = "",
    *,
    job_id: Optional[str] = None,
    approval_id: Optional[str] = None,
    user_id: str = "local",
    notify: bool = True,
) -> dict[str, Any]:
    data = {
        "id": new_id("act"),
        "user_id": user_id,
        "kind": kind,
        "title": title,
        "detail": detail,
        "job_id": job_id,
        "approval_id": approval_id,
        "created_at": now(),
    }
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO runtime_activity(id,user_id,kind,title,detail,job_id,approval_id,created_at) "
            "VALUES (:id,:user_id,:kind,:title,:detail,:job_id,:approval_id,:created_at)",
            data,
        )
    if notify and kind in {"job_succeeded", "job_failed", "approval_requested", "approval_decided"}:
        try:
            create_notification(user_id, "runtime", title, detail)
        except Exception:
            # Activity is durable even if notification delivery is unavailable.
            pass
    return data


def create_job(
    job_type: str,
    payload: dict[str, Any],
    run_at: Optional[str] = None,
    *,
    requires_approval: bool = False,
    user_id: str = "local",
) -> dict[str, Any]:
    if job_type not in SAFE_JOB_TYPES:
        raise ValueError(f"Unsupported runtime job type: {job_type}")
    payload = dict(payload or {})
    max_attempts = _max_attempts(payload)
    payload["max_attempts"] = max_attempts
    timestamp = now()
    data = {
        "id": new_id("job"),
        "user_id": user_id,
        "type": job_type,
        "status": "waiting_approval" if requires_approval else "queued",
        "payload_json": _json(payload),
        "result_json": "{}",
        "error": None,
        "attempts": 0,
        "run_at": run_at or timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO runtime_jobs(id,user_id,type,status,payload_json,result_json,error,attempts,run_at,created_at,updated_at) "
            "VALUES (:id,:user_id,:type,:status,:payload_json,:result_json,:error,:attempts,:run_at,:created_at,:updated_at)",
            data,
        )
    append_activity(
        "approval_requested" if requires_approval else "job_queued",
        "需要确认后才能继续" if requires_approval else f"已排队：{job_type}",
        "runtime worker 将在后台执行。" if not requires_approval else "这项操作会写入你的长期数据。",
        job_id=data["id"],
        user_id=user_id,
    )
    return _job(data)


def list_jobs(limit: int = 50, user_id: str = "local") -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM runtime_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [item for row in rows if (item := _job(row)) is not None]


def get_job(job_id: str, user_id: str = "local") -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    return _job(row)


def list_approvals(limit: int = 50, user_id: str = "local") -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM runtime_approvals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [item for row in rows if (item := _approval(row)) is not None]


def create_approval(job_id: str, action: str, payload: dict[str, Any], user_id: str = "local", *, browser_fingerprint: Optional[str] = None) -> dict[str, Any]:
    timestamp = now()
    stored_payload = dict(payload)
    if action in {"browser.click", "browser.submit"}:
        # Only the server's observed DOM fingerprint can bind a browser
        # approval. Client/model payloads cannot supply this reserved state.
        stored_payload.pop("_browser_guard", None)
        if browser_fingerprint:
            stored_payload["_browser_guard"] = {"fingerprint": str(browser_fingerprint)}
    data = {
        "id": new_id("approval"),
        "user_id": user_id,
        "job_id": job_id,
        "action": action,
        "status": "pending",
        "payload_json": _json(stored_payload),
        "decision_note": None,
        "created_at": timestamp,
        "decided_at": None,
    }
    with get_connection() as conn:
        job_row = conn.execute("SELECT status FROM runtime_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        if job_row is None:
            raise ValueError("Runtime job not found")
        # A background agent creates its approval while the worker row is
        # still ``running``; the worker transitions it to waiting after the
        # loop returns a checkpoint.  API callers may also add approvals to
        # queued or already waiting jobs.
        if job_row["status"] not in {"queued", "running", "waiting_approval"}:
            raise ValueError("Runtime job is not waiting for approval")
        conn.execute(
            "INSERT INTO runtime_approvals(id,user_id,job_id,action,status,payload_json,decision_note,created_at,decided_at) "
            "VALUES (:id,:user_id,:job_id,:action,:status,:payload_json,:decision_note,:created_at,:decided_at)",
            data,
        )
        conn.execute(
            "UPDATE runtime_jobs SET status = 'waiting_approval', updated_at = ? WHERE id = ? AND user_id = ? AND status = 'queued'",
            (timestamp, job_id, user_id),
        )
    append_activity("approval_requested", "需要确认后才能继续", action, job_id=job_id, approval_id=data["id"], user_id=user_id)
    return _approval(data)


def decide_approval(approval_id: str, decision: str, note: Optional[str] = None, user_id: str = "local") -> Optional[dict[str, Any]]:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Invalid approval decision")
    timestamp = now()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM runtime_approvals WHERE id = ? AND user_id = ?", (approval_id, user_id)).fetchone()
        approval = _approval(row)
        if approval is None:
            return None
        if approval["status"] != "pending":
            return approval
        conn.execute(
            "UPDATE runtime_approvals SET status = ?, decision_note = ?, decided_at = ? WHERE id = ? AND user_id = ?",
            (decision, note, timestamp, approval_id, user_id),
        )
        conn.execute(
            "UPDATE runtime_jobs SET status = ?, updated_at = ? WHERE id = ? AND user_id = ? AND status = 'waiting_approval'",
            ("queued" if decision == "approved" else "cancelled", timestamp, approval["job_id"], user_id),
        )
        row = conn.execute("SELECT * FROM runtime_approvals WHERE id = ? AND user_id = ?", (approval_id, user_id)).fetchone()
    result = _approval(row)
    append_activity(
        "approval_decided",
        "已批准" if decision == "approved" else "已拒绝",
        note or "",
        job_id=approval["job_id"],
        approval_id=approval_id,
        user_id=user_id,
    )
    return result


def list_activity(limit: int = 100, user_id: str = "local") -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM runtime_activity WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [item for row in rows if (item := _activity(row)) is not None]


def cancel_job(job_id: str, user_id: str = "local") -> Optional[dict[str, Any]]:
    timestamp = now()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE runtime_jobs SET status = 'cancelled', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status IN ('queued','waiting_approval','paused')",
            (timestamp, job_id, user_id),
        )
        if cursor.rowcount == 0:
            row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    data = _job(row)
    if data and data["status"] == "cancelled":
        append_activity("job_cancelled", "后台任务已取消", job_id=job_id, user_id=user_id)
    return data


def pause_job(job_id: str, user_id: str = "local") -> Optional[dict[str, Any]]:
    """Pause a queued job at a durable checkpoint boundary."""

    timestamp = now()
    with get_connection() as conn:
        conn.execute(
            "UPDATE runtime_jobs SET status = 'paused', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'queued'",
            (timestamp, job_id, user_id),
        )
        row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    data = _job(row)
    if data and data["status"] == "paused":
        append_activity("job_paused", "后台任务已暂停", "可从当前检查点恢复。", job_id=job_id, user_id=user_id)
    return data


def resume_job(job_id: str, user_id: str = "local") -> Optional[dict[str, Any]]:
    """Resume a paused job without losing its previous attempt metadata."""

    timestamp = now()
    with get_connection() as conn:
        conn.execute(
            "UPDATE runtime_jobs SET status = 'queued', run_at = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'paused'",
            (timestamp, timestamp, job_id, user_id),
        )
        row = conn.execute("SELECT * FROM runtime_jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    data = _job(row)
    if data and data["status"] == "queued":
        append_activity("job_resumed", "后台任务已恢复", "worker 将从当前检查点继续。", job_id=job_id, user_id=user_id)
    return data


def _payload_text(payload: dict[str, Any], key: str, *, required: bool = True, max_length: int = 20_000) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    if len(value) > max_length:
        raise ValueError(f"{key} is too long")
    return value


def _execute_create_task(payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    title = _payload_text(payload, "title", max_length=300)
    description = _payload_text(payload, "description", required=False)
    due_at = normalize_due_at(payload.get("due_at"))
    timestamp = now()
    task_id = new_id("task")
    data = {
        "id": task_id,
        "user_id": user_id,
        "title": title,
        "description": description,
        "status": payload.get("status", "todo") if payload.get("status", "todo") in {"todo", "in_progress"} else "todo",
        "due_at": due_at,
        "created_at": timestamp,
        "updated_at": timestamp,
        "metadata_json": _json(payload.get("metadata", {})),
    }
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO tasks(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json) "
            "VALUES (:id,:user_id,:title,:description,:status,:due_at,:created_at,:updated_at,:metadata_json)",
            data,
        )
    return {"task_id": task_id, "title": title, "status": data["status"]}


def _execute_create_memory(payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    content = _payload_text(payload, "content")
    category = _payload_text(payload, "category", required=False, max_length=80) or "general"
    importance = payload.get("importance", 3)
    if not isinstance(importance, int) or not 1 <= importance <= 5:
        raise ValueError("importance must be an integer between 1 and 5")
    timestamp = now()
    memory_id = new_id("mem")
    data = {
        "id": memory_id,
        "user_id": user_id,
        "content": content,
        "category": category,
        "importance": importance,
        "created_at": timestamp,
        "updated_at": timestamp,
        "metadata_json": _json(payload.get("metadata", {})),
    }
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO memories(id,user_id,content,category,importance,created_at,updated_at,metadata_json) "
            "VALUES (:id,:user_id,:content,:category,:importance,:created_at,:updated_at,:metadata_json)",
            data,
        )
    return {"memory_id": memory_id, "category": category, "importance": importance}


def _execute_briefing(_: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    with get_connection() as conn:
        tasks = conn.execute(
            "SELECT id,title,status,due_at FROM tasks WHERE user_id = ? AND status NOT IN ('done','cancelled') "
            "ORDER BY due_at IS NULL, due_at, updated_at DESC LIMIT 10"
            , (user_id,)
        ).fetchall()
        memories = conn.execute(
            "SELECT id,content,category,importance FROM memories WHERE user_id = ? ORDER BY importance DESC, updated_at DESC LIMIT 5",
            (user_id,),
        ).fetchall()
    return {
        "pending_tasks": [dict(row) for row in tasks],
        "recent_memories": [dict(row) for row in memories],
        "generated_at": now(),
    }


def _execute_list_tasks(_: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id,title,description,status,due_at,updated_at FROM tasks WHERE user_id = ? "
            "ORDER BY due_at IS NULL, due_at, updated_at DESC LIMIT 50"
            , (user_id,)
        ).fetchall()
    return {"tasks": [dict(row) for row in rows]}


def _execute_list_memories(_: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id,content,category,importance,updated_at FROM memories WHERE user_id = ? "
            "ORDER BY importance DESC, updated_at DESC LIMIT 50"
            , (user_id,)
        ).fetchall()
    return {"memories": [dict(row) for row in rows]}


def _runtime_file_path(storage_key: str) -> Path:
    root = Path(os.getenv("ASSISTANT_FILE_ROOT", str(Path(__file__).resolve().parents[1] / "data" / "files"))).expanduser().resolve()
    candidate = (root / storage_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("文件存储路径无效") from exc
    return candidate


def _execute_files(payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    file_id = payload.get("file_id")
    limit = min(max(int(payload.get("limit", 20) or 20), 1), 100)
    with get_connection() as conn:
        if file_id:
            row = conn.execute(
                "SELECT id,filename,media_type,size_bytes,sha256,storage_key,storage,session_id,created_at FROM files WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                (str(file_id), user_id),
            ).fetchone()
            if row is None:
                raise ValueError("File not found")
            record = dict(row)
            storage_name = str(record.pop("storage") or "local")
            storage_key = str(record.pop("storage_key") or "")
            suffix = Path(record["filename"]).suffix.lower()
            text_extensions = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".sh", ".log"}
            if record["media_type"].startswith("text/") or suffix in text_extensions:
                try:
                    from .storage import storage_for_row
                    raw = storage_for_row({"storage": storage_name}).get(storage_key, max_bytes=32 * 1024)
                    record["content"] = raw.decode("utf-8", errors="replace")
                except Exception:
                    record["content"] = None
            record["truncated"] = bool(record.get("size_bytes", 0) > 32 * 1024)
            return {"file": record}
        rows = conn.execute(
            "SELECT id,filename,media_type,size_bytes,sha256,session_id,created_at FROM files WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return {"files": [dict(row) for row in rows]}


_SHELL_DENY = re.compile(r"(?:^|[;&|`$()<>])\s*(?:sudo|su|ssh|scp|curl|wget|nc|ncat|docker|podman|systemctl|shutdown|reboot)\b|\brm\s+-rf\b|/etc/shadow|/root/\.ssh", re.I)


def _execute_shell(payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    command = str(payload.get("command") or "").strip()
    if not command or len(command) > 4_000:
        raise ValueError("command must be 1-4000 characters")
    if "\x00" in command or _SHELL_DENY.search(command):
        raise ValueError("command rejected by sandbox policy")
    timeout = min(max(int(payload.get("timeout_seconds", 30) or 30), 1), 120)
    try:
        # S1 owns recovery/creation of the provider sandbox.  Keeping this
        # call behind the adapter avoids a local subprocess fallback and lets
        # paused sandboxes resume with their persistent workspace.
        box = agent_runtime.connect_user_sandbox(user_id)
        result = box.commands.run(command, timeout=timeout)
        return {
            "command": command,
            "exit_code": int(getattr(result, "exit_code", 0) or 0),
            "stdout": str(getattr(result, "stdout", "") or "")[:32 * 1024],
            "stderr": str(getattr(result, "stderr", "") or "")[:16 * 1024],
        }
    except AgentRuntimeUnavailable:
        raise
    except Exception as exc:
        raise RuntimeError(f"sandbox command failed: {type(exc).__name__}") from exc
    finally:
        # A command is activity, not a lease release.  The provider adapter
        # renews the idle TTL while preserving a user-opened/shared sandbox.
        try:
            touch_user_runtime(user_id)
        except Exception:
            pass


def _sandbox_job_payload(payload: dict[str, Any]) -> tuple[str, int]:
    """Validate a long-running sandbox command at the worker boundary."""

    command = str(payload.get("command") or "").strip()
    if not command or len(command) > 4_000:
        raise ValueError("command must be 1-4000 characters")
    if "\x00" in command or _SHELL_DENY.search(command):
        raise ValueError("command rejected by sandbox policy")
    try:
        timeout = int(payload.get("timeout_seconds", 600) or 600)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be an integer") from exc
    return command, min(max(timeout, 1), 1_800)


def _wait_background_command(handle: Any, timeout_seconds: int, user_id: str) -> Any:
    """Wait for an E2B CommandHandle while renewing the sandbox lease.

    SDK releases have exposed ``wait`` and ``result`` at different times.  A
    tiny waiter thread isolates that provider detail and lets this worker
    renew the user runtime every minute without holding a database connection.
    """

    result_holder: list[Any] = []
    error_holder: list[BaseException] = []
    done = threading.Event()

    def wait_for_result() -> None:
        try:
            waiter = getattr(handle, "wait", None)
            if callable(waiter):
                try:
                    result_holder.append(waiter())
                except TypeError:
                    result_holder.append(waiter(timeout=timeout_seconds))
            else:
                resolver = getattr(handle, "result", None)
                result_holder.append(resolver() if callable(resolver) else handle)
        except BaseException as exc:  # propagate on the worker thread
            error_holder.append(exc)
        finally:
            done.set()

    threading.Thread(target=wait_for_result, name="sandbox-job-wait", daemon=True).start()
    deadline = _runtime_monotonic() + timeout_seconds
    while not done.wait(min(60.0, max(0.1, deadline - _runtime_monotonic()))):
        try:
            touch_user_runtime(user_id)
        except Exception:
            pass
        if _runtime_monotonic() >= deadline:
            killer = getattr(handle, "kill", None)
            if callable(killer):
                try:
                    killer()
                except Exception:
                    pass
            raise TimeoutError("sandbox job timed out")
    if error_holder:
        raise error_holder[0]
    return result_holder[0] if result_holder else handle


def _sandbox_job_result(result: Any, command: str, status: str = "succeeded") -> dict[str, Any]:
    exit_code = getattr(result, "exit_code", None)
    try:
        exit_code = int(exit_code) if exit_code is not None else (0 if status == "succeeded" else 1)
    except (TypeError, ValueError):
        exit_code = 1 if status != "succeeded" else 0
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    return {
        "kind": "sandbox_job",
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "stdout_tail": stdout[-4_096:],
        "stderr_tail": stderr[-4_096:],
    }


def _execute_sandbox_job(payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    """Run a bounded command in the persistent user sandbox.

    ``sandbox.job.start`` persists this payload and returns immediately; the
    queue worker is the only caller that starts the provider background
    command.  No host subprocess or model-controlled command runs here.
    """

    command, timeout = _sandbox_job_payload(payload)
    box = agent_runtime.connect_user_sandbox(user_id)
    handle = box.commands.run(command, background=True)
    try:
        result = _wait_background_command(handle, timeout, user_id)
        data = _sandbox_job_result(result, command, "succeeded")
        if int(data.get("exit_code", 0) or 0) != 0:
            data["status"] = "failed"
            data["error"] = "exit_code"
        return data
    except TimeoutError:
        return _sandbox_job_result(handle, command, "failed") | {"error": "timeout"}
    finally:
        try:
            touch_user_runtime(user_id)
        except Exception:
            pass


def _sandbox_job_assistant_message(job: dict[str, Any], result: dict[str, Any], status: str) -> None:
    """Persist a plain-text completion message in the initiating session."""

    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return
    exit_code = result.get("exit_code")
    stdout_tail = str(result.get("stdout_tail") or "")[-4_096:]
    stderr_tail = str(result.get("stderr_tail") or "")[-4_096:]
    lines = ["沙箱后台任务状态：%s" % status, "退出码：%s" % (exit_code if exit_code is not None else "未知")]
    output_tail = ""
    if stdout_tail:
        output_tail += "标准输出末尾：\n" + stdout_tail
    if stderr_tail:
        output_tail += ("\n" if output_tail else "") + "标准错误末尾：\n" + stderr_tail
    if output_tail:
        # Keep the combined untrusted output bounded. The status and exit
        # code remain visible outside this tail.
        lines.append(output_tail[-4_096:])
    content = "\n".join(lines)
    try:
        with get_connection() as conn:
            session = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?", (session_id, job.get("user_id", "local"))
            ).fetchone()
            if session is None:
                return
            message_id = new_id("msg")
            timestamp = now()
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    job.get("user_id", "local"),
                    session_id,
                    "assistant",
                    content,
                    timestamp,
                    _json({"runtime_job_id": job.get("id"), "sandbox_job": True}),
                    "complete",
                ),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND user_id = ?", (timestamp, session_id, job.get("user_id", "local")))
    except Exception:
        logger.debug("sandbox job assistant message failed", exc_info=True)


def execute_tool(name: str, payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    """Execute exactly one registered safe tool.

    This is the common gate used by direct tool calls and the agent loop. It
    intentionally raises for disabled browser/network tools instead of falling
    back to a local subprocess or an unrestricted HTTP client.
    """

    validate_tool(name)
    if name == "briefing":
        return _execute_briefing(payload, user_id)
    if name == "list_tasks":
        return _execute_list_tasks(payload, user_id)
    if name == "list_memories":
        return _execute_list_memories(payload, user_id)
    if name == "create_task":
        return _execute_create_task(payload, user_id)
    if name == "create_memory":
        return _execute_create_memory(payload, user_id)
    if name == "files":
        return _execute_files(payload, user_id)
    if name == "shell":
        return _execute_shell(payload, user_id)
    # validate_tool above handles disabled tools; this guard protects future
    # registry additions from accidentally becoming executable.
    raise RuntimeError(f"Runtime tool '{name}' has no executor")


def _allowed_tools(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("allowed_tools", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("allowed_tools must be a list of tool names")
    names = list(dict.fromkeys(LEGACY_AGENT_TOOL_ALIASES.get(str(item), str(item)) for item in raw))
    dynamic_names: set[str] = set()
    dynamic_read_names: set[str] = set()
    try:
        from .agent.tools import registry_for

        registered, _ = registry_for(str(payload.get("user_id") or "local"), mode="background")
        dynamic_names = {
            str(getattr(item, "name", item.get("name", "") if isinstance(item, dict) else ""))
            for item in registered
        }
        dynamic_read_names = {
            str(getattr(item, "name", item.get("name", "") if isinstance(item, dict) else ""))
            for item in registered
            if str(getattr(item, "risk", item.get("risk", "") if isinstance(item, dict) else "")) == "read"
        }
        dynamic_names.discard("")
    except Exception:
        dynamic_names = set()
    if "browser" in names:
        names = list(dict.fromkeys(
            [name for name in names if name != "browser"]
            + sorted(name for name in dynamic_names if name.startswith("browser."))
        ))
    for name in names:
        if name in dynamic_names or name.startswith(("luma.", "mcp.", "sandbox.", "browser.")):
            continue
        validate_tool(name)
    # Read-only briefing is the safe default for a prompt-only run when the
    # unified registry cannot be loaded during a rolling deployment.
    return names or (["luma.briefing"] if not dynamic_names else sorted(dynamic_read_names))


def _execute_agent_run(payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    prompt = _payload_text(payload, "prompt", max_length=20_000)
    # ``engine`` remains accepted in persisted payloads for rolling upgrades,
    # but model-backed runs always use the shared agent loop.
    engine = str(payload.get("engine", "auto") or "auto").strip().lower()
    if engine != "auto":
        engine = "auto"
    session_id = payload.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError("session_id must be a non-empty string")
    allowed = _allowed_tools({**payload, "user_id": user_id})

    from .provider import local_mode

    if local_mode():
        # Local mode is a deterministic preview, as in chat and background
        # generators. A prompt cannot plan tool calls without a model, and
        # credentials in .env must not silently switch this run online.
        from .services.chat import local_reply

        reply = local_reply(prompt)
        return {
            "prompt": prompt,
            "engine": "auto",
            "allowed_tools": allowed,
            "provider": "local",
            "status": "completed",
            "reply": reply,
            "message": reply,
            "tool_calls": [],
        }

    # Import lazily so runtime queue maintenance remains usable when an
    # optional provider dependency is unavailable during process startup.
    try:
        from .agent.loop import run_agent
    except ImportError as exc:
        raise RuntimeError("统一 Agent 循环不可用") from exc

    class _RuntimeContext:
        def __init__(self) -> None:
            self.user_id = user_id
            self.session_id = session_id or f"job-{uuid.uuid4().hex}"
            self.mode = "background"
            self.assistant_message_id = None
            self.job_id = payload.get("job_id")
            self.allowed_tools = allowed
            self.allow_code = bool(payload.get("allow_code", False))
            self.job_approval_granted = bool(
                payload.get("job_id")
                and _agent_run_code_approval_granted(
                    {"id": payload.get("job_id"), "user_id": user_id}
                )
            )
            self.approved_calls: list[dict[str, Any]] = []
            if payload.get("job_id"):
                try:
                    with get_connection() as conn:
                        rows = conn.execute(
                            "SELECT id, action, payload_json FROM runtime_approvals "
                            "WHERE job_id = ? AND user_id = ? AND status = 'approved'",
                            (payload.get("job_id"), user_id),
                        ).fetchall()
                    for row in rows:
                        item = dict(row)
                        raw_payload = item.pop("payload_json", "{}")
                        item["payload"] = _decode(raw_payload)
                        if item.get("action") in {"browser.click", "browser.submit"}:
                            guard = item["payload"].pop("_browser_guard", {})
                            item["browser_fingerprint"] = guard.get("fingerprint") if isinstance(guard, dict) else None
                            item["approval_payload_json"] = raw_payload
                        self.approved_calls.append(item)
                except Exception:
                    self.approved_calls = []

        async def request_approval(self, tool: Any, args: Any) -> dict[str, Any]:
            """Persist a background approval using the durable runtime row.

            The unified loop feature-detects this callback so it can remain
            independent from the queue implementation.  Only the server-side
            arguments supplied by the loop are persisted.
            """
            name = getattr(tool, "name", None) or (tool if isinstance(tool, str) else "agent.tool")
            data = args if isinstance(args, dict) else {}
            job_id = payload.get("job_id")
            if not job_id:
                return {"status": "waiting_approval", "reason": "运行作业缺少 job_id"}
            inspection = getattr(self, "browser_inspection", None)
            fingerprint = inspection.get("fingerprint") if str(name) in {"browser.click", "browser.submit"} and isinstance(inspection, dict) else None
            if str(name) in {"browser.click", "browser.submit"}:
                approval = create_approval(str(job_id), str(name), data, user_id, browser_fingerprint=fingerprint)
            else:
                approval = create_approval(str(job_id), str(name), data, user_id)
            result = {"status": "waiting_approval", "approval_id": approval.get("id"), "reason": "需要确认后才能继续"}
            try:
                try:
                    from .agent.policy import permission_key_for
                except (ImportError, AttributeError):
                    from .services.permissions import permission_key_for_tool as permission_key_for

                key, allow_always = permission_key_for(tool)
                if key:
                    result.update(permission_key=str(key), allow_always=bool(allow_always))
            except Exception:
                pass
            return result

    messages = [{"role": "user", "content": prompt}]
    emitted: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        # The worker persists the final result.  Keep event data bounded and
        # structured for callers that inspect a running job checkpoint.
        if isinstance(event, dict):
            from .services.browser_events import without_browser_credentials

            emitted.append(without_browser_credentials({"type": str(event.get("type", "event")), **event}))

    import asyncio

    async def execute_agent() -> Any:
        try:
            return await run_agent(_RuntimeContext(), messages, emit=emit)
        finally:
            # Background workers use a temporary loop, so its CDP clients
            # must close before asyncio.run destroys their transports.
            from .services.browser import close_connections

            await close_connections()

    outcome = asyncio.run(execute_agent())
    if isinstance(outcome, dict):
        from .services.browser_events import without_browser_credentials

        result = without_browser_credentials(dict(outcome))
    else:
        result = {
            key: getattr(outcome, key)
            for key in ("reply", "message", "tool_calls", "status", "events")
            if hasattr(outcome, key)
        }
    result.setdefault("prompt", prompt)
    result.setdefault("engine", "auto")
    result.setdefault("allowed_tools", allowed)
    if emitted:
        result.setdefault("events", emitted)
    calls = result.get("tool_calls")
    if isinstance(calls, list) and calls:
        first_call = calls[0] if isinstance(calls[0], dict) else {}
        tool_name = first_call.get("tool") or first_call.get("name")
        if tool_name:
            result.setdefault("tool", str(tool_name))
    result.setdefault("message", result.get("reply", "Agent 运行完成"))
    return result


def execute_job(job_type: str, payload: dict[str, Any], user_id: str = "local") -> dict[str, Any]:
    if job_type in {"proactive", "feed"}:
        import asyncio
        import httpx
        from .provider import async_client_context, local_mode

        # Explicit local mode has no model/browser generator. In particular,
        # development and test lifespans must not use credentials in .env.
        if local_mode():
            return {"evaluated": False, "sent": False, "kind": None, "reason": None} if job_type == "proactive" else {"generated": False}

        async def generate():
            async with httpx.AsyncClient() as client:
                with async_client_context(client):
                    if job_type == "proactive":
                        from .services.proactive import evaluate_user

                        return await evaluate_user(user_id)
                    from .services.feed import generate_post
                    from .services.browser import close_connections

                    try:
                        return await generate_post(user_id, manual=bool(payload.get("manual")), job_id=payload.get("job_id"))
                    finally:
                        await close_connections()

        return asyncio.run(generate())
    if job_type == "create_task":
        return _execute_create_task(payload, user_id)
    if job_type == "create_memory":
        return _execute_create_memory(payload, user_id)
    if job_type == "briefing":
        return _execute_briefing(payload, user_id)
    if job_type == "agent_run":
        return _execute_agent_run(payload, user_id)
    if job_type == "files":
        return _execute_files(payload, user_id)
    if job_type == "shell":
        return _execute_shell(payload, user_id)
    if job_type == "sandbox_job":
        return _execute_sandbox_job(payload, user_id)
    raise ValueError(f"Unsupported runtime job type: {job_type}")


def _worker_concurrency() -> int:
    try:
        value = int(os.getenv("RUNTIME_WORKER_CONCURRENCY", "3"))
    except (TypeError, ValueError):
        value = 3
    return min(max(value, 1), 16)


def _claim_due_jobs(limit: int) -> list[dict[str, Any]]:
    """Claim jobs atomically; PostgreSQL workers skip rows locked by peers."""

    timestamp = now()
    claimed: list[dict[str, Any]] = []
    with get_connection() as conn:
        query = (
            "SELECT * FROM runtime_jobs WHERE status = 'queued' AND run_at <= ? "
            "ORDER BY run_at, created_at LIMIT ?"
        )
        query += " FOR UPDATE SKIP LOCKED"
        rows = conn.execute(query, (timestamp, limit)).fetchall()
        for raw in rows:
            job = _job(raw)
            if not job:
                continue
            job_id = job["id"]
            user_id = job.get("user_id", "local")
            cursor = conn.execute(
                "UPDATE runtime_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND status = 'queued'",
                (timestamp, job_id, user_id),
            )
            if getattr(cursor, "rowcount", 0):
                claimed.append(job)
    return claimed


def _heartbeat_loop(job_id: str, user_id: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(30.0):
        try:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE runtime_jobs SET updated_at = ? WHERE id = ? AND user_id = ? AND status = 'running'",
                    (now(), job_id, user_id),
                )
        except Exception:
            # A transient heartbeat failure must not interrupt the actual job.
            continue


def start_runtime() -> None:
    """Allow runtime queue polling after a previous lifespan has ended."""

    global _RUNTIME_SHUTTING_DOWN
    with _RUNTIME_EXECUTOR_LOCK:
        _RUNTIME_SHUTTING_DOWN = False


def shutdown_runtime() -> None:
    """Stop process-level runtime workers during application shutdown.

    The queue executor is intentionally shared by all polling iterations.  A
    lifespan shutdown therefore has to release it explicitly; otherwise its
    worker threads (and any per-job heartbeat loops) can outlive the worker
    task and keep writing after the application has shut down. Running jobs
    finish their cleanup before this returns; queued futures are cancelled.
    """

    global _RUNTIME_EXECUTOR, _RUNTIME_EXECUTOR_SIZE, _RUNTIME_SHUTTING_DOWN

    # Wake all heartbeat loops first. ``_run_claimed_job`` still joins its own
    # heartbeat in its finally block, so this is safe even when a job is
    # currently executing a provider call.
    with _RUNTIME_HEARTBEAT_LOCK:
        heartbeat_stops = tuple(_RUNTIME_HEARTBEAT_STOPS)
    for stop_event in heartbeat_stops:
        stop_event.set()

    with _RUNTIME_EXECUTOR_LOCK:
        _RUNTIME_SHUTTING_DOWN = True
        executor = _RUNTIME_EXECUTOR
        _RUNTIME_EXECUTOR = None
        _RUNTIME_EXECUTOR_SIZE = 0
        # Futures are only local bookkeeping. Job state is persisted by each
        # worker; clearing the set prevents a later process startup from
        # retaining completed futures from the old lifecycle.
        _RUNTIME_FUTURES.clear()
    if executor is not None:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:  # pragma: no cover - Python 3.8 compatibility
            executor.shutdown(wait=True)


def _approval_granted(job: dict[str, Any]) -> bool:
    """Return whether any approval has been granted for a job.

    This broad check is retained for the durable worker gate, where the
    approval action is the operation the worker is about to execute.  Agent
    code execution uses ``_agent_run_code_approval_granted`` below so a
    previously approved per-call MCP write cannot grant job-level code
    access.
    """

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM runtime_approvals WHERE job_id = ? AND user_id = ? AND status = 'approved' LIMIT 1",
            (job["id"], job.get("user_id", "local")),
        ).fetchone()
    return row is not None


def _agent_run_code_approval_granted(job: dict[str, Any]) -> bool:
    """Return whether an agent job has its explicit code approval.

    A job-level approval is represented by the ``agent_run`` row created when
    the job requires approval.  Per-tool approvals (for example an MCP write)
    must remain scoped to that individual call and therefore cannot satisfy
    this check.  The payload is decoded and checked for the exact JSON boolean
    ``allow_code: true`` so malformed or truthy string values fail closed.
    """

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runtime_approvals "
            "WHERE job_id = ? AND user_id = ? AND action = ? "
            "AND status = ? LIMIT 1",
            (job["id"], job.get("user_id", "local"), "agent_run", "approved"),
        ).fetchall()
    for row in rows:
        payload_json = row["payload_json"] if isinstance(row, dict) else row[0]
        if _decode(payload_json).get("allow_code") is True:
            return True
    return False


def _mark_job_failed(job: dict[str, Any], detail: str, attempt: int) -> None:
    finished = now()
    user_id = job.get("user_id", "local")
    with get_connection() as conn:
        conn.execute(
            "UPDATE runtime_jobs SET status = 'failed', error = ?, result_json = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'running'",
            (
                detail[:1000],
                _json({"checkpoint": {"phase": "failed", "attempt": attempt, "error": detail[:500], "updated_at": finished}}),
                finished,
                job["id"],
                user_id,
            ),
        )
    try:
        append_activity("job_failed", f"执行失败：{job['type']}", detail[:1000], job_id=job["id"], user_id=user_id, notify=not _is_routine_job(job))
    except Exception:
        pass
    _routine_job_finished(job, "failed", detail=detail)


def _safe_exception_detail(exc: BaseException) -> str:
    """Keep provider/database exception text out of durable activity logs."""

    return type(exc).__name__


def _routine_job_finished(
    job: dict[str, Any],
    status: str,
    result: Any = None,
    detail: Optional[str] = None,
) -> None:
    """Best-effort callback for jobs created by the routine scheduler."""

    if str(status).lower() in {"waiting_approval", "waiting_confirmation", "paused"}:
        return
    if str(job.get("type") or "") != "agent_run":
        return
    payload = job.get("payload") or {}
    if not isinstance(payload, dict) or not payload.get("routine_id"):
        return
    try:
        from .scheduler import routine_job_finished

        routine_job_finished(job, status, result=result, detail=detail)
    except Exception:
        # A completion notification must never change the durable job result.
        pass


def _is_routine_job(job: dict[str, Any]) -> bool:
    payload = job.get("payload") or {}
    return str(job.get("type") or "") == "agent_run" and isinstance(payload, dict) and bool(payload.get("routine_id"))


def _run_claimed_job(job: dict[str, Any]) -> bool:
    job_id = job["id"]
    user_id = job.get("user_id", "local")
    attempt = int(job.get("attempts") or 0) + 1
    started = now()
    try:
        append_activity("job_started", f"开始执行：{job['type']}", job_id=job_id, user_id=user_id)
    except Exception:
        pass

    # This check remains in the worker so a missed API gate cannot execute a
    # write.  The approval row is scoped by both job and owner.
    if job_requires_approval(str(job.get("type") or ""), job.get("payload")) and not _approval_granted(job):
        _mark_job_failed(job, "需要审批后才能执行", attempt)
        return True

    job_payload = dict(job.get("payload") or {})
    if job.get("type") in {"agent_run", "sandbox_job", "feed", "proactive"}:
        job_payload.setdefault("session_id", f"job-{job_id}")
        job_payload.setdefault("job_id", job_id)
    checkpoint = {"phase": "running", "attempt": attempt, "updated_at": started}
    with get_connection() as conn:
        conn.execute(
            "UPDATE runtime_jobs SET result_json = ?, updated_at = ? WHERE id = ? AND user_id = ? AND status = 'running'",
            (_json({"checkpoint": checkpoint}), started, job_id, user_id),
        )

    heartbeat_stop = threading.Event()
    with _RUNTIME_HEARTBEAT_LOCK:
        _RUNTIME_HEARTBEAT_STOPS.add(heartbeat_stop)
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(job_id, user_id, heartbeat_stop), daemon=True)
    heartbeat.start()
    try:
        result = execute_job(str(job["type"]), job_payload, user_id)
        finished = now()
        result = dict(result or {})
        # A sandbox command can finish with a provider-level timeout while
        # still returning a structured result. Persist that terminal failure
        # instead of treating the worker call itself as successful.
        if str(job.get("type") or "") == "sandbox_job" and str(result.get("status") or "").lower() == "failed":
            result["checkpoint"] = {"phase": "failed", "attempt": attempt, "updated_at": finished}
            with get_connection() as conn:
                conn.execute(
                    "UPDATE runtime_jobs SET status = 'failed', result_json = ?, error = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND status = 'running'",
                    (_json(result), str(result.get("error") or "sandbox job failed")[:1000], finished, job_id, user_id),
                )
            _sandbox_job_assistant_message(job, result, "failed")
            try:
                append_activity("job_failed", "沙箱后台任务失败", str(result.get("error") or "执行失败")[:1000], job_id=job_id, user_id=user_id)
            except Exception:
                pass
            return True
        if str(result.get("status", "")).lower() in {"waiting_approval", "needs_approval", "paused"}:
            result["checkpoint"] = {
                "phase": "waiting_approval",
                "attempt": attempt,
                "updated_at": finished,
            }
            next_status = "waiting_approval"
            with get_connection() as conn:
                # A very fast user decision may race the worker's transition
                # out of ``running``.  Preserve that approval by re-queueing
                # the job so the next worker pass resumes the loop.
                approved = conn.execute(
                    "SELECT id FROM runtime_approvals WHERE job_id = ? AND user_id = ? AND status = 'approved' LIMIT 1",
                    (job_id, user_id),
                ).fetchone()
                if approved is not None:
                    next_status = "queued"
                conn.execute(
                    "UPDATE runtime_jobs SET status = ?, result_json = ?, error = NULL, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND status = 'running'",
                    (next_status, _json(result), finished, job_id, user_id),
                )
            if next_status == "queued":
                return True
            try:
                append_activity(
                    "approval_requested",
                    "需要确认后才能继续",
                    str(result.get("reason", "Agent 工具调用需要审批"))[:1000],
                    job_id=job_id,
                    user_id=user_id,
                    notify=not _is_routine_job(job),
                )
            except Exception:
                pass
            _routine_job_finished(job, "waiting_approval", result=result, detail="需要确认后才能继续")
            return True
        result["checkpoint"] = {"phase": "completed", "attempt": attempt, "updated_at": finished}
        with get_connection() as conn:
            conn.execute(
                "UPDATE runtime_jobs SET status = 'succeeded', result_json = ?, error = NULL, updated_at = ? "
                "WHERE id = ? AND user_id = ? AND status = 'running'",
                (_json(result), finished, job_id, user_id),
            )
        if str(job.get("type") or "") == "sandbox_job":
            _sandbox_job_assistant_message(job, result, "succeeded")
        detail = "执行完成"
        try:
            if job["type"] == "briefing":
                detail = f"{len(result.get('pending_tasks', []))} 个待处理任务 · {len(result.get('recent_memories', []))} 条近期记忆"
            elif job["type"] == "create_task":
                detail = f"已创建任务：{result.get('title', '')}"
            elif job["type"] == "create_memory":
                detail = f"已保存 {result.get('category', 'general')} 记忆"
            elif job["type"] == "agent_run":
                detail = str(result.get("message", "Agent 运行完成"))[:500]
            append_activity("job_succeeded", f"已完成：{job['type']}", detail, job_id=job_id, user_id=user_id, notify=not _is_routine_job(job) and job["type"] not in {"proactive", "feed"})
        except Exception:
            pass
        _routine_job_finished(job, "succeeded", result=result, detail=detail)
        return True
    except Exception as exc:
        detail = _safe_exception_detail(exc)
        if str(job.get("type") or "") == "sandbox_job":
            _sandbox_job_assistant_message(
                job,
                {"exit_code": 1, "stderr_tail": detail},
                "failed",
            )
        try:
            max_attempts = _max_attempts(job.get("payload"))
        except ValueError:
            max_attempts = 3
        if _job_is_idempotent(job) and attempt < max_attempts:
            finished = now()
            retry_at = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            retry_at = retry_at.timestamp() + min(60, 2 ** max(0, attempt - 1))
            retry_iso = datetime.fromtimestamp(retry_at, timezone.utc).isoformat()
            with get_connection() as conn:
                conn.execute(
                    "UPDATE runtime_jobs SET status = 'queued', error = ?, result_json = ?, run_at = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND status = 'running'",
                    (
                        detail[:1000],
                        _json({"checkpoint": {"phase": "retrying", "attempt": attempt, "error": detail[:500], "updated_at": finished}}),
                        retry_iso,
                        finished,
                        job_id,
                        user_id,
                    ),
                )
            try:
                append_activity("job_retry_scheduled", f"稍后重试：{job['type']}", f"第 {attempt}/{max_attempts} 次尝试失败，已安排 {retry_iso} 重试。", job_id=job_id, user_id=user_id)
            except Exception:
                pass
            return True
        _mark_job_failed(job, detail, attempt)
        return True
    finally:
        heartbeat_stop.set()
        heartbeat.join()
        with _RUNTIME_HEARTBEAT_LOCK:
            _RUNTIME_HEARTBEAT_STOPS.discard(heartbeat_stop)


def run_due_jobs(limit: Optional[int] = None) -> int:
    """Claim and submit due jobs without waiting for long-running jobs.

    The executor is kept at module scope so the two-second worker poll can
    continue filling free slots while another ``agent_run`` is still active.
    A context-managed executor here would wait for that long job before the
    next poll and would recreate the serial bottleneck this queue is meant to
    remove.
    """

    global _RUNTIME_EXECUTOR, _RUNTIME_EXECUTOR_SIZE
    concurrency = _worker_concurrency()
    requested = concurrency if limit is None else min(max(int(limit), 1), concurrency)
    with _RUNTIME_EXECUTOR_LOCK:
        if _RUNTIME_SHUTTING_DOWN:
            return 0
        # Collect finished futures and surface their exceptions to avoid
        # retaining completed work forever.  Job state is persisted by the
        # worker itself, so an exception here is only a local diagnostic.
        finished = {future for future in _RUNTIME_FUTURES if future.done()}
        for future in finished:
            try:
                future.result()
            except Exception:
                pass
        _RUNTIME_FUTURES.difference_update(finished)

        if (
            _RUNTIME_EXECUTOR is not None
            and _RUNTIME_EXECUTOR_SIZE != concurrency
            and not _RUNTIME_FUTURES
        ):
            _RUNTIME_EXECUTOR.shutdown(wait=False)
            _RUNTIME_EXECUTOR = None
            _RUNTIME_EXECUTOR_SIZE = 0
        if _RUNTIME_EXECUTOR is None:
            _RUNTIME_EXECUTOR = ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix="runtime-job",
            )
            _RUNTIME_EXECUTOR_SIZE = concurrency
        available = max(0, _RUNTIME_EXECUTOR_SIZE - len(_RUNTIME_FUTURES))
        claim_limit = min(requested, available)
        if claim_limit <= 0:
            return 0
        jobs = _claim_due_jobs(claim_limit)
        for job in jobs:
            _RUNTIME_FUTURES.add(_RUNTIME_EXECUTOR.submit(_run_claimed_job, job))
        return len(jobs)


def reap_stale_jobs() -> int:
    """Recover jobs left running by a crashed worker or deployment."""

    try:
        stale_seconds = max(int(os.getenv("RUNTIME_JOB_STALE_SECONDS", "600")), 1)
    except (TypeError, ValueError):
        stale_seconds = 600
    cutoff = datetime.now(timezone.utc).timestamp() - stale_seconds
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM runtime_jobs WHERE status = 'running' AND updated_at <= ? ORDER BY updated_at",
            (cutoff_iso,),
        ).fetchall()
    recovered = 0
    for raw in rows:
        job = _job(raw)
        if not job:
            continue
        attempt = int(job.get("attempts") or 0)
        try:
            max_attempts = _max_attempts(job.get("payload"))
        except ValueError:
            max_attempts = 3
        user_id = job.get("user_id", "local")
        if _job_is_idempotent(job) and attempt < max_attempts:
            timestamp = now()
            with get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE runtime_jobs SET status = 'queued', run_at = ?, error = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND status = 'running'",
                    (timestamp, "执行中断（进程重启或超时），已重新排队", timestamp, job["id"], user_id),
                )
            if not getattr(cursor, "rowcount", 0):
                continue
            kind, title, detail = "job_requeued", "卡死任务已重新排队", "执行中断（进程重启或超时），已重新排队"
        else:
            timestamp = now()
            detail = "执行中断（进程重启或超时），请重新提交"
            with get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE runtime_jobs SET status = 'failed', error = ?, updated_at = ? "
                    "WHERE id = ? AND user_id = ? AND status = 'running'",
                    (detail, timestamp, job["id"], user_id),
                )
            if not getattr(cursor, "rowcount", 0):
                continue
            kind, title = "job_failed", "卡死任务已失败"
            if str(job.get("type") or "") == "sandbox_job":
                _sandbox_job_assistant_message(
                    job,
                    {"exit_code": 1, "stderr_tail": detail},
                    "failed",
                )
        try:
            append_activity(kind, title, detail, job_id=job["id"], user_id=user_id)
        except Exception:
            pass
        recovered += 1
    return recovered


def _expired_lease_count(value: Any) -> int:
    if isinstance(value, dict):
        for key in ("count", "reaped", "stopped", "expired"):
            if key in value:
                try:
                    return int(value[key])
                except (TypeError, ValueError):
                    return 0
        return 0
    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def run_maintenance() -> dict[str, int]:
    """Run stale-job and expired-lease recovery under one process lock."""

    def _run() -> dict[str, int]:
        stale = reap_stale_jobs()
        try:
            from .agent_runtime import reap_expired_leases
        except ImportError:
            expired = 0
        else:
            expired = _expired_lease_count(reap_expired_leases())
        from .services.secret_vault import cleanup_expired_secrets

        cleanup_expired_secrets()
        return {"stale_jobs": stale, "expired_leases": expired}

    # Keep the advisory lock connection open while both reapers run. The
    # reapers use short-lived connections for their own updates, but all
    # contenders first pass this transaction-scoped gate.
    with get_connection() as lock_conn:
        row = lock_conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(?)) AS locked",
            ("luma-runtime-maintenance",),
        ).fetchone()
        locked = bool(row and (row.get("locked") if isinstance(row, dict) else row[0]))
        if not locked:
            return {"stale_jobs": 0, "expired_leases": 0}
        return _run()


async def worker_loop(stop_event: Any, interval_seconds: float = 2.0) -> None:
    """Run the durable queue while the FastAPI process is alive."""
    import asyncio

    sweep_task = None

    def _file_sweep_done(task: Any) -> None:
        """Retrieve a detached sweep exception so asyncio does not warn."""
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            # File backends can be temporarily unavailable; leave the
            # soft-deleted row for the next maintenance pass.  Keep the
            # detached task from surfacing an unhandled exception while the
            # durable job worker continues to run.
            logger.warning("deleted-file sweep failed: %s", type(exc).__name__)

    # Run one maintenance pass as soon as the worker starts, then keep the
    # existing 60-second cadence.  ``monotonic()`` is process-relative and
    # can start near zero in tests or freshly booted runtimes.
    last_maintenance = -60.0
    try:
        while not stop_event.is_set():
            try:
                current = _runtime_monotonic()
                if current - last_maintenance >= 60.0:
                    await asyncio.to_thread(run_maintenance)
                    # Generation rows have a separate advisory-lock guarded
                    # reaper.  Keep it beside the existing runtime maintenance
                    # cadence so every worker gets a chance to recover a crashed
                    # streaming request without adding another background loop.
                    # Import lazily to avoid a runtime -> generation -> chat
                    # import cycle during application startup.
                    try:
                        from .services import generation

                        await generation.manager.recover_stale()
                    except Exception as exc:
                        # A storage/Redis outage must not stop the durable job
                        # worker.  recover_stale itself applies the Redis-aware
                        # age fallback and owns its advisory lock.
                        logger.warning("generation stale recovery failed: %s", type(exc).__name__)
                    if sweep_task is None or sweep_task.done():
                        from .services.files import sweep_deleted_files

                        # The storage call can spend up to a minute on a
                        # remote backend.  Keep it detached so run_due_jobs
                        # continues on its normal cadence and no DB connection
                        # is held by the worker while the object is deleted.
                        sweep_task = asyncio.create_task(asyncio.to_thread(sweep_deleted_files, 20))
                        sweep_task.add_done_callback(_file_sweep_done)
                    last_maintenance = current
                await asyncio.to_thread(run_due_jobs)
            except Exception as exc:  # keep the service alive if storage blips
                try:
                    append_activity("worker_error", "后台 worker 暂时不可用", _safe_exception_detail(exc))
                except Exception:
                    # A storage outage must not kill the worker task itself.
                    pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        # A cancelled to_thread task would leave the storage/SQL thread alive
        # after teardown. Drain its idempotent delete before returning.
        if sweep_task is not None:
            await asyncio.gather(asyncio.shield(sweep_task), return_exceptions=True)
