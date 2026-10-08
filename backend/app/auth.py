"""Independent accounts and Redis-backed cookie/bearer authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request, Response
from .db import (
    CacheUnavailable, cache_delete_strict, cache_eval_strict, cache_get_strict,
    cache_sadd_strict, cache_set_existing_strict, cache_set_strict,
    cache_smembers_strict, cache_srem_strict, get_connection,
)

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_SESSION_COOKIE = "luma_session"
_SESSION_PREFIX = "luma:auth:session:"
_USER_SESSION_PREFIX = "luma:user_sessions:"
_SESSION_TTL = 7 * 24 * 60 * 60
_AUTH_CACHE_UNAVAILABLE_DETAIL = "登录服务暂时不可用，请稍后重试"
_PUBLIC_FIELDS = ("user_id", "username", "email", "display_name", "role")

def _read_dotenv(path: Path = ENV_PATH) -> None:
    """Read local configuration without logging or exposing its contents."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or name in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


def _env(name: str, default: str = "") -> str:
    _read_dotenv()
    return os.getenv(name, default).strip()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name)
    return raw.lower() in {"1", "true", "yes", "on"} if raw else default


def _auth_required() -> bool:
    # Every business endpoint requires an account, including development.
    return True


def _cache_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=_AUTH_CACHE_UNAVAILABLE_DETAIL)


def _cache_call(callback: Any, *args: Any) -> Any:
    try:
        return callback(*args)
    except CacheUnavailable:
        raise _cache_unavailable() from None


def _session_ttl_seconds() -> int:
    try:
        days = float(_env("AUTH_SESSION_TTL_DAYS", "7"))
        return max(1, int(days * 86400))
    except (TypeError, ValueError, OverflowError):
        return _SESSION_TTL


def _session_max_seconds() -> int:
    try:
        days = float(_env("AUTH_SESSION_MAX_DAYS", "30"))
        return max(1, int(days * 86400))
    except (TypeError, ValueError, OverflowError):
        return 30 * 86400


def validate_auth_configuration() -> None:
    if len(_env("AUTH_SESSION_SECRET")) < 32:
        raise RuntimeError("AUTH_SESSION_SECRET must contain at least 32 characters")
    from .auth_providers import validate_provider_configuration
    validate_provider_configuration()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_user(row: Any) -> dict[str, Any]:
    return {name: row.get(name) for name in _PUBLIC_FIELDS}


def get_account(user_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def _token_hash(token: str) -> str:
    return hmac.new(_env("AUTH_SESSION_SECRET").encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _session_key(token_or_hash: str, *, is_hash: bool = False) -> str:
    return _SESSION_PREFIX + (token_or_hash if is_hash else _token_hash(token_or_hash))


def _session_index_key(user_id: str) -> str:
    return _USER_SESSION_PREFIX + user_id


def _request_token(request: Request) -> Optional[str]:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return value.strip()
    cookie = request.cookies.get(_SESSION_COOKIE)
    return cookie.strip() if cookie else None


def _client_metadata(request: Optional[Request]) -> tuple[str, str]:
    if request is None:
        return "web", "unknown"
    headers = request.headers
    client = (headers.get("x-luma-client") or headers.get("x-client-type") or "").strip().lower()
    platform = (headers.get("x-luma-platform") or headers.get("x-platform") or "").strip().lower()
    user_agent = headers.get("user-agent", "").lower()
    if not client:
        client = "mobile" if any(value in user_agent for value in ("android", "iphone", "ipad", "mobile")) else "web"
    if not platform:
        platform = next((name for name, match in (("android", "android"), ("ios", "iphone"),
                         ("ios", "ipad"), ("macos", "mac os"), ("windows", "windows"),
                         ("linux", "linux")) if match in user_agent), "unknown")
    return client[:64], platform[:64]


def _session_cookie_config(max_age: Optional[int] = None) -> dict[str, Any]:
    return {"key": _SESSION_COOKIE, "httponly": True, "secure": _bool_env("AUTH_COOKIE_SECURE", True),
            "samesite": "lax", "path": "/", "max_age": max_age if max_age is not None else _session_ttl_seconds()}


def refresh_session_cookie(request: Request, response: Response) -> None:
    """Keep a browser cookie in step with the renewed Redis lifetime."""
    if not getattr(request.state, "luma_session_renewed", False):
        return
    scheme = request.headers.get("authorization", "").partition(" ")[0]
    if scheme.lower() == "bearer":
        return
    # Explicit issuance/revocation wins over the previous session read by
    # telemetry. In particular logout must never recreate its cookie.
    if any(name.lower() == b"set-cookie" and value.startswith((_SESSION_COOKIE + "=").encode("ascii"))
           for name, value in response.raw_headers):
        return
    token = request.cookies.get(_SESSION_COOKIE)
    session = getattr(request.state, "luma_session", None)
    if not token or not _TOKEN_PATTERN.fullmatch(token) or not session:
        return
    remaining = min(int(session["expires_at"]), int(session["absolute_expires_at"])) - int(time.time())
    if remaining > 0:
        response.set_cookie(value=token, **_session_cookie_config(remaining))


def _issue_session(user: dict[str, Any], response: Response, request: Optional[Request], next_path: str) -> dict[str, Any]:
    if len(_env("AUTH_SESSION_SECRET")) < 32:
        raise HTTPException(status_code=503, detail="登录服务未配置")
    session_token = secrets.token_urlsafe(32)
    session_hash = _token_hash(session_token)
    created_at = int(time.time())
    absolute_expires_at = created_at + _session_max_seconds()
    expires_at = min(created_at + _session_ttl_seconds(), absolute_expires_at)
    client_type, platform = _client_metadata(request)
    safe_user = public_user(user)
    session_payload = {
        "user": safe_user, "session_version": int(user.get("session_version") or 0),
        "session_id": session_hash, "created_at": created_at, "last_used_at": created_at,
        "expires_at": expires_at, "absolute_expires_at": absolute_expires_at,
        "client_type": client_type, "platform": platform, "next": next_path,
    }
    ttl = max(1, expires_at - created_at)
    stored = _cache_call(cache_set_strict, _session_key(session_hash, is_hash=True),
                         json.dumps(session_payload, ensure_ascii=False), ttl)
    if not stored:
        raise _cache_unavailable()
    try:
        cache_sadd_strict(_session_index_key(str(user["user_id"])), session_hash)
    except CacheUnavailable:
        try:
            cache_delete_strict(_session_key(session_hash, is_hash=True))
        except CacheUnavailable:
            pass
        raise _cache_unavailable() from None
    response.set_cookie(value=session_token, **_session_cookie_config(ttl))
    return {"authenticated": True, "user": safe_user, "redirect_to": next_path,
            "access_token": session_token, "token_type": "Bearer", "expires_in": ttl}


def _decode_session(raw: Any) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("user"), dict) else None


def _discard_session(session_hash: str, user_id: str) -> None:
    _cache_call(cache_delete_strict, _session_key(session_hash, is_hash=True))
    _cache_call(cache_srem_strict, _session_index_key(user_id), session_hash)


def _session_data(request: Request) -> Optional[dict[str, Any]]:
    token = _request_token(request)
    if not token or not _TOKEN_PATTERN.fullmatch(token):
        return None
    session_hash = _token_hash(token)
    key = _session_key(session_hash, is_hash=True)
    data = _decode_session(_cache_call(cache_get_strict, key))
    if not data:
        return None
    user_id = str(data["user"].get("user_id") or "")
    with get_connection() as conn:
        # Password changes hold FOR UPDATE through the kept Redis session
        # rewrite. Hold this shared row lock through validation and renewal,
        # then reread Redis after acquiring it so an earlier snapshot cannot
        # delete or overwrite that newly updated session.
        account = conn.execute("SELECT * FROM users WHERE user_id = ? FOR SHARE", (user_id,)).fetchone()
        data = _decode_session(_cache_call(cache_get_strict, key))
        if not data:
            return None
        if str(data["user"].get("user_id") or "") != user_id:
            return None
        now = int(time.time())
        try:
            created_at = int(data["created_at"])
            expires_at = int(data["expires_at"])
            absolute_expires_at = int(data["absolute_expires_at"])
            version = int(data["session_version"])
            last_used_at = int(data.get("last_used_at", created_at))
        except (KeyError, ValueError, TypeError):
            _discard_session(session_hash, user_id)
            return None
        if (now >= expires_at or now >= absolute_expires_at or not account
                or account.get("status") != "active" or int(account.get("session_version") or 0) != version):
            _discard_session(session_hash, user_id)
            return None
        data["user"] = public_user(account)
        data["session_id"] = session_hash
        remaining = expires_at - now
        desired_expires_at = min(now + _session_ttl_seconds(), absolute_expires_at)
        # Batch writes at most once a minute, while keeping expiry tied to
        # recent activity rather than waiting for half the lifetime to pass.
        renew_due = now - last_used_at >= 60 and expires_at < desired_expires_at
        if renew_due:
            expires_at = desired_expires_at
            remaining = max(1, expires_at - now)
            data["expires_at"] = expires_at
        if renew_due or now - last_used_at >= 60:
            data["last_used_at"] = now
            if not _cache_call(cache_set_existing_strict, key, json.dumps(data, ensure_ascii=False), remaining):
                return None  # Revocation won: SET XX must never resurrect a token.
        if renew_due:
            request.state.luma_session_renewed = True
        request.state.luma_session = data
        return data


def current_user(request: Request) -> Optional[dict[str, Any]]:
    if "luma_user" in request.state._state:
        return request.state.luma_user
    data = _session_data(request)
    user = data.get("user") if data else None
    request.state.luma_user = user
    return user


def current_user_id(request: Request, *, required: Optional[bool] = None) -> str:
    user = current_user(request)
    if user and str(user.get("user_id") or "").strip():
        return str(user["user_id"])
    raise HTTPException(status_code=401, detail="未登录")


def _session_public(data: dict[str, Any], session_hash: str) -> dict[str, Any]:
    client = str(data.get("client_type") or "web")
    return {"id": session_hash, "client": client, "client_type": client,
            "platform": str(data.get("platform") or "unknown"), "created_at": data.get("created_at"),
            "last_used_at": data.get("last_used_at") or data.get("created_at")}


def list_auth_sessions(request: Request) -> list[dict[str, Any]]:
    user_id = current_user_id(request)
    index_key = _session_index_key(user_id)
    sessions = []
    with get_connection() as conn:
        # Listing can discard stale entries, so it must share the same
        # account-generation lock used by authentication and password changes.
        account = conn.execute("SELECT * FROM users WHERE user_id = ? FOR SHARE", (user_id,)).fetchone()
        if not account or account.get("status") != "active":
            raise HTTPException(status_code=401, detail="未登录")
        members = _cache_call(cache_smembers_strict, index_key)
        now = int(time.time())
        for member in members:
            if not re.fullmatch(r"[0-9a-f]{64}", member):
                _cache_call(cache_srem_strict, index_key, member)
                continue
            data = _decode_session(_cache_call(cache_get_strict, _session_key(member, is_hash=True)))
            if not data:
                _cache_call(cache_srem_strict, index_key, member)
                continue
            if str(data["user"].get("user_id") or "") != user_id:
                _cache_call(cache_srem_strict, index_key, member)
                continue
            try:
                valid = (int(data["expires_at"]) > now and int(data["absolute_expires_at"]) > now
                         and int(data["session_version"]) == int(account.get("session_version") or 0))
            except (KeyError, ValueError, TypeError):
                valid = False
            if not valid:
                _discard_session(member, user_id)
                continue
            sessions.append(_session_public(data, member))
    sessions.sort(key=lambda value: value.get("last_used_at") or 0, reverse=True)
    return sessions


def revoke_auth_session_by_id(request: Request, session_id: str) -> bool:
    user_id = current_user_id(request)
    if not re.fullmatch(r"[0-9a-f]{64}", session_id or ""):
        return False
    members = _cache_call(cache_smembers_strict, _session_index_key(user_id))
    if session_id not in members:
        return False
    data = _decode_session(_cache_call(cache_get_strict, _session_key(session_id, is_hash=True)))
    if data and str(data["user"].get("user_id") or "") != user_id:
        return False
    _discard_session(session_id, user_id)
    return True


def revoke_user_sessions(user_id: str, keep_session_hash: Optional[str] = None) -> int:
    """Remove indexed sessions; callers increment the DB generation as needed."""
    index_key = _session_index_key(user_id)
    members = _cache_call(cache_smembers_strict, index_key)
    count = 0
    for member in members:
        if member == keep_session_hash:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", member):
            _cache_call(cache_delete_strict, _session_key(member, is_hash=True))
            count += 1
        _cache_call(cache_srem_strict, index_key, member)
    return count


def logout_all(request: Request, response: Response) -> int:
    user_id = current_user_id(request)
    with get_connection() as conn:
        conn.execute("UPDATE users SET session_version = session_version + 1 WHERE user_id = ?", (user_id,))
    count = revoke_user_sessions(user_id)
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return count


def logout(request: Request, response: Response) -> None:
    token = _request_token(request)
    if token and _TOKEN_PATTERN.fullmatch(token):
        session_hash = _token_hash(token)
        data = _decode_session(_cache_call(cache_get_strict, _session_key(session_hash, is_hash=True)))
        if data:
            _discard_session(session_hash, str(data["user"].get("user_id") or ""))
        else:
            _cache_call(cache_delete_strict, _session_key(session_hash, is_hash=True))
    response.delete_cookie(_SESSION_COOKIE, path="/")


from .auth_providers.password import install as _install_password_provider

_install_password_provider(globals())
