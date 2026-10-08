"""Local username and password accounts.

The functions below are the account implementation. ``install`` rebinds them
onto ``app.auth`` so existing callers and tests keep patching one module.
Session cookies and bearer tokens stay in ``app.auth``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from typing import Any, Optional, Type

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg2 import IntegrityError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..db import get_connection

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError
    from argon2.low_level import Type as ArgonType
except ImportError:  # Standard-library fallback for minimal installations.
    PasswordHasher = None
    ArgonType = None
    InvalidHashError = VerificationError = Exception

# Rebind name expected by password hashing and the historical auth module.
Type = ArgonType

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PASSWORD_PREFIX = "luma:auth:password:"
_PASSWORD_INVALID_DETAIL = "用户名或密码错误"
_PASSWORD_LOCKED_DETAIL = "尝试次数过多，请 15 分钟后再试"

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
        "first_seen_at,last_seen_at,last_login_at,password_changed_at,login_count,session_version,auth_provider) "
        "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?,0,'password') RETURNING *",
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
        # An SSO identity can share the users table but must not accept a local password,
        # even if a hash was written onto the row by mistake.
        external = bool(row and (row.get("auth_provider") or "") == "sso")
        stored_hash = None if external or not row or not row.get("password_hash") else row["password_hash"]
        matched = verify_password(password, stored_hash or _DUMMY_HASH)
        if external or not row or not matched or row.get("status") != "active":
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


def install(namespace: dict[str, Any]) -> None:
    """Publish account helpers on the shared auth module without moving sessions."""

    import types

    constants = (
        "PasswordHasher", "Type", "VerificationError", "InvalidHashError",
        "_USERNAME_PATTERN", "_EMAIL_PATTERN", "_PASSWORD_PREFIX",
        "_PASSWORD_INVALID_DETAIL", "_PASSWORD_LOCKED_DETAIL", "_COMMON_PASSWORDS",
        "_PASSWORD_ATTEMPT_SCRIPT", "_PASSWORD_FAILURE_SCRIPT",
        "_PASSWORD_SUCCESS_SCRIPT", "_REGISTRATION_ATTEMPT_SCRIPT",
        "_DUMMY_HASH", "base64", "uuid", "IntegrityError",
    )
    functions = (
        "normalize_email", "validate_password", "_derive_scrypt", "hash_password",
        "verify_password", "registration_config", "_request_ip", "_prepare_account",
        "_insert_account", "create_admin_account", "register_account",
        "_password_account_keys", "_password_locked", "password_login",
    )
    for name in constants:
        namespace[name] = globals()[name]
    for name in functions:
        value = globals()[name]
        if value.__code__.co_freevars:
            raise RuntimeError("password provider function has closures: " + name)
        bound = types.FunctionType(
            value.__code__, namespace, value.__name__, value.__defaults__, value.__closure__,
        )
        bound.__kwdefaults__ = value.__kwdefaults__
        bound.__annotations__ = dict(getattr(value, "__annotations__", {}))
        namespace[name] = bound


class JsonPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginPayload(JsonPayload):
    login: str = Field(min_length=1, max_length=254, strict=True)
    password: str = Field(min_length=1, max_length=128, strict=True, repr=False)


class RegisterPayload(JsonPayload):
    username: str = Field(min_length=3, max_length=32, strict=True)
    password: str = Field(min_length=8, max_length=128, strict=True, repr=False)
    email: Optional[str] = Field(default=None, max_length=254, strict=True)
    display_name: Optional[str] = Field(default=None, max_length=100, strict=True)
    invite_code: Optional[str] = Field(default=None, max_length=256, strict=True, repr=False)
    bootstrap_token: Optional[str] = Field(default=None, strict=True, repr=False)


router = APIRouter()


def _require_password() -> None:
    from . import enabled_provider_names

    try:
        names = enabled_provider_names()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="登录配置无效") from None
    if "password" not in names:
        raise HTTPException(status_code=404, detail="登录方式未启用")


async def json_payload(request: Request, model: Type[BaseModel]) -> Any:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请使用 JSON 提交信息")
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise HTTPException(status_code=422, detail="提交信息格式无效") from None


async def _login_payload(request: Request) -> LoginPayload:
    _require_password()
    payload = await json_payload(request, LoginPayload)
    if not payload.login.strip():
        raise HTTPException(status_code=422, detail="提交信息格式无效")
    return payload


async def _register_payload(request: Request) -> RegisterPayload:
    _require_password()
    return await json_payload(request, RegisterPayload)


def set_json_body(response: JSONResponse, content: Any) -> JSONResponse:
    response.body = JSONResponse(content=content).body
    response.headers["content-length"] = str(len(response.body))
    return response


def _json_request(model: Type[BaseModel]) -> dict[str, Any]:
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}


def _accounts() -> Any:
    from .. import auth as session

    return session


@router.get("/api/v1/auth/config")
def auth_config() -> dict[str, bool]:
    _require_password()
    return _accounts().registration_config()


@router.post("/api/v1/auth/register", openapi_extra=_json_request(RegisterPayload))
def auth_register(request: Request, payload: RegisterPayload = Depends(_register_payload)) -> JSONResponse:
    response = JSONResponse(content={})
    data = _accounts().register_account(
        payload.username, payload.password, payload.email, payload.display_name,
        payload.invite_code, response, request, bootstrap_token=payload.bootstrap_token,
    )
    return set_json_body(response, data)


@router.post("/api/v1/auth/login", openapi_extra=_json_request(LoginPayload))
def auth_login(request: Request, payload: LoginPayload = Depends(_login_payload)) -> JSONResponse:
    response = JSONResponse(content={})
    return set_json_body(response, _accounts().password_login(payload.login, payload.password, response, request))


class PasswordProvider:
    name = "password"

    def public_config(self) -> dict[str, Any]:
        config = _accounts().registration_config()
        return {
            "entries": [{"name": self.name, "label": "账号密码", "kind": "password"}],
            "registration_open": config["registration_open"],
            "requires_invite": config["requires_invite"],
            "bootstrap_required": config["bootstrap_required"],
        }

    def routes(self) -> APIRouter:
        return router
