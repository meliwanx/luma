"""Role-protected operator API with audited access to account data."""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
import time
import uuid
from urllib.parse import urlparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import telemetry
from .agent_runtime import _hash_user
from .agent_runtime import status as agent_runtime_status
from .auth import current_user, current_user_id, revoke_user_sessions
from .db import get_connection, ping_database, ping_redis
from .usage import admin_models

router = APIRouter(prefix="/api/admin", tags=["admin"])

LIVE_SECONDS = 30
SERVICE_NAME = os.getenv("LUMA_SERVICE_NAME", "luma-assistant")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**delta: float) -> str:
    return (_now() - timedelta(**delta)).isoformat()


def _rows(conn: Any, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _scalar(conn: Any, sql: str, params: Any = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    value = list(dict(row).values())[0]
    return int(value or 0)


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _like() -> str:
    return "ILIKE"


def _admin_ids() -> set[str]:
    return {item.strip() for item in os.getenv("ADMIN_USER_IDS", "").split(",") if item.strip()}


def _identity(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user:
        return user
    raise HTTPException(status_code=401, detail="未登录")


def _is_admin(user: dict[str, Any]) -> bool:
    return user.get("role") == "admin" or str(user.get("user_id", "")) in _admin_ids()


def require_admin(request: Request) -> dict[str, Any]:
    user = _identity(request)
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    route = request.scope.get("route")
    target = dict(request.path_params)
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO admin_audit(id,admin_user_id,action,target,ip,created_at) VALUES (?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    str(user["user_id"]),
                    f"{request.method} {getattr(route, 'path', request.url.path)}",
                    json.dumps(target, ensure_ascii=False)[:500],
                    telemetry.client_ip(request),
                    _now().isoformat(),
                ),
            )
    except Exception:
        telemetry.logger.exception("admin audit write failed")
    return user


def _people(conn: Any) -> dict[str, dict[str, Any]]:
    """Every known owner: profiled users plus owners seen only in sessions."""

    people = {
        row["user_id"]: row
        for row in _rows(
            conn,
            "SELECT user_id, username, display_name, email, role, status, created_at, "
            "first_seen_at, last_seen_at, last_login_at, login_count FROM users",
        )
    }
    for row in _rows(conn, "SELECT DISTINCT user_id FROM sessions"):
        people.setdefault(row["user_id"], {"user_id": row["user_id"], "username": None, "display_name": None, "email": None, "role": "user", "status": "disabled"})
    return people


def _label(person: Optional[dict[str, Any]], user_id: Optional[str]) -> dict[str, Any]:
    person = person or {}
    return {
        "user_id": user_id,
        "username": person.get("username"),
        "display_name": person.get("display_name"),
    }


@router.get("/me")
def admin_me(request: Request) -> dict[str, Any]:
    """Tell any signed-in user whether they are an admin and who they are."""

    user = _identity(request)
    return {
        "is_admin": _is_admin(user),
        "user_id": user.get("user_id"),
        "username": user.get("username"),
        "display_name": user.get("display_name"),
        "role": user.get("role"),
    }


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metas = [_json(row.get("metadata_json")) for row in rows]
    first = sorted(m["first_token_ms"] for m in metas if isinstance(m.get("first_token_ms"), int))
    total = sorted(m["duration_ms"] for m in metas if isinstance(m.get("duration_ms"), int))
    failed = sum(
        1
        for row, metadata in zip(rows, metas)
        if metadata.get("incomplete")
        or metadata.get("provider") == "local"
        or row.get("status") in {"incomplete", "error"}
    )

    def pct(values: list[int], q: float) -> Optional[int]:
        return values[min(len(values) - 1, int(len(values) * q))] if values else None

    return {
        "replies": len(metas),
        "failed": failed,
        "first_token_avg_ms": int(sum(first) / len(first)) if first else None,
        "first_token_p95_ms": pct(first, 0.95),
        "duration_avg_ms": int(sum(total) / len(total)) if total else None,
        "duration_p95_ms": pct(total, 0.95),
        "models": dict(Counter(str(m.get("model") or m.get("provider") or "unknown") for m in metas)),
    }


@router.get("/overview")
def overview(_: dict = Depends(require_admin)) -> dict[str, Any]:
    day, week = _ago(days=1), _ago(days=7)
    current = _now()
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_connection() as conn:
        people = _people(conn)
        active_ids = {r["user_id"] for r in _rows(conn, "SELECT DISTINCT user_id FROM messages WHERE created_at >= ? AND role = 'user'", (day,))}
        active_week = {r["user_id"] for r in _rows(conn, "SELECT DISTINCT user_id FROM messages WHERE created_at >= ? AND role = 'user'", (week,))}
        seen_day = {r["user_id"] for r in _rows(conn, "SELECT user_id FROM users WHERE last_seen_at >= ?", (day,))}
        reply_rows = _rows(
            conn,
            "SELECT metadata_json, status FROM messages WHERE role = 'assistant' AND created_at >= ? ORDER BY created_at DESC LIMIT 5000",
            (day,),
        )
        message_series = _rows(
            conn,
            "SELECT substr(created_at, 1, 13) AS hour, role, COUNT(*) AS count FROM messages "
            "WHERE created_at >= ? GROUP BY substr(created_at, 1, 13), role",
            (day,),
        )
        request_series = _rows(
            conn,
            "SELECT hour, SUM(count) AS count, SUM(error_count) AS errors FROM api_stats_hourly WHERE hour >= ? GROUP BY hour",
            (_ago(days=1)[:13] + ":00",),
        )
        jobs = {r["status"]: int(r["count"]) for r in _rows(conn, "SELECT status, COUNT(*) AS count FROM runtime_jobs GROUP BY status")}
        clients = _rows(
            conn,
            "SELECT client, COUNT(DISTINCT user_id) AS users, SUM(request_count) AS requests FROM client_devices "
            "WHERE last_seen_at >= ? GROUP BY client",
            (week,),
        )
        data = {
            "counts": {
                "model_calls_today": _scalar(conn, "SELECT COUNT(*) AS c FROM model_calls WHERE created_at >= ? AND created_at <= ?", (today, current)),
                "tokens_today": _scalar(conn, "SELECT COALESCE(SUM(total_tokens), 0) AS c FROM model_calls WHERE created_at >= ? AND created_at <= ?", (today, current)),
            },
            "users": {
                "total": len(people),
                "active_24h": len(active_ids | seen_day),
                "chatting_24h": len(active_ids),
                "chatting_7d": len(active_week),
                "new_24h": _scalar(conn, "SELECT COUNT(*) AS c FROM users WHERE first_seen_at >= ?", (day,)),
            },
            "conversations": {
                "sessions": _scalar(conn, "SELECT COUNT(*) AS c FROM sessions"),
                "messages": _scalar(conn, "SELECT COUNT(*) AS c FROM messages"),
                "sessions_24h": _scalar(conn, "SELECT COUNT(*) AS c FROM sessions WHERE created_at >= ?", (day,)),
                "user_messages_24h": _scalar(conn, "SELECT COUNT(*) AS c FROM messages WHERE role = 'user' AND created_at >= ?", (day,)),
                "files": _scalar(conn, "SELECT COUNT(*) AS c FROM files WHERE deleted_at IS NULL"),
                "memories": _scalar(conn, "SELECT COUNT(*) AS c FROM memories"),
                "tasks": _scalar(conn, "SELECT COUNT(*) AS c FROM tasks"),
            },
            "quality_24h": _metric_summary(reply_rows),
            "runtime": {
                "jobs": jobs,
                "pending_approvals": _scalar(conn, "SELECT COUNT(*) AS c FROM runtime_approvals WHERE status = 'pending'"),
                "sandboxes_active": _scalar(conn, "SELECT COUNT(*) AS c FROM runtime_leases WHERE status IN ('running','ready','starting')"),
                "sandboxes_max": agent_runtime_status().get("max_instances"),
                "workers_live": _scalar(conn, "SELECT COUNT(*) AS c FROM worker_heartbeats WHERE last_seen_at >= ?", (_ago(seconds=LIVE_SECONDS),)),
            },
            "clients_7d": clients,
            "series": {"messages": message_series, "requests": request_series},
        }
    data["health"] = {"database": ping_database(), "redis": ping_redis(), "storage": "postgres"}
    data["generated_at"] = _now().isoformat()
    return data


@router.get("/models")
def models(
    range: str = Query(default="7d", pattern="^(24h|7d|30d)$"),
    model: Optional[str] = Query(default=None, max_length=200),
    user_id: Optional[str] = Query(default=None, max_length=200),
    _: dict = Depends(require_admin),
) -> dict[str, Any]:
    with get_connection() as conn:
        return admin_models(conn, range, model, user_id)


def _read_proc(pid: int) -> Optional[dict[str, Any]]:
    base = Path(f"/proc/{pid}")
    try:
        status = dict(
            line.split(":", 1) for line in (base / "status").read_text().splitlines() if ":" in line
        )
        cmdline = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        stat = (base / "stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        boot = time.time() - float(Path("/proc/uptime").read_text().split()[0])
        return {
            "pid": pid,
            "rss_mb": round(int(status.get("VmRSS", "0 kB").split()[0]) / 1024, 1),
            "threads": int(status.get("Threads", "0").strip()),
            "cpu_seconds": round((int(stat[11]) + int(stat[12])) / ticks, 2),
            "started_at": datetime.fromtimestamp(boot + int(stat[19]) / ticks, timezone.utc).isoformat(),
            "cmdline": cmdline[:300],
        }
    except (OSError, ValueError, IndexError):
        return None


def _host() -> dict[str, Any]:
    info: dict[str, Any] = {"hostname": telemetry.HOST, "cpu_count": os.cpu_count(), "release": telemetry.RELEASE}
    try:
        info["loadavg"] = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        pass
    try:
        mem = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        info["memory_mb"] = {"total": mem["MemTotal"] // 1024, "available": mem["MemAvailable"] // 1024}
        info["uptime_seconds"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, KeyError, ValueError):
        pass
    usage = shutil.disk_usage("/")
    info["disk_gb"] = {"total": round(usage.total / 1e9, 1), "free": round(usage.free / 1e9, 1)}
    return info


def _service() -> Optional[dict[str, str]]:
    try:
        output = subprocess.run(
            ["systemctl", "show", SERVICE_NAME, "-p", "ActiveState,SubState,MainPID,NRestarts,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return values or None


@router.get("/instances")
def instances(_: dict = Depends(require_admin)) -> dict[str, Any]:
    with get_connection() as conn:
        workers = _rows(conn, "SELECT * FROM worker_heartbeats WHERE last_seen_at >= ? ORDER BY host, pid", (_ago(minutes=10),))
        leases = _rows(conn, "SELECT * FROM runtime_leases ORDER BY COALESCE(last_seen_at, started_at) DESC LIMIT 200")
        people = _people(conn)
    live_after = _ago(seconds=LIVE_SECONDS)
    for worker in workers:
        worker["live"] = worker["last_seen_at"] >= live_after
    masters = {}
    for ppid in {w["ppid"] for w in workers if w["live"] and w["host"] == telemetry.HOST and w.get("ppid")}:
        proc = _read_proc(int(ppid))
        if proc:
            masters[str(ppid)] = proc
    by_hash = {_hash_user(user_id): person for user_id, person in people.items()}
    sandboxes = []
    for lease in leases:
        person = by_hash.get(lease["user_id_hash"])
        sandboxes.append({
            **{k: v for k, v in lease.items() if k not in {"capabilities_json", "metadata_json", "user_id_hash"}},
            "capabilities": _json(lease.get("capabilities_json")) or [],
            "user": _label(person, person.get("user_id") if person else None),
            "active": lease.get("status") in {"running", "ready", "starting"},
        })
    return {
        "host": _host(),
        "service": _service(),
        "masters": list(masters.values()),
        "workers": workers,
        "sandboxes": sandboxes,
        "agent_runtime": agent_runtime_status(),
        "this_worker": {"pid": os.getpid(), **telemetry.counters()},
        "generated_at": _now().isoformat(),
    }


def _group_counts(conn: Any, table: str, extra: str = "") -> dict[str, dict[str, Any]]:
    return {
        row["user_id"]: row
        for row in _rows(conn, f"SELECT user_id, COUNT(*) AS count{extra} FROM {table} GROUP BY user_id")
    }


@router.get("/users")
def users(
    q: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: dict = Depends(require_admin),
) -> dict[str, Any]:
    with get_connection() as conn:
        people = _people(conn)
        sessions = _group_counts(conn, "sessions")
        messages = _group_counts(conn, "messages", ", MAX(created_at) AS last_message_at")
        memories = _group_counts(conn, "memories")
        tasks = _group_counts(conn, "tasks")
        devices = _rows(conn, "SELECT user_id, client, platform, client_version, last_seen_at FROM client_devices ORDER BY last_seen_at DESC")
    clients: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        clients.setdefault(device["user_id"], []).append(device)
    items = []
    for user_id, person in people.items():
        item = {
            **person,
            "sessions": int(sessions.get(user_id, {}).get("count", 0)),
            "messages": int(messages.get(user_id, {}).get("count", 0)),
            "memories": int(memories.get(user_id, {}).get("count", 0)),
            "tasks": int(tasks.get(user_id, {}).get("count", 0)),
            "last_message_at": messages.get(user_id, {}).get("last_message_at"),
            "clients": sorted({d["client"] for d in clients.get(user_id, [])}),
        }
        item["last_active_at"] = max(filter(None, [item.get("last_seen_at"), item["last_message_at"]]), default=None)
        items.append(item)
    if q:
        needle = q.strip().lower()
        items = [
            item for item in items
            if any(needle in str(item.get(key) or "").lower() for key in ("user_id", "username", "display_name", "email"))
        ]
    items.sort(key=lambda item: item.get("last_active_at") or "", reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit]}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, request: Request, actor: dict = Depends(require_admin)) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请使用 JSON 请求")
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="无效的账号设置") from None
    if not isinstance(payload, dict) or not payload or set(payload) - {"role", "status"}:
        raise HTTPException(status_code=422, detail="无效的账号设置")
    role, account_status = payload.get("role"), payload.get("status")
    if ("role" in payload and role not in ("admin", "user")) or (
        "status" in payload and account_status not in ("active", "disabled")
    ):
        raise HTTPException(status_code=422, detail="无效的账号设置")
    if user_id == str(actor["user_id"]) and (role == "user" or account_status == "disabled"):
        raise HTTPException(status_code=400, detail="管理员不能降级或禁用自己的账户")
    with get_connection() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ? FOR UPDATE", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        conn.execute(
            "UPDATE users SET role = COALESCE(?, role), status = COALESCE(?, status), "
            "session_version = session_version + ? WHERE user_id = ?",
            (role, account_status, 1 if account_status == "disabled" else 0, user_id),
        )
        result = conn.execute(
            "SELECT user_id,username,email,display_name,role,status FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    # The DB status/version is already authoritative if Redis is unavailable.
    if account_status == "disabled":
        revoke_user_sessions(user_id)
    return dict(result)


@router.get("/users/{user_id}")
def user_detail(user_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
    with get_connection() as conn:
        people = _people(conn)
        if user_id not in people:
            raise HTTPException(status_code=404, detail="用户不存在")
        sessions = _rows(
            conn,
            "SELECT s.id, s.title, s.created_at, s.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
            "FROM sessions s WHERE s.user_id = ? ORDER BY s.updated_at DESC LIMIT 100",
            (user_id,),
        )
        data = {
            "profile": people[user_id],
            "devices": _rows(conn, "SELECT * FROM client_devices WHERE user_id = ? ORDER BY last_seen_at DESC", (user_id,)),
            "sessions": sessions,
            "memories": _rows(conn, "SELECT id, category, content, importance, created_at FROM memories WHERE user_id = ? ORDER BY created_at DESC LIMIT 200", (user_id,)),
            "tasks": _rows(conn, "SELECT id, title, status, due_at, updated_at FROM tasks WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200", (user_id,)),
            "goals": _rows(conn, "SELECT id, title, status, progress, updated_at FROM goals WHERE user_id = ? ORDER BY updated_at DESC LIMIT 100", (user_id,)),
            "files": _rows(conn, "SELECT id, filename, media_type, size_bytes, session_id, created_at FROM files WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100", (user_id,)),
            "connectors": [],
            "jobs": {r["status"]: int(r["count"]) for r in _rows(conn, "SELECT status, COUNT(*) AS count FROM runtime_jobs WHERE user_id = ? GROUP BY status", (user_id,))},
            "errors": _rows(conn, "SELECT method, route, status, duration_ms, client, ip, error, created_at FROM request_log WHERE user_id = ? AND status >= 400 ORDER BY created_at DESC LIMIT 50", (user_id,)),
            "replies_7d": _metric_summary(_rows(conn, "SELECT metadata_json, status FROM messages WHERE user_id = ? AND role = 'assistant' AND created_at >= ?", (user_id, _ago(days=7)))),
        }
        for connector in _rows(conn, "SELECT id,name,kind,endpoint,enabled,metadata_json,updated_at FROM connectors WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)):
            metadata = _json(connector.pop("metadata_json", "{}"))
            connector["endpoint_host"] = urlparse(connector.pop("endpoint", "") or "").hostname or ""
            connector["tool_count"] = len(metadata.get("tools", [])) if isinstance(metadata, dict) and isinstance(metadata.get("tools"), list) else 0
            connector["status"] = metadata.get("status") if isinstance(metadata, dict) else None
            connector["last_error"] = metadata.get("last_error") if isinstance(metadata, dict) else None
            data["connectors"].append(connector)
        lease = conn.execute("SELECT * FROM runtime_leases WHERE user_id_hash = ?", (_hash_user(user_id),)).fetchone()
    data["sandbox"] = dict(lease) if lease else None
    if data["sandbox"]:
        data["sandbox"].pop("metadata_json", None)
    return data


@router.get("/sessions")
def sessions(
    user_id: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, alias="from"),
    date_to: Optional[str] = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: dict = Depends(require_admin),
) -> dict[str, Any]:
    where, params = [], []
    if user_id:
        where.append("s.user_id = ?")
        params.append(user_id)
    if q:
        like = f"%{q.strip()}%"
        where.append(f"(s.title {_like()} ? OR s.id IN (SELECT session_id FROM messages WHERE content {_like()} ?))")
        params += [like, like]
    if date_from:
        where.append("s.updated_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("s.updated_at < ?")
        params.append(date_to)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with get_connection() as conn:
        total = _scalar(conn, f"SELECT COUNT(*) AS c FROM sessions s {clause}", tuple(params))
        rows = _rows(
            conn,
            "SELECT s.id, s.user_id, s.title, s.created_at, s.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count, "
            "(SELECT m.content FROM messages m WHERE m.session_id = s.id AND m.role = 'user' ORDER BY m.created_at DESC LIMIT 1) AS last_user_message "
            f"FROM sessions s {clause} ORDER BY s.updated_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        people = _people(conn)
    for row in rows:
        row["user"] = _label(people.get(row["user_id"]), row["user_id"])
        if row.get("last_user_message"):
            row["last_user_message"] = row["last_user_message"][:160]
    return {"total": total, "items": rows}


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
    with get_connection() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        session = dict(session)
        messages = _rows(
            conn,
            "SELECT id, role, content, created_at, metadata_json FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        files = _rows(conn, "SELECT id, filename, media_type, size_bytes, created_at FROM files WHERE session_id = ? AND deleted_at IS NULL", (session_id,))
        people = _people(conn)
    for message in messages:
        message["metadata"] = _json(message.pop("metadata_json"))
    return {"session": session, "user": _label(people.get(session["user_id"]), session["user_id"]), "messages": messages, "files": files}


@router.get("/jobs")
def jobs(status: Optional[str] = None, limit: int = Query(default=100, ge=1, le=500), _: dict = Depends(require_admin)) -> dict[str, Any]:
    with get_connection() as conn:
        if status:
            job_rows = _rows(conn, "SELECT * FROM runtime_jobs WHERE status = ? ORDER BY updated_at DESC LIMIT ?", (status, limit))
        else:
            job_rows = _rows(conn, "SELECT * FROM runtime_jobs ORDER BY updated_at DESC LIMIT ?", (limit,))
        approvals = _rows(conn, "SELECT * FROM runtime_approvals ORDER BY created_at DESC LIMIT 100")
        counts = {r["status"]: int(r["count"]) for r in _rows(conn, "SELECT status, COUNT(*) AS count FROM runtime_jobs GROUP BY status")}
        people = _people(conn)
    for row in job_rows:
        row["payload"] = _json(row.pop("payload_json", None))
        row["result"] = _json(row.pop("result_json", None))
        row["user"] = _label(people.get(row["user_id"]), row["user_id"])
    for row in approvals:
        row["payload"] = _json(row.pop("payload_json", None))
        row["user"] = _label(people.get(row["user_id"]), row["user_id"])
    return {"counts": counts, "jobs": job_rows, "approvals": approvals}


@router.get("/requests")
def requests_log(kind: str = Query(default="errors", pattern="^(errors|slow|all)$"), limit: int = Query(default=200, ge=1, le=1000), _: dict = Depends(require_admin)) -> dict[str, Any]:
    condition = {"errors": "WHERE status >= 400", "slow": f"WHERE duration_ms >= {telemetry.SLOW_MS}", "all": ""}[kind]
    with get_connection() as conn:
        rows = _rows(conn, f"SELECT * FROM request_log {condition} ORDER BY created_at DESC LIMIT ?", (limit,))
        people = _people(conn)
    for row in rows:
        row["user"] = _label(people.get(row["user_id"]), row["user_id"]) if row.get("user_id") else None
    return {"items": rows}


def _activity_cursor(value: str) -> tuple[str, str]:
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        marker = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(marker, list)
            or len(marker) != 2
            or any(not isinstance(item, str) or not item for item in marker)
            or len(marker[0]) > 64
            or len(marker[1]) > 200
        ):
            raise ValueError("invalid activity cursor")
        datetime.fromisoformat(marker[0].replace("Z", "+00:00"))
        return marker[0], marker[1]
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="无效的 cursor") from exc


@router.get("/activity")
def activity(
    user_id: Optional[str] = Query(default=None, max_length=200),
    kind: Optional[str] = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: Optional[str] = Query(default=None, max_length=1024),
    _: dict = Depends(require_admin),
) -> dict[str, Any]:
    where, params = [], []
    if user_id:
        where.append("a.user_id = ?")
        params.append(user_id)
    if kind:
        where.append("a.kind = ?")
        params.append(kind)
    if cursor:
        created_at, activity_id = _activity_cursor(cursor)
        where.append("(a.created_at < ? OR (a.created_at = ? AND a.id < ?))")
        params.extend((created_at, created_at, activity_id))
    clause = "WHERE " + " AND ".join(where) if where else ""
    with get_connection() as conn:
        rows = _rows(
            conn,
            "SELECT a.*, u.username, u.display_name "
            "FROM runtime_activity a LEFT JOIN users u ON u.user_id = a.user_id "
            + clause + " ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
            (*params, limit + 1),
        )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        marker = json.dumps([rows[-1]["created_at"], rows[-1]["id"]], separators=(",", ":"))
        next_cursor = base64.urlsafe_b64encode(marker.encode("utf-8")).decode("ascii").rstrip("=")
    for row in rows:
        row["user"] = _label(row, row["user_id"])
    return {"items": rows, "next_cursor": next_cursor}


@router.get("/routes")
def routes(hours: int = Query(default=24, ge=1, le=24 * 30), _: dict = Depends(require_admin)) -> dict[str, Any]:
    with get_connection() as conn:
        rows = _rows(
            conn,
            "SELECT method, route, SUM(count) AS count, SUM(error_count) AS errors, SUM(total_ms) AS total_ms, MAX(max_ms) AS max_ms "
            "FROM api_stats_hourly WHERE hour >= ? GROUP BY method, route",
            (_ago(hours=hours)[:13] + ":00",),
        )
    for row in rows:
        count = int(row["count"] or 0)
        row["avg_ms"] = int(int(row.pop("total_ms") or 0) / count) if count else 0
        row["error_rate"] = round(int(row["errors"] or 0) / count, 4) if count else 0
    rows.sort(key=lambda row: int(row["count"] or 0), reverse=True)
    return {"hours": hours, "items": rows}


@router.get("/audit")
def audit(limit: int = Query(default=200, ge=1, le=1000), _: dict = Depends(require_admin)) -> dict[str, Any]:
    with get_connection() as conn:
        rows = _rows(conn, "SELECT * FROM admin_audit ORDER BY created_at DESC LIMIT ?", (limit,))
        people = _people(conn)
    for row in rows:
        row["user"] = _label(people.get(row["admin_user_id"]), row["admin_user_id"])
    return {"items": rows}
