"""Account profile, credential changes and password-confirmed erasure."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg2 import IntegrityError
from pydantic import Field

from .. import auth
from ..db import get_connection, lock_account_data
from ..storage import storage_for_row
from .auth import JsonPayload, _json_request, json_payload

router = APIRouter()


class PasswordPayload(JsonPayload):
    current_password: str = Field(min_length=1, max_length=128, strict=True, repr=False)
    new_password: str = Field(min_length=8, max_length=128, strict=True, repr=False)


class ProfilePayload(JsonPayload):
    display_name: Optional[str] = Field(default=None, max_length=100, strict=True)
    email: Optional[str] = Field(default=None, max_length=254, strict=True)


class DeletePayload(JsonPayload):
    password: str = Field(min_length=1, max_length=128, strict=True, repr=False)


async def _password_payload(request: Request) -> PasswordPayload:
    return await json_payload(request, PasswordPayload)


async def _profile_payload(request: Request) -> ProfilePayload:
    return await json_payload(request, ProfilePayload)


async def _delete_payload(request: Request) -> DeletePayload:
    return await json_payload(request, DeletePayload)


@router.post("/api/v1/account/password", openapi_extra=_json_request(PasswordPayload))
def change_password(request: Request, payload: PasswordPayload = Depends(_password_payload)) -> dict[str, bool]:
    user_id = auth.current_user_id(request)
    with get_connection() as conn:
        account = conn.execute("SELECT * FROM users WHERE user_id = ? FOR UPDATE", (user_id,)).fetchone()
        if not account or account.get("status") != "active" or not auth.verify_password(payload.current_password, account["password_hash"]):
            raise HTTPException(status_code=401, detail=auth._PASSWORD_INVALID_DETAIL)
        auth.validate_password(payload.new_password, account["username"])
        updated = conn.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = ?, session_version = session_version + 1 "
            "WHERE user_id = ? RETURNING session_version",
            (auth.hash_password(payload.new_password), auth._timestamp(), user_id),
        ).fetchone()
        session = dict(request.state.luma_session)
        session["session_version"] = int(updated["session_version"])
        ttl = max(1, min(int(session["expires_at"]), int(session["absolute_expires_at"])) - int(auth.time.time()))
        key = auth._session_key(session["session_id"], is_hash=True)
        if not auth._cache_call(auth.cache_set_existing_strict, key, auth.json.dumps(session, ensure_ascii=False), ttl):
            raise HTTPException(status_code=401, detail="未登录")
    # The DB generation invalidates even sessions omitted from the index or
    # issued by another worker before this transaction committed.
    auth.revoke_user_sessions(user_id, keep_session_hash=session["session_id"])
    request.state.luma_session = session
    return {"changed": True}


@router.patch("/api/v1/account/profile", openapi_extra=_json_request(ProfilePayload))
def update_profile(request: Request, payload: ProfilePayload = Depends(_profile_payload)) -> dict[str, Any]:
    user_id = auth.current_user_id(request)
    updates, values = [], []
    if "display_name" in payload.model_fields_set:
        display_name = (payload.display_name or "").strip()
        if not display_name:
            raise HTTPException(status_code=422, detail="显示名称不能为空")
        updates.append("display_name = ?")
        values.append(display_name)
    if "email" in payload.model_fields_set:
        updates.append("email = ?")
        values.append(auth.normalize_email(payload.email))
    try:
        with get_connection() as conn:
            if updates:
                conn.execute("UPDATE users SET " + ", ".join(updates) + " WHERE user_id = ?", (*values, user_id))
            account = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    except IntegrityError:
        raise HTTPException(status_code=409, detail="邮箱已被使用") from None
    if not account:
        raise HTTPException(status_code=401, detail="未登录")
    return auth.public_user(account)


@router.delete("/api/v1/account", openapi_extra=_json_request(DeletePayload))
def delete_account(request: Request, payload: DeletePayload = Depends(_delete_payload)) -> JSONResponse:
    user_id = auth.current_user_id(request)
    # Password re-entry is the second confirmation after the authenticated
    # session. Clients must also show a clear irreversible-deletion prompt.
    with get_connection() as conn:
        account = conn.execute("SELECT * FROM users WHERE user_id = ? FOR UPDATE", (user_id,)).fetchone()
        if not account or account.get("status") != "active" or not auth.verify_password(payload.password, account["password_hash"]):
            raise HTTPException(status_code=401, detail=auth._PASSWORD_INVALID_DETAIL)
        conn.execute("UPDATE users SET status = 'disabled', session_version = session_version + 1 WHERE user_id = ?", (user_id,))
    try:
        auth.revoke_user_sessions(user_id)
        from ..agent_runtime import release_all_user_runtimes
        release_all_user_runtimes(user_id)
        with get_connection() as conn:
            # Serialize with all owned writes so files created by an in-flight
            # request are either included or rejected after the tombstone.
            lock_account_data(conn, user_id)
            files = [dict(row) for row in conn.execute("SELECT * FROM files WHERE user_id = ?", (user_id,)).fetchall()]
            for record in files:
                if record.get("storage_key"):
                    storage_for_row(record).delete(str(record["storage_key"]))
            # The account migration cascades every owned business table,
            # including connector secrets and the user's runtime lease.
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    except HTTPException:
        with get_connection() as conn:
            conn.execute("UPDATE users SET status = 'active' WHERE user_id = ?", (user_id,))
        raise
    except Exception:
        # Preserve the account so erasure can be retried when storage/runtime
        # returns. Sessions stay revoked; retry requires a fresh login.
        with get_connection() as conn:
            conn.execute("UPDATE users SET status = 'active' WHERE user_id = ?", (user_id,))
        raise HTTPException(status_code=503, detail="账户数据删除暂时不可用，请重新登录后重试") from None
    response = JSONResponse(content={"deleted": True})
    response.delete_cookie(auth._SESSION_COOKIE, path="/")
    return response
