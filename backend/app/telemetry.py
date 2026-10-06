"""Operator telemetry for the admin console.

Request handling only touches in-memory buffers; a per-worker background loop
flushes them to the database every few seconds together with a heartbeat row
for this process.  Request bodies and query strings are never recorded: the
log keeps the route template, status, timing, client and owner only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from .db import get_connection, lock_account_data
from .model_calls import merged_metadata_sql

logger = logging.getLogger("luma.telemetry")

HOST = socket.gethostname()
# The release directory name (e.g. 20261003105902-muse-views) identifies the
# deployed build; local checkouts report their folder name.
RELEASE = Path(__file__).resolve().parents[2].name
STARTED_AT = datetime.now(timezone.utc).isoformat()

FLUSH_SECONDS = 5
PRUNE_SECONDS = 3600
USER_SEEN_SECONDS = 60
SLOW_MS = 1000
REQUEST_LOG_DAYS = 30
STATS_DAYS = 90
MODEL_CALL_DAYS = 180

CLIENTS = {"web", "desktop", "ios", "android", "macos", "windows", "linux", "flutter"}
_ID_SEGMENT = re.compile(r"^(?:[a-z]+_[A-Za-z0-9_-]{6,}|[0-9a-fA-F-]{16,}|\d+)$")

_lock = threading.Lock()
_counters = {"in_flight": 0, "active_streams": 0, "requests_total": 0, "errors_total": 0}
_requests: list[tuple[Any, ...]] = []
_stats: dict[tuple[str, str, str], list[int]] = {}
_devices: dict[str, dict[str, Any]] = {}
_users: dict[str, dict[str, Any]] = {}
_user_seen: dict[str, float] = {}
_model_calls: list[dict[str, Any]] = []
_MODEL_CALL_COLUMNS = (
    "id", "user_id", "session_id", "message_id", "purpose", "model", "stream",
    "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "estimated",
    "first_token_ms", "duration_ms", "tokens_per_sec", "status", "error_type", "tool_calls_count", "created_at",
)


def record_model_call(row: dict[str, Any]) -> None:
    # Explicit allow-list keeps accidental content out of the buffer and SQL.
    item = {key: row.get(key) for key in _MODEL_CALL_COLUMNS}
    with _lock:
        if len(_model_calls) < 10000:
            _model_calls.append(item)


def _flush_model_calls(conn: Any, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            "INSERT INTO model_calls(" + ",".join(_MODEL_CALL_COLUMNS) + ") VALUES (" +
            ",".join("?" for _ in _MODEL_CALL_COLUMNS) + ") ON CONFLICT (id) DO NOTHING",
            tuple(row[key] for key in _MODEL_CALL_COLUMNS),
        )
    # Detached summary/extraction calls can finish after the assistant does.
    # Merge their totals atomically without reading or rewriting reply content.
    messages = {(row["user_id"], row["message_id"]) for row in rows if row["message_id"]}
    for user_id, message_id in sorted(messages):
        # Workers may insert new calls for the same resumed reply at once.
        # Lock before SUM so the second worker sees the first worker's commit.
        message = conn.execute(
            "SELECT id FROM messages WHERE id = ? AND user_id = ? FOR UPDATE",
            (message_id, user_id),
        ).fetchone()
        if message is None:
            continue
        row = conn.execute(
            "SELECT COUNT(*) AS calls, SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
            "SUM(total_tokens) AS total_tokens, SUM(cached_tokens) AS cached_tokens, SUM(reasoning_tokens) AS reasoning_tokens, "
            "COUNT(*) FILTER (WHERE estimated) AS estimated_calls FROM model_calls WHERE user_id = ? AND message_id = ?",
            (user_id, message_id),
        ).fetchone()
        usage = {key: int(value or 0) for key, value in dict(row).items()}
        usage["estimated"] = usage["estimated_calls"] > 0
        conn.execute(
            "UPDATE messages SET metadata_json = " + merged_metadata_sql(new="messages.metadata_json::jsonb || incoming.metadata") +
            " FROM (SELECT ?::jsonb AS metadata) incoming WHERE id = ? AND user_id = ? AND role = 'assistant'",
            (json.dumps({"usage": usage}), message_id, user_id),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: Any, size: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:size] if text else None


def client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return _clip(forwarded.split(",")[0], 64)
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return _clip(real_ip, 64)
    return request.client.host if request.client else None


def _platform_from_ua(ua: str) -> Optional[str]:
    for marker, name in (
        ("iPhone", "ios"),
        ("iPad", "ios"),
        ("Android", "android"),
        ("Mac OS X", "macos"),
        ("Macintosh", "macos"),
        ("Windows", "windows"),
        ("Linux", "linux"),
    ):
        if marker in ua:
            return name
    return None


def classify_client(request: Request) -> dict[str, Optional[str]]:
    """Identify the client from explicit headers, falling back to the UA."""

    ua = request.headers.get("user-agent", "")
    client = (request.headers.get("x-luma-client") or "").strip().lower()
    if client not in CLIENTS:
        if "Electron/" in ua:
            client = "desktop"
        elif ua.startswith("Dart/"):
            client = "flutter"
        elif "Mozilla/" in ua:
            client = "web"
        else:
            client = "unknown"
    return {
        "client": client,
        "client_version": _clip(request.headers.get("x-luma-client-version"), 40),
        "platform": _clip(request.headers.get("x-luma-platform"), 120) or _platform_from_ua(ua),
        "user_agent": _clip(ua, 300),
        "ip": client_ip(request),
    }


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    parts = ["{id}" if _ID_SEGMENT.match(part) else part for part in request.url.path.split("/")]
    return "/".join(parts)[:200]


def request_started() -> None:
    with _lock:
        _counters["in_flight"] += 1


def request_finished(request: Request, status: int, duration_ms: float, error: Optional[str] = None) -> None:
    user = getattr(request.state, "luma_user", None)
    user_id = str(user.get("user_id")) if isinstance(user, dict) and user.get("user_id") is not None else None
    if user_id is None:
        user_id = getattr(request.state, "luma_user_id", None)
    info = classify_client(request)
    method = request.method
    route = route_template(request)
    duration = int(duration_ms)
    failed = status >= 500 or error is not None
    created_at = _now()
    hour = created_at[:13] + ":00"
    with _lock:
        _counters["in_flight"] = max(0, _counters["in_flight"] - 1)
        _counters["requests_total"] += 1
        if failed:
            _counters["errors_total"] += 1
        stat = _stats.setdefault((hour, method, route), [0, 0, 0, 0])
        stat[0] += 1
        stat[1] += 1 if status >= 400 else 0
        stat[2] += duration
        stat[3] = max(stat[3], duration)
        if method != "GET" or status >= 400 or duration >= SLOW_MS:
            _requests.append(
                (uuid.uuid4().hex, user_id, method, route, status, duration, info["client"], info["ip"], _clip(error, 200), created_at)
            )
        if user_id:
            key = hashlib.sha256(
                f"{user_id}|{info['client']}|{info['platform']}|{info['user_agent']}".encode("utf-8")
            ).hexdigest()[:32]
            device = _devices.get(key)
            if device is None:
                device = _devices[key] = {**info, "id": key, "user_id": user_id, "count": 0}
            device.update({"client_version": info["client_version"] or device.get("client_version"), "ip": info["ip"]})
            device["count"] += 1
            device["seen"] = created_at
            if isinstance(user, dict) and time.monotonic() - _user_seen.get(user_id, 0) >= USER_SEEN_SECONDS:
                _user_seen[user_id] = time.monotonic()
                _users[user_id] = {"user": user, "seen": created_at}


def stream_started() -> None:
    with _lock:
        _counters["active_streams"] += 1


def stream_finished() -> None:
    with _lock:
        _counters["active_streams"] = max(0, _counters["active_streams"] - 1)


def counters() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def _upsert_user(conn: Any, user: dict[str, Any], seen_at: str, *, login: bool) -> None:
    """Update activity only; buffered telemetry must never recreate accounts."""
    if login:
        conn.execute(
            "UPDATE users SET last_seen_at = ?, last_login_at = ?, login_count = login_count + 1 WHERE user_id = ?",
            (seen_at, seen_at, str(user["user_id"])),
        )
    else:
        conn.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ?", (seen_at, str(user["user_id"])))


def upsert_user_profile(user: dict[str, Any], *, login: bool = False) -> None:
    """Record account activity without copying identity or credential fields."""
    if not isinstance(user, dict) or user.get("user_id") is None:
        return
    try:
        with get_connection() as conn:
            _upsert_user(conn, user, _now(), login=login)
    except Exception:
        logger.error("failed to record account activity")


def _proc_status() -> dict[str, float]:
    result = {"rss_mb": 0.0, "threads": float(threading.active_count())}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                result["rss_mb"] = int(line.split()[1]) / 1024
            elif line.startswith("Threads:"):
                result["threads"] = float(line.split()[1])
    except OSError:
        try:
            import resource

            # ru_maxrss is bytes on macOS and kilobytes on Linux.
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            result["rss_mb"] = peak / (1024 * 1024) if os.uname().sysname == "Darwin" else peak / 1024
        except Exception:
            pass
    return result


def flush() -> None:
    """Persist buffered telemetry and this worker's heartbeat."""

    with _lock:
        requests, stats, devices, users = list(_requests), dict(_stats), dict(_devices), dict(_users)
        _requests.clear()
        _stats.clear()
        _devices.clear()
        _users.clear()
        snapshot = dict(_counters)
        model_calls = list(_model_calls)
        _model_calls.clear()
    proc = _proc_status()
    seen_at = _now()
    try:
        with get_connection() as conn:
            owners = {str(row[1]) for row in requests if row[1] is not None}
            owners.update(str(item["user_id"]) for item in devices.values())
            owners.update(str(row["user_id"]) for row in model_calls if row.get("user_id") is not None)
            owners.update(str(user_id) for user_id in users)
            # Acquire in stable order before message/device locks. Deletion
            # and background telemetry share this boundary across workers.
            for user_id in sorted(owners):
                lock_account_data(conn, user_id)
            erased = set()
            if owners:
                digests = [hashlib.sha256(owner.encode("utf-8")).hexdigest() for owner in sorted(owners)]
                erased_hashes = {row["user_id_hash"] for row in conn.execute(
                    "SELECT user_id_hash FROM deleted_account_ids WHERE user_id_hash = ANY(?)", (digests,)
                ).fetchall()}
                erased = {owner for owner in owners if hashlib.sha256(owner.encode("utf-8")).hexdigest() in erased_hashes}
            requests = [row for row in requests if row[1] is None or str(row[1]) not in erased]
            devices = {key: item for key, item in devices.items() if str(item["user_id"]) not in erased}
            users = {key: item for key, item in users.items() if str(key) not in erased}
            model_calls = [row for row in model_calls if str(row.get("user_id")) not in erased]
            for row in requests:
                conn.execute(
                    "INSERT INTO request_log(id,user_id,method,route,status,duration_ms,client,ip,error,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
            for (hour, method, route), (count, errors, total, peak) in stats.items():
                conn.execute(
                    "INSERT INTO api_stats_hourly(hour,method,route,count,error_count,total_ms,max_ms) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT (hour, method, route) DO UPDATE SET "
                    "count = api_stats_hourly.count + excluded.count, "
                    "error_count = api_stats_hourly.error_count + excluded.error_count, "
                    "total_ms = api_stats_hourly.total_ms + excluded.total_ms, "
                    "max_ms = CASE WHEN excluded.max_ms > api_stats_hourly.max_ms THEN excluded.max_ms ELSE api_stats_hourly.max_ms END",
                    (hour, method, route, count, errors, total, peak),
                )
            for item in users.values():
                _upsert_user(conn, item["user"], item["seen"], login=False)
            for device in devices.values():
                conn.execute(
                    "INSERT INTO client_devices(id,user_id,client,client_version,platform,user_agent,last_ip,first_seen_at,last_seen_at,request_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET "
                    "client_version = COALESCE(excluded.client_version, client_devices.client_version), "
                    "last_ip = excluded.last_ip, last_seen_at = excluded.last_seen_at, "
                    "request_count = client_devices.request_count + excluded.request_count",
                    (
                        device["id"], device["user_id"], device["client"], device["client_version"], device["platform"],
                        device["user_agent"], device["ip"], device["seen"], device["seen"], device["count"],
                    ),
                )
            conn.execute(
                "INSERT INTO worker_heartbeats(id,host,pid,ppid,release,started_at,last_seen_at,rss_mb,cpu_seconds,threads,"
                "in_flight,active_streams,requests_total,errors_total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (id) DO UPDATE SET last_seen_at = excluded.last_seen_at, rss_mb = excluded.rss_mb, "
                "cpu_seconds = excluded.cpu_seconds, threads = excluded.threads, in_flight = excluded.in_flight, "
                "active_streams = excluded.active_streams, requests_total = excluded.requests_total, "
                "errors_total = excluded.errors_total, release = excluded.release",
                (
                    f"{HOST}:{os.getpid()}", HOST, os.getpid(), os.getppid(), RELEASE, STARTED_AT, seen_at,
                    round(proc["rss_mb"], 1), round(time.process_time(), 2), int(proc["threads"]),
                    snapshot["in_flight"], snapshot["active_streams"], snapshot["requests_total"], snapshot["errors_total"],
                ),
            )
            _flush_model_calls(conn, model_calls)
    except Exception:
        # Model measurements survive transient DB failures; IDs make retries
        # idempotent if the connection outcome was uncertain.
        with _lock:
            _model_calls[:0] = model_calls[:max(0, 10000 - len(_model_calls))]
        logger.exception("telemetry flush failed; %d request rows dropped", len(requests))


def prune() -> None:
    now = datetime.now(timezone.utc)
    try:
        with get_connection() as conn:
            conn.execute("DELETE FROM model_calls WHERE created_at < ?", ((now - timedelta(days=MODEL_CALL_DAYS)).isoformat(),))
            conn.execute("DELETE FROM request_log WHERE created_at < ?", ((now - timedelta(days=REQUEST_LOG_DAYS)).isoformat(),))
            conn.execute("DELETE FROM api_stats_hourly WHERE hour < ?", ((now - timedelta(days=STATS_DAYS)).isoformat()[:13] + ":00",))
            conn.execute("DELETE FROM worker_heartbeats WHERE last_seen_at < ?", ((now - timedelta(days=1)).isoformat(),))
    except Exception:
        logger.exception("telemetry prune failed")


async def _run_telemetry_work(function: Any) -> None:
    """Drain the SQL thread even when lifespan uses asyncio task cancellation."""

    task = asyncio.create_task(run_in_threadpool(function))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # AnyIO's cancel scope does not intercept a direct Task.cancel().
        # Keep its worker future alive, then wait before final flush/teardown.
        await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        raise


async def telemetry_loop() -> None:
    """Flush every few seconds until the lifespan cancels this task."""

    last_prune = 0.0
    try:
        while True:
            await _run_telemetry_work(flush)
            if time.monotonic() - last_prune >= PRUNE_SECONDS:
                last_prune = time.monotonic()
                await _run_telemetry_work(prune)
            await asyncio.sleep(FLUSH_SECONDS)
    except asyncio.CancelledError:
        await _run_telemetry_work(flush)
        raise
