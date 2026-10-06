"""Independent accounts and Redis-backed cookie/bearer authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request, Response
from psycopg2 import IntegrityError

from .db import (
    CacheUnavailable, cache_delete_strict, cache_eval_strict, cache_get_strict,
    cache_sadd_strict, cache_set_existing_strict, cache_set_strict,
    cache_smembers_strict, cache_srem_strict, get_connection,
)

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError
    from argon2.low_level import Type
except ImportError:  # Standard-library fallback for minimal installations.
    PasswordHasher = None

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SESSION_COOKIE = "luma_session"
_SESSION_PREFIX = "luma:auth:session:"
_USER_SESSION_PREFIX = "luma:user_sessions:"
_SESSION_TTL = 7 * 24 * 60 * 60
_PASSWORD_PREFIX = "luma:auth:password:"
_PASSWORD_INVALID_DETAIL = "用户名或密码错误"
_PASSWORD_LOCKED_DETAIL = "尝试次数过多，请 15 分钟后再试"
_AUTH_CACHE_UNAVAILABLE_DETAIL = "登录服务暂时不可用，请稍后重试"
_PUBLIC_FIELDS = ("user_id", "username", "email", "display_name", "role")

# A small built-in common-password denylist. Case folding also rejects common
# spelling variants while the actual password remains case sensitive.
_COMMON_PASSWORDS = frozenset("""
123456 password 123456789 12345678 12345 1234567 1234567890 qwerty abc123
111111 123123 admin letmein welcome monkey dragon football iloveyou sunshine
princess 654321 password1 000000 qwerty123 1q2w3e4r asdfghjkl zxcvbnm
passw0rd login hello charlie donald master qazwsx trustno1 shadow
superman michael jennifer jordan hunter harley buster thomas robert daniel
hannah michelle jessica william ashley nicole tigger mustang baseball soccer
hockey killer freedom secret whatever cheese computer internet test guest
changeme default administrator 11111111 222222 333333 444444 555555
666666 7777777 888888 999999 121212 112233 123321 987654321 777777 55555555
password123 welcome1 admin123 qwertyuiop 1qaz2wsx qwe123 qazwsxedc 123qwe
zaq12wsx 123abc abcdef abcdefgh starwars pokemon summer winter
""".split())

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
_PASSWORD_SUCCESS_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then return 1 end
redis.call('DEL', KEYS[1])
return 0
"""
_REGISTRATION_ATTEMPT_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 3600000)
if redis.call('ZCARD', KEYS[1]) >= 10 then return 1 end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], 3600000)
return 0
"""


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
    if _env("AUTH_REGISTRATION", "invite").lower() not in {"open", "invite", "closed"}:
        raise RuntimeError("AUTH_REGISTRATION must be open, invite, or closed")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_user(row: Any) -> dict[str, Any]:
    return {name: row.get(name) for name in _PUBLIC_FIELDS}


def get_account(user_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def normalize_email(value: Optional[str]) -> Optional[str]:
    if value is None or not value.strip():
        return None
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise HTTPException(status_code=422, detail="邮箱格式无效")
    return email


def validate_password(password: str, username: str) -> None:
    if (
        len(password) < 8 or len(password) > 128
        or password.casefold() == username.casefold()
        or password.casefold() in _COMMON_PASSWORDS
    ):
        raise HTTPException(status_code=422, detail="密码必须为 8–128 位，且不能使用用户名或常见弱口令")


def _derive_scrypt(password: str, salt: bytes) -> bytes:
    implementation = getattr(hashlib, "scrypt", None)
    if implementation is not None:
        return implementation(password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024)
    # Some macOS Python builds omit OpenSSL's hashlib.scrypt. Cryptography is
    # already a required dependency and derives the identical standard bytes.
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=64, n=2 ** 15, r=8, p=1).derive(password.encode("utf-8"))


def hash_password(password: str) -> str:
    if PasswordHasher is not None:
        return PasswordHasher(type=Type.ID).hash(password)
    salt = secrets.token_bytes(16)
    digest = _derive_scrypt(password, salt)
    return "scrypt$32768$8$1$%s$%s" % (
        base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: Optional[str]) -> bool:
    if not isinstance(stored_hash, str):
        return False
    if stored_hash.startswith("$argon2id$"):
        if PasswordHasher is None:
            return False
        try:
            return PasswordHasher(type=Type.ID).verify(stored_hash, password)
        except (VerificationError, InvalidHashError):
            return False
    if stored_hash.startswith("scrypt$"):
        try:
            algorithm, n, r, p, encoded_salt, encoded_hash = stored_hash.split("$")
            if (algorithm, n, r, p) != ("scrypt", "32768", "8", "1"):
                return False
            salt = base64.b64decode(encoded_salt, validate=True)
            expected = base64.b64decode(encoded_hash, validate=True)
            if len(salt) != 16 or len(expected) != 64:
                return False
            actual = _derive_scrypt(password, salt)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
    return False


# Unknown identities still incur a real password verification.
_DUMMY_HASH = hash_password("dummy-verification-only-7b6e4419")


def registration_config() -> dict[str, bool]:
    with get_connection() as conn:
        has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    mode = _env("AUTH_REGISTRATION", "invite").lower()
    return {
        "registration_open": not has_users or mode in {"open", "invite"},
        "requires_invite": has_users and mode == "invite",
        "bootstrap_required": not has_users,
    }


def _request_ip(request: Request) -> str:
    # Proxy headers are handled only by the server's explicitly trusted proxy
    # configuration. Never trust a caller-provided forwarding header here.
    return request.client.host if request.client else "unknown"


def _prepare_account(
    username: str, password: str, email: Optional[str], display_name: Optional[str],
) -> tuple[Optional[str], str, str]:
    if not _USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(status_code=422, detail="用户名须为 3–32 位字母、数字、下划线、点或短横线")
    email = normalize_email(email)
    if display_name is not None and not display_name.strip():
        raise HTTPException(status_code=422, detail="显示名称不能为空")
    validate_password(password, username)
    return email, (display_name or username).strip(), hash_password(password)


def _insert_account(
    conn: Any, username: str, email: Optional[str], display_name: str,
    encoded: str, role: str, *, logged_in: bool,
) -> dict[str, Any]:
    duplicate = conn.execute(
        "SELECT 1 FROM users WHERE lower(username) = lower(?) OR (? IS NOT NULL AND email = ?)",
        (username, email, email),
    ).fetchone()
    if duplicate:
        raise HTTPException(status_code=409, detail="用户名或邮箱已被使用")
    timestamp = _timestamp()
    row = conn.execute(
        "INSERT INTO users(user_id,username,email,password_hash,display_name,role,status,created_at,"
        "first_seen_at,last_seen_at,last_login_at,password_changed_at,login_count,session_version) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?,0) RETURNING *",
        ("user_" + uuid.uuid4().hex, username, email, encoded, display_name, role,
         timestamp, timestamp, timestamp, timestamp if logged_in else None, timestamp, int(logged_in)),
    ).fetchone()
    return dict(row)


def create_admin_account(
    username: str, password: str, email: Optional[str] = None, *,
    force_additional_admin: bool = False,
) -> dict[str, Any]:
    """Create an administrator from the server CLI without issuing a session."""
    email, display_name, encoded = _prepare_account(username, password, email, None)
    try:
        with get_connection() as conn:
            # Use the registration lock so CLI and HTTP bootstrap cannot race.
            conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
            has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
            if has_users and not force_additional_admin:
                raise HTTPException(status_code=403, detail="数据库已有用户；如需额外管理员，请使用 --force-additional-admin")
            row = _insert_account(conn, username, email, display_name, encoded, "admin", logged_in=False)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="用户名或邮箱已被使用") from None
    return public_user(row)


def register_account(
    username: str, password: str, email: Optional[str], display_name: Optional[str],
    invite_code: Optional[str], response: Response, request: Request,
    bootstrap_token: Optional[str] = None,
) -> dict[str, Any]:
    ip_digest = hashlib.sha256(_request_ip(request).encode("utf-8")).hexdigest()
    limited = _cache_call(cache_eval_strict, _REGISTRATION_ATTEMPT_SCRIPT,
                          ["luma:auth:registration:" + ip_digest], [secrets.token_hex(16)])
    if limited:
        raise HTTPException(status_code=429, detail="注册尝试次数过多，请稍后再试", headers={"Retry-After": "3600"})
    email, display_name, encoded = _prepare_account(username, password, email, display_name)
    try:
        with get_connection() as conn:
            # Serializes bootstrap across processes and prevents two first
            # registrations from both obtaining the administrator role.
            conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
            first_user = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            mode = _env("AUTH_REGISTRATION", "invite").lower()
            if first_user:
                expected = _env("AUTH_BOOTSTRAP_TOKEN")
                if len(expected) < 16:
                    raise HTTPException(status_code=403, detail="尚未完成初始化：请在服务器上设置 AUTH_BOOTSTRAP_TOKEN 后创建首个管理员")
                if not hmac.compare_digest((bootstrap_token or "").encode("utf-8"), expected.encode("utf-8")):
                    raise HTTPException(status_code=403, detail="初始化令牌无效")
            else:
                if mode not in {"open", "invite"}:
                    raise HTTPException(status_code=403, detail="注册未开放")
                if mode == "invite":
                    expected = _env("AUTH_INVITE_CODE")
                    supplied = (invite_code or "").encode("utf-8")
                    if not expected or not hmac.compare_digest(supplied, expected.encode("utf-8")):
                        raise HTTPException(status_code=403, detail="邀请码无效")
            row = _insert_account(conn, username, email, display_name, encoded,
                                  "admin" if first_user else "user", logged_in=True)
            result = _issue_session(row, response, request, "/app")
    except IntegrityError:
        raise HTTPException(status_code=409, detail="用户名或邮箱已被使用") from None
    return result


def _password_account_keys(account: str) -> tuple[str, str]:
    digest = hashlib.sha256(account.casefold().encode("utf-8")).hexdigest()
    return f"{_PASSWORD_PREFIX}failures:{digest}", f"{_PASSWORD_PREFIX}lock:{digest}"


def _password_locked() -> HTTPException:
    return HTTPException(status_code=429, detail=_PASSWORD_LOCKED_DETAIL, headers={"Retry-After": "900"})


def password_login(login: str, password: str, response: Response, request: Request) -> dict[str, Any]:
    login = login.strip()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(username) = lower(?) OR email = ? FOR UPDATE",
            (login, login.lower()),
        ).fetchone()
        # Username and email aliases share the same account lock.
        identity = str(row["user_id"]) if row else "unknown:" + login.casefold()
        failures_key, lock_key = _password_account_keys(identity)
        ip_digest = hashlib.sha256(_request_ip(request).encode("utf-8")).hexdigest()
        limited = _cache_call(cache_eval_strict, _PASSWORD_ATTEMPT_SCRIPT,
                              [f"{_PASSWORD_PREFIX}ip:{ip_digest}", lock_key], [secrets.token_hex(16)])
        if limited == 2:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试", headers={"Retry-After": "60"})
        if limited == 1:
            raise _password_locked()
        matched = verify_password(password, row["password_hash"] if row and row.get("password_hash") else _DUMMY_HASH)
        if not row or not matched or row.get("status") != "active":
            locked = _cache_call(cache_eval_strict, _PASSWORD_FAILURE_SCRIPT,
                                 [failures_key, lock_key], [secrets.token_hex(16)])
            if locked:
                raise _password_locked()
            raise HTTPException(status_code=401, detail=_PASSWORD_INVALID_DETAIL)
        if _cache_call(cache_eval_strict, _PASSWORD_SUCCESS_SCRIPT, [failures_key, lock_key], []):
            raise _password_locked()
        timestamp = _timestamp()
        conn.execute("UPDATE users SET last_login_at = ?, login_count = login_count + 1 WHERE user_id = ?",
                     (timestamp, row["user_id"]))
        return _issue_session(dict(row), response, request, "/app")


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
