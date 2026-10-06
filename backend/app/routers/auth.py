"""JSON account login, registration and device-session endpoints."""

from typing import Any, Optional, Type

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..auth import (
    current_user, list_auth_sessions, logout as revoke_auth_session, logout_all,
    password_login, register_account, registration_config, revoke_auth_session_by_id,
)
from ._common import get_connection, owner_id

router = APIRouter()


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


async def json_payload(request: Request, model: Type[BaseModel]) -> Any:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="请使用 JSON 提交信息")
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        # Default validation errors include raw input values, including
        # passwords. Expose only a fixed message for credential endpoints.
        raise HTTPException(status_code=422, detail="提交信息格式无效") from None


async def _login_payload(request: Request) -> LoginPayload:
    payload = await json_payload(request, LoginPayload)
    if not payload.login.strip():
        raise HTTPException(status_code=422, detail="提交信息格式无效")
    return payload


async def _register_payload(request: Request) -> RegisterPayload:
    return await json_payload(request, RegisterPayload)


def set_json_body(response: JSONResponse, content: Any) -> JSONResponse:
    response.body = JSONResponse(content=content).body
    response.headers["content-length"] = str(len(response.body))
    return response


def _json_request(model: Type[BaseModel]) -> dict[str, Any]:
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}


@router.get("/api/v1/auth/config")
def auth_config() -> dict[str, bool]:
    return registration_config()


@router.post("/api/v1/auth/register", openapi_extra=_json_request(RegisterPayload))
def auth_register(request: Request, payload: RegisterPayload = Depends(_register_payload)) -> JSONResponse:
    response = JSONResponse(content={})
    data = register_account(payload.username, payload.password, payload.email, payload.display_name,
                            payload.invite_code, response, request, bootstrap_token=payload.bootstrap_token)
    return set_json_body(response, data)


@router.post("/api/v1/auth/login", openapi_extra=_json_request(LoginPayload))
def auth_login(request: Request, payload: LoginPayload = Depends(_login_payload)) -> JSONResponse:
    response = JSONResponse(content={})
    return set_json_body(response, password_login(payload.login, payload.password, response, request))


@router.get("/api/v1/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@router.get("/api/v1/auth/sessions")
def auth_sessions(request: Request) -> list[dict[str, Any]]:
    return list_auth_sessions(request)


@router.delete("/api/v1/auth/sessions/{session_id}")
def auth_session_revoke(request: Request, session_id: str) -> dict[str, Any]:
    return {"revoked": bool(revoke_auth_session_by_id(request, session_id)), "id": session_id}


@router.post("/api/v1/auth/logout-all")
def auth_logout_all(request: Request) -> JSONResponse:
    response = JSONResponse(content={"authenticated": False})
    logout_all(request, response)
    return response


@router.get("/api/v1/account/devices")
def account_devices(request: Request) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT client, client_version, platform, user_agent, last_ip, first_seen_at, last_seen_at, request_count "
            "FROM client_devices WHERE user_id = ? ORDER BY last_seen_at DESC LIMIT 20", (owner_id(request),),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post("/api/v1/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    response = JSONResponse(content={"authenticated": False})
    revoke_auth_session(request, response)
    return response
