"""Optional single sign-on provider.

Tickets and account passwords are checked by the configured identity service.
This module stores no upstream password. Shared session issuance stays in app.auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from ..db import (
    CacheUnavailable,
    cache_delete_strict,
    cache_eval_strict,
    cache_pop_strict,
    cache_set,
    get_connection,
    ping_redis,
)

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
_STATE_PREFIX = "luma:sso:state:"
_STATE_TTL = 600
_PASSWORD_PREFIX = "luma:auth:sso-password:"
_PASSWORD_INVALID_DETAIL = "账号或密码错误"
_PASSWORD_UNAVAILABLE_DETAIL = "登录服务暂时不可用"
_PASSWORD_LOCKED_DETAIL = "尝试次数过多，请 15 分钟后再试"
_CACHE_UNAVAILABLE_DETAIL = "登录服务暂时不可用，请稍后重试"
_DEFAULT_SYSTEM_CODE = "assistant"

# Optional profile keys. All are omitted when the identity service does not send them.
_PROFILE_KEYS = (
    "job_number",
    "nickname",
    "phone_number",
    "email",
    "department_id",
    "department_name",
    "department_path",
    "position",
    "job_title",
    "avatar_url",
    "status",
)
_COLUMN_SOURCES = (
    ("job_number", "job_number"),
    ("phone_number", "phone_number"),
    ("department_name", "department_name"),
    ("department_path", "department_path"),
    ("job_title", "position"),
    ("avatar_url", "avatar_url"),
    ("nickname", "nickname"),
)

_PASSWORD_ATTEMPT_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
if redis.call('ZCARD', KEYS[1]) >= 20 then return 2 end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 60000)
if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
return 0
"""
_PASSWORD_FAILURE_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 900000)
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 900000)
if redis.call('ZCARD', KEYS[1]) >= 5 then
    redis.call('SET', KEYS[2], '1', 'PX', 900000)
    redis.call('DEL', KEYS[1])
    return 1
end
return 0
"""


def _auth() -> Any:
    from .. import auth as session

    return session


def _env(name: str, default: str = "") -> str:
    return _auth()._env(name, default)


def _bool_env(name: str, default: bool = False) -> bool:
    return _auth()._bool_env(name, default)


def _safe_url(value: str, *, default: str = "") -> str:
    value = (value or default).strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return value


def sso_config() -> dict[str, Any]:
    """Read SSO settings. There is no built-in service host."""

    base_url = _safe_url(_env("SSO_BASE_URL"))
    verify_url = _safe_url(_env("SSO_VERIFY_URL"), default=f"{base_url}/api/sso/verify" if base_url else "")
    login_url = _safe_url(
        _env("SSO_FEDERATED_LOGIN_URL"),
        default=f"{base_url}/api/sso/federated-login" if base_url else "",
    )
    app_base_url = _safe_url(_env("SSO_APP_BASE_URL"))
    callback_url = _safe_url(
        _env("SSO_CALLBACK_URL"),
        default=f"{app_base_url}/sso-callback" if app_base_url else "",
    )
    try:
        timeout_value = _env("SSO_VERIFY_TIMEOUT_SECONDS") or _env("SSO_TIMEOUT_SECONDS", "8")
        timeout = max(1.0, min(float(timeout_value), 30.0))
    except ValueError:
        timeout = 8.0
    system_code = _env("SSO_SYSTEM_CODE") or _DEFAULT_SYSTEM_CODE
    return {
        "base_url": base_url,
        "login_url": login_url,
        "verify_url": verify_url,
        "callback_url": callback_url,
        "system_code": system_code,
        "api_secret": _env("SSO_API_SECRET") or _env("SSO_SYSTEM_SECRET"),
        "session_secret": _env("AUTH_SESSION_SECRET"),
        "timeout": timeout,
    }


def missing_sso_settings() -> list[str]:
    config = sso_config()
    missing = []
    if not config["base_url"]:
        missing.append("SSO_BASE_URL")
    if not config["api_secret"]:
        missing.append("SSO_API_SECRET")
    if not config["session_secret"]:
        missing.append("AUTH_SESSION_SECRET")
    return missing


def _configured(config: dict[str, Any]) -> bool:
    return not missing_sso_settings() and bool(config["login_url"] and config["verify_url"])


def _url_origin(value: str) -> Optional[str]:
    parsed = urlsplit(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"


def sso_public_status() -> dict[str, Any]:
    """Public SSO status. Secrets and the upstream password are never included."""

    config = sso_config()
    configured = _configured(config)
    return {
        "enabled": configured,
        "configured": configured,
        "password_login": configured and _bool_env("AUTH_PASSWORD_LOGIN_ENABLED", True),
        "provider": "sso",
        "base_origin": _url_origin(config["base_url"]),
        "redis_reachable": ping_redis(),
    }


def _label(name: str, default: str) -> str:
    value = _env(name)
    return value or default


def _require_sso() -> None:
    from . import enabled_provider_names

    try:
        names = enabled_provider_names()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="登录配置无效") from None
    if "sso" not in names:
        raise HTTPException(status_code=404, detail="登录方式未启用")


def _validate_relative_path(value: str) -> str:
    value = (value or "/").strip()
    if not value or not value.startswith("/") or value.startswith("//"):
        raise HTTPException(status_code=400, detail="next 必须是站内相对路径")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or "://" in value
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise HTTPException(status_code=400, detail="next 必须是站内相对路径")
    return value


def _require_config(config: dict[str, Any]) -> None:
    missing = missing_sso_settings()
    if not config["login_url"] or not config["verify_url"]:
        if "SSO_BASE_URL" not in missing:
            missing.append("SSO_BASE_URL")
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "sso_not_configured", "message": "单点登录未配置", "missing": sorted(set(missing))},
        )


def _signed_state(state_id: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), state_id.encode(), hashlib.sha256).digest()
    return f"{state_id}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def _state_id(state: str, secret: str) -> str:
    if not isinstance(state, str) or "." not in state:
        raise HTTPException(status_code=400, detail="SSO state 无效")
    state_id, signature = state.split(".", 1)
    if not _STATE_PATTERN.fullmatch(state_id) or not signature:
        raise HTTPException(status_code=400, detail="SSO state 无效")
    expected = _signed_state(state_id, secret)
    if not hmac.compare_digest(expected, state):
        raise HTTPException(status_code=400, detail="SSO state 无效")
    return state_id


def build_sso_start_url(next_path: str = "/") -> tuple[str, str]:
    config = sso_config()
    _require_config(config)
    next_path = _validate_relative_path(next_path)
    state_id = secrets.token_urlsafe(32)
    state = _signed_state(state_id, config["session_secret"])
    if not cache_set(
        f"{_STATE_PREFIX}{state_id}",
        json.dumps({"next": next_path, "created_at": int(time.time())}, ensure_ascii=False),
        _STATE_TTL,
    ):
        raise HTTPException(status_code=503, detail="登录状态存储不可用")
    query = {
        "redirect_system": config["system_code"],
        "redirect_path": next_path,
        "callback_state": state,
    }
    if config.get("callback_url"):
        query["redirect_uri"] = config["callback_url"]
    return f"{config['login_url']}?{urlencode(query, quote_via=quote)}", state


def _validate_ticket(ticket: str) -> str:
    if not isinstance(ticket, str) or not _TOKEN_PATTERN.fullmatch(ticket):
        raise HTTPException(status_code=400, detail="SSO ticket 无效")
    return ticket


def _clip(value: Any, limit: int = 500) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    if not text:
        return None
    return text[:limit]


def _safe_user(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("SSO user payload is not an object")
    user_id = raw.get("user_id")
    if user_id is None or str(user_id).strip() == "":
        raise ValueError("SSO user payload has no user_id")
    user = {"user_id": str(user_id).strip()}
    for field in _PROFILE_KEYS:
        if field in raw:
            user[field] = raw[field]
    return user


def _sso_request_upstream(
    url: str,
    payload: dict[str, Any],
    config: dict[str, Any],
    *,
    credential_error: str,
    unavailable_error: str,
    invalid_error: str,
    credential_statuses: tuple[int, ...],
) -> dict[str, Any]:
    timestamp = str(int(time.time() * 1000))
    signature = base64.b64encode(
        hmac.new(config["api_secret"].encode(), timestamp.encode(), hashlib.sha256).digest()
    ).decode()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = UrlRequest(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-System-Code": config["system_code"],
            "X-System-Signature-Timestamp": timestamp,
            "X-System-Signature": signature,
        },
    )
    try:
        with urlopen(request, timeout=config["timeout"]) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        code = exc.code
        try:
            exc.close()
        except Exception:
            pass
        if code in credential_statuses:
            raise HTTPException(status_code=401, detail=credential_error) from None
        raise HTTPException(status_code=502, detail=unavailable_error) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise HTTPException(status_code=502, detail=unavailable_error) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=invalid_error)
    if payload.get("success") is not True:
        raise HTTPException(status_code=401, detail=credential_error)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=invalid_error)
    try:
        return _safe_user(data.get("user"))
    except ValueError:
        raise HTTPException(status_code=502, detail=invalid_error) from None


def _verify_ticket_upstream(ticket: str, config: dict[str, Any]) -> dict[str, Any]:
    return _sso_request_upstream(
        config["verify_url"], {"ticket": ticket}, config,
        credential_error="SSO ticket 无效或已过期",
        unavailable_error="SSO 服务暂时不可用",
        invalid_error="SSO 响应格式无效",
        credential_statuses=(400, 401, 403, 404),
    )


def _profile_document(user: dict[str, Any]) -> dict[str, str]:
    document = {}
    for key in _PROFILE_KEYS:
        value = _clip(user.get(key))
        if value:
            document[key] = value
    return document


def _column_values(user: dict[str, Any]) -> dict[str, str]:
    values = {}
    for column, source in _COLUMN_SOURCES:
        value = _clip(user.get(source))
        if column == "job_title" and not value:
            value = _clip(user.get("job_title"))
        if value:
            values[column] = value
    return values


def _password_claimed(row: Any) -> bool:
    provider = str(row.get("auth_provider") or "").strip().lower()
    if provider == "sso":
        return False
    if provider == "password":
        return True
    return bool(row.get("password_hash"))


def _optional_email(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return _auth().normalize_email(value)
    except HTTPException:
        return None


def _sso_username(user_id: str, attempt: int) -> str:
    if attempt == 0:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        return "sso_" + digest[:28]
    digest = hashlib.sha256(("%s:%s" % (attempt, user_id)).encode("utf-8")).hexdigest()
    return ("s" + digest)[:32]


def _unique_username(conn: Any, user_id: str) -> str:
    for attempt in range(6):
        username = _sso_username(user_id, attempt)
        taken = conn.execute(
            "SELECT 1 FROM users WHERE lower(username) = lower(?) AND user_id <> ?",
            (username, user_id),
        ).fetchone()
        if not taken:
            return username
    raise HTTPException(status_code=409, detail="无法分配登录名")


def upsert_user_profile(user: dict[str, Any]) -> dict[str, Any]:
    """Insert or refresh an SSO user. A password-owned user id is never claimed."""

    user_id = _clip(user.get("user_id"), 200)
    if not user_id:
        raise HTTPException(status_code=502, detail="SSO 响应格式无效")
    profile = _profile_document(user)
    columns = _column_values(user)
    nickname = _clip(user.get("nickname") or user.get("display_name"), 100)
    email = _optional_email(user.get("email"))
    now = _auth()._timestamp()
    profile_json = json.dumps(profile, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        row = conn.execute("SELECT * FROM users WHERE user_id = ? FOR UPDATE", (user_id,)).fetchone()
        if row and _password_claimed(row):
            raise HTTPException(status_code=409, detail="该用户已绑定其他登录方式")
        if row and row.get("status") == "disabled":
            raise HTTPException(status_code=403, detail="账号已停用")
        if row:
            assignments = [
                "auth_provider = 'sso'",
                "last_seen_at = ?",
                "last_login_at = ?",
                "login_count = login_count + 1",
                "profile = ?::jsonb",
            ]
            params: list[Any] = [now, now, profile_json]
            if nickname and not str(row.get("display_name") or "").strip():
                assignments.append("display_name = ?")
                params.append(nickname)
            for column, value in columns.items():
                assignments.append(column + " = ?")
                params.append(value)
            if email and not row.get("email"):
                taken = conn.execute(
                    "SELECT 1 FROM users WHERE email = ? AND user_id <> ?",
                    (email, user_id),
                ).fetchone()
                if not taken:
                    assignments.append("email = ?")
                    params.append(email)
            params.append(user_id)
            updated = conn.execute(
                "UPDATE users SET " + ", ".join(assignments) + " WHERE user_id = ? RETURNING *",
                tuple(params),
            ).fetchone()
            return dict(updated)
        username = _unique_username(conn, user_id)
        if email:
            taken = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
            if taken:
                email = None
        display = nickname or username
        inserted = conn.execute(
            "INSERT INTO users(user_id,username,email,password_hash,display_name,nickname,role,status,"
            "created_at,first_seen_at,last_seen_at,last_login_at,login_count,session_version,auth_provider,profile,"
            "job_number,phone_number,department_name,department_path,job_title,avatar_url) "
            "VALUES (?,?,?,NULL,?,?,'user','active',?,?,?,?,1,0,'sso',?::jsonb,?,?,?,?,?,?) RETURNING *",
            (
                user_id, username, email, display, columns.get("nickname") or display, now, now, now, now,
                profile_json, columns.get("job_number"), columns.get("phone_number"),
                columns.get("department_name"), columns.get("department_path"),
                columns.get("job_title"), columns.get("avatar_url"),
            ),
        ).fetchone()
        return dict(inserted)


def _password_account_keys(account: str) -> tuple[str, str]:
    digest = hashlib.sha256(account.strip().casefold().encode("utf-8")).hexdigest()
    return f"{_PASSWORD_PREFIX}failures:{digest}", f"{_PASSWORD_PREFIX}lock:{digest}"


def _password_locked() -> HTTPException:
    return HTTPException(status_code=429, detail=_PASSWORD_LOCKED_DETAIL, headers={"Retry-After": "900"})


def _cache_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=_CACHE_UNAVAILABLE_DETAIL)


def sso_password_login(account: str, password: str, response: Any, request: Request) -> dict[str, Any]:
    """Forward an account password to the identity service and issue a local session."""

    if not _bool_env("AUTH_PASSWORD_LOGIN_ENABLED", True):
        raise HTTPException(status_code=404, detail="账号密码登录未启用")
    config = sso_config()
    if not _configured(config):
        raise HTTPException(status_code=502, detail=_PASSWORD_UNAVAILABLE_DETAIL)
    account = account.strip()
    failures_key, lock_key = _password_account_keys(account)
    ip = request.client.host if request.client else "unknown"
    ip_key = f"{_PASSWORD_PREFIX}ip:{hashlib.sha256(ip.encode('utf-8')).hexdigest()}"
    try:
        limited = cache_eval_strict(_PASSWORD_ATTEMPT_SCRIPT, [ip_key, lock_key], [secrets.token_hex(16)])
    except CacheUnavailable:
        raise _cache_unavailable() from None
    if limited == 2:
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试", headers={"Retry-After": "60"})
    if limited == 1:
        raise _password_locked()
    try:
        user = _sso_request_upstream(
            f"{config['base_url']}/api/sso/authenticate",
            {"account": account, "password": password},
            config,
            credential_error=_PASSWORD_INVALID_DETAIL,
            unavailable_error=_PASSWORD_UNAVAILABLE_DETAIL,
            invalid_error=_PASSWORD_UNAVAILABLE_DETAIL,
            credential_statuses=(401,),
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            try:
                locked = cache_eval_strict(_PASSWORD_FAILURE_SCRIPT, [failures_key, lock_key], [secrets.token_hex(16)])
            except CacheUnavailable:
                raise _cache_unavailable() from None
            if locked:
                raise _password_locked() from None
        raise
    try:
        cache_delete_strict(failures_key)
    except CacheUnavailable:
        raise _cache_unavailable() from None
    row = upsert_user_profile(user)
    return _auth()._issue_session(row, response, request, "/app")


def exchange_ticket(
    ticket: str,
    state: Optional[str],
    response: Any,
    request: Optional[Request] = None,
) -> dict[str, Any]:
    config = sso_config()
    _require_config(config)
    ticket = _validate_ticket(ticket)
    next_path = "/app"
    if state:
        state_id = _state_id(state, config["session_secret"])
        try:
            consumed = cache_pop_strict(f"{_STATE_PREFIX}{state_id}")
        except CacheUnavailable:
            raise _cache_unavailable() from None
        if not consumed:
            raise HTTPException(status_code=400, detail="SSO state 无效或已使用")
        try:
            transaction = json.loads(consumed)
        except (TypeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="SSO state 无效") from None
        next_path = _validate_relative_path(str(transaction.get("next") or "/app"))
    user = _verify_ticket_upstream(ticket, config)
    row = upsert_user_profile(user)
    return _auth()._issue_session(row, response, request, next_path)


class SsoExchangePayload(BaseModel):
    ticket: str = Field(min_length=16, max_length=256)
    state: Optional[str] = Field(default=None, min_length=32, max_length=512)


class PasswordLoginPayload(BaseModel):
    account: str = Field(min_length=1, max_length=64, strict=True)
    password: str = Field(min_length=1, max_length=128, strict=True, repr=False)


router = APIRouter()


async def _password_payload(request: Request) -> PasswordLoginPayload:
    _require_sso()
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请使用 JSON 提交登录信息")
    try:
        payload = PasswordLoginPayload.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise HTTPException(status_code=422, detail="登录信息格式无效") from None
    if not payload.account.strip():
        raise HTTPException(status_code=422, detail="登录信息格式无效")
    return payload


def _set_json_body(response: JSONResponse, content: Any) -> JSONResponse:
    response.body = JSONResponse(content=content).body
    response.headers["content-length"] = str(len(response.body))
    return response


@router.post(
    "/api/v1/auth/password/login",
    openapi_extra={"requestBody": {
        "required": True,
        "content": {"application/json": {"schema": PasswordLoginPayload.model_json_schema()}},
    }},
)
def auth_password_login(request: Request, payload: PasswordLoginPayload = Depends(_password_payload)) -> JSONResponse:
    response = JSONResponse(content={})
    data = sso_password_login(payload.account, payload.password, response, request)
    return _set_json_body(response, data)


@router.get("/api/v1/auth/sso/status")
def sso_status() -> dict[str, Any]:
    _require_sso()
    return sso_public_status()


@router.get("/api/v1/auth/sso/start")
def sso_start(next_path: str = Query(default="/app", alias="next")) -> dict[str, Any]:
    _require_sso()
    url, state = build_sso_start_url(next_path)
    return {"url": url, "state": state, "provider": "sso"}


@router.post("/api/v1/auth/sso/exchange")
def sso_exchange(request: Request, payload: SsoExchangePayload) -> JSONResponse:
    _require_sso()
    response = JSONResponse(content={})
    data = exchange_ticket(payload.ticket, payload.state, response, request=request)
    return _set_json_body(response, data)


class SsoProvider:
    name = "sso"

    def public_config(self) -> dict[str, Any]:
        config = sso_config()
        origin = _url_origin(config["base_url"])
        account_label = _label("AUTH_ACCOUNT_LABEL", "账号")
        sso_label = _label("AUTH_SSO_LABEL", "单点登录")
        entries = [{
            "name": self.name,
            "label": sso_label,
            "kind": "redirect",
            "origin": origin,
        }]
        if _bool_env("AUTH_PASSWORD_LOGIN_ENABLED", True):
            entries.append({
                "name": self.name,
                "label": account_label,
                "kind": "credentials",
                "account_label": account_label,
            })
        return {"entries": entries, "account_label": account_label, "sso_label": sso_label}

    def routes(self) -> APIRouter:
        return router
