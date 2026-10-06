"""Agent runtime queue, approval and activity routes."""
import asyncio
from typing import Iterable, Optional
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from .. import runtime
from ..models import RuntimeActivity, RuntimeAgentRun, RuntimeAgentRunCreate, RuntimeApproval, RuntimeApprovalCreate, RuntimeApprovalDecision, RuntimeJob, RuntimeJobCreate, RuntimeTool, RuntimeToolCallCreate, ToolPermission, ToolPermissionUpdate
from ..runtime import cancel_job, create_approval, create_job, decide_approval, get_job, list_activity, list_approvals, list_jobs, list_tools, pause_job, resume_job, tool_descriptor
from ..services.chat import sse_event
from ..agent_runtime import AgentRuntimeUnavailable, ensure_user_runtime, get_user_runtime, release_user_runtime
from ..agent_runtime import status as agent_runtime_status
from ..models import RuntimeSandbox, RuntimeSandboxCreate
from ..agent.policy import AUTO_APPROVED_WRITE_TOOLS
from ..services.permissions import permission_items, set_permission_mode
from ._common import now, owner_id

status_router = APIRouter()
router = APIRouter()


def _remember_runtime_approval(user_id: str, approval: dict[str, object]) -> None:
    """Persist ``remember`` only for a catalogued, always-allowable tool."""

    action = str(approval.get("action") or "")
    explicit = approval.get("payload")
    explicit_key = explicit.get("permission_key") if isinstance(explicit, dict) else None
    key = str(explicit_key or "").strip()
    allow_always = False
    if not key:
        try:
            from ..agent.tools import registry_for
            from ..agent.policy import permission_key_for

            registered, _ = registry_for(user_id, mode="background")
            target = None
            for item in registered:
                metadata = getattr(item, "metadata", {}) or {}
                remote = str(metadata.get("mcp_name") or metadata.get("tool") or "") if isinstance(metadata, dict) else ""
                if action in {str(getattr(item, "name", "")), remote}:
                    target = item
                    break
            if target is not None:
                key, allow_always = permission_key_for(target)
        except Exception:
            key = ""
    if key and not allow_always:
        # Built-in keys can be remembered without requiring a live registry.
        try:
            from ..services.permissions import permission_spec

            spec = permission_spec(user_id, key)
            allow_always = bool(spec and spec.get("allow_always"))
        except Exception:
            allow_always = False
    if key and allow_always:
        set_permission_mode(user_id, key, "always")



@status_router.get("/api/v1/runtime/status")
def runtime_status() -> dict[str, object]:
    return {"agent_runtime": agent_runtime_status(), "product_worker": "postgres-runtime-queue"}


@status_router.get("/api/v1/runtime/sandbox", response_model=Optional[RuntimeSandbox])
def runtime_sandbox_get(request: Request) -> Optional[RuntimeSandbox]:
    lease = get_user_runtime(owner_id(request))
    return RuntimeSandbox.model_validate(lease.to_public()) if lease else None


@status_router.post("/api/v1/runtime/sandbox", response_model=RuntimeSandbox, status_code=status.HTTP_202_ACCEPTED)
def runtime_sandbox_start(request: Request, payload: RuntimeSandboxCreate) -> RuntimeSandbox:
    try:
        lease = ensure_user_runtime(owner_id(request), payload.capabilities)
    except AgentRuntimeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RuntimeSandbox.model_validate(lease.to_public())


@status_router.delete("/api/v1/runtime/sandbox", response_model=Optional[RuntimeSandbox])
def runtime_sandbox_stop(request: Request) -> Optional[RuntimeSandbox]:
    try:
        lease = release_user_runtime(owner_id(request))
    except AgentRuntimeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RuntimeSandbox.model_validate(lease.to_public()) if lease else None


@router.get("/api/v1/permissions", response_model=dict[str, list[ToolPermission]])
def permissions_list(request: Request) -> dict[str, list[ToolPermission]]:
    """List valid long-lived permissions for the authenticated user."""

    return {"items": [ToolPermission.model_validate(item) for item in permission_items(owner_id(request))]}


@router.put("/api/v1/permissions/{key}", response_model=ToolPermission)
def permissions_update(request: Request, key: str, payload: ToolPermissionUpdate) -> ToolPermission:
    try:
        result = set_permission_mode(owner_id(request), key, payload.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Permission key not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ToolPermission.model_validate(result)

@router.post("/api/v1/runtime/jobs", response_model=RuntimeJob, status_code=202)
def runtime_job_create(request: Request, payload: RuntimeJobCreate) -> RuntimeJob:
    user_id = owner_id(request)
    try:
        requires = runtime.job_requires_approval(payload.type, payload.payload)
        job = create_job(payload.type, payload.payload, payload.run_at.isoformat() if payload.run_at else None, requires_approval=requires, user_id=user_id)
        if requires:
            create_approval(job["id"], payload.type, payload.payload, user_id); job = get_job(job["id"], user_id) or job
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RuntimeJob.model_validate(job)

@router.get("/api/v1/runtime/jobs", response_model=list[RuntimeJob])
def runtime_job_list(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[RuntimeJob]:
    return [RuntimeJob.model_validate(job) for job in list_jobs(limit, owner_id(request))]

@router.get("/api/v1/runtime/jobs/{job_id}", response_model=RuntimeJob)
def runtime_job_get(request: Request, job_id: str) -> RuntimeJob:
    job = get_job(job_id, owner_id(request))
    if job is None: raise HTTPException(status_code=404, detail="Runtime job not found")
    return RuntimeJob.model_validate(job)

def _job_action(fn, message: str, request: Request, job_id: str) -> RuntimeJob:
    job = fn(job_id, owner_id(request))
    if job is None: raise HTTPException(status_code=404, detail=message)
    return RuntimeJob.model_validate(job)

@router.post("/api/v1/runtime/jobs/{job_id}/cancel", response_model=RuntimeJob)
def runtime_job_cancel(request: Request, job_id: str) -> RuntimeJob: return _job_action(cancel_job, "Runtime job not found", request, job_id)

@router.post("/api/v1/runtime/jobs/{job_id}/pause", response_model=RuntimeJob)
def runtime_job_pause(request: Request, job_id: str) -> RuntimeJob: return _job_action(pause_job, "Runtime job not found or is not queued", request, job_id)

@router.post("/api/v1/runtime/jobs/{job_id}/resume", response_model=RuntimeJob)
def runtime_job_resume(request: Request, job_id: str) -> RuntimeJob: return _job_action(resume_job, "Runtime job not found or is not paused", request, job_id)

@router.post("/api/v1/runtime/approvals", response_model=RuntimeApproval, status_code=201)
def runtime_approval_create(request: Request, payload: RuntimeApprovalCreate) -> RuntimeApproval:
    try: approval = create_approval(payload.job_id, payload.action, payload.payload, owner_id(request))
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RuntimeApproval.model_validate(approval)

@router.get("/api/v1/runtime/approvals", response_model=list[RuntimeApproval])
def runtime_approval_list(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[RuntimeApproval]: return [RuntimeApproval.model_validate(item) for item in list_approvals(limit, owner_id(request))]

@router.post("/api/v1/runtime/approvals/{approval_id}/decision", response_model=RuntimeApproval)
def runtime_approval_decide(request: Request, approval_id: str, payload: RuntimeApprovalDecision) -> RuntimeApproval:
    approval = decide_approval(approval_id, "approved" if payload.decision == "approve" else "rejected", payload.note, owner_id(request))
    if approval is None: raise HTTPException(status_code=404, detail="Runtime approval not found")
    if payload.decision == "approve" and bool(getattr(payload, "remember", False)):
        try:
            _remember_runtime_approval(owner_id(request), approval)
        except (KeyError, PermissionError, ValueError):
            pass
    return RuntimeApproval.model_validate(approval)

@router.get("/api/v1/runtime/activity", response_model=list[RuntimeActivity])
def runtime_activity_list(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> list[RuntimeActivity]: return [RuntimeActivity.model_validate(item) for item in list_activity(limit, owner_id(request))]

@router.get("/api/v1/runtime/tools", response_model=list[RuntimeTool])
def runtime_tool_list(request: Request) -> list[RuntimeTool]:
    try:
        from ..agent.tools import registry_for

        registered, _ = registry_for(owner_id(request), mode="background")
        result = []
        for item in registered:
            risk = str(getattr(item, "risk", "read"))
            result.append(
                RuntimeTool(
                    name=str(getattr(item, "name", "")),
                    description=str(getattr(item, "description", "")),
                    kind="read" if risk == "read" else "write",
                    requires_approval=risk == "external_write" or risk == "write" and str(getattr(item, "name", "")) not in AUTO_APPROVED_WRITE_TOOLS,
                    enabled=True,
                    safety="统一 Agent 策略层控制",
                )
            )
        return result
    except Exception:
        return [RuntimeTool.model_validate(item) for item in list_tools()]

@router.post("/api/v1/runtime/tools/{tool_name}", response_model=RuntimeJob, status_code=202)
def runtime_tool_call(request: Request, tool_name: str, payload: RuntimeToolCallCreate) -> RuntimeJob:
    descriptor = tool_descriptor(tool_name); user_id = owner_id(request)
    if descriptor is None: raise HTTPException(status_code=404, detail="Runtime tool not found")
    if not descriptor["enabled"]: raise HTTPException(status_code=501, detail=descriptor["safety"])
    try:
        requires = runtime.job_requires_approval(tool_name, payload.payload)
        job = create_job(tool_name, payload.payload, payload.run_at.isoformat() if payload.run_at else None, requires_approval=requires, user_id=user_id)
        if requires: create_approval(job["id"], tool_name, payload.payload, user_id); job = get_job(job["id"], user_id) or job
    except (ValueError, RuntimeError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RuntimeJob.model_validate(job)

@router.post("/api/v1/runtime/agent-runs", response_model=RuntimeAgentRun, status_code=202)
def runtime_agent_run(request: Request, payload: RuntimeAgentRunCreate) -> RuntimeAgentRun:
    user_id = owner_id(request)
    allowed = [runtime.LEGACY_AGENT_TOOL_ALIASES.get(item, item) for item in payload.allowed_tools]
    if not allowed:
        # Keep the queue API usable while the unified registry is optional in
        # rolling deployments.  The loop still intersects this list with its
        # server-side registry before executing any call.
        try:
            from ..agent.tools import registry_for

            available = registry_for(user_id, mode="background")
            candidates = available[0] if isinstance(available, tuple) else available
            if isinstance(candidates, dict):
                candidates = list(candidates.values())
            allowed = [getattr(item, "name", item.get("name") if isinstance(item, dict) else "") for item in candidates or [] if getattr(item, "risk", item.get("risk") if isinstance(item, dict) else "") == "read"]
            allowed = [name for name in allowed if name]
        except Exception:
            allowed = []
        if not allowed:
            allowed = ["briefing", "list_tasks", "list_memories", "create_task", "create_memory", "files"]
    try:
        from ..agent.tools import registry_for

        available = registry_for(user_id, mode="background")
        candidates = available[0] if isinstance(available, tuple) else available
        items = candidates.values() if isinstance(candidates, dict) else (candidates or [])
        names = {getattr(item, "name", item.get("name") if isinstance(item, dict) else "") for item in items}
        names.discard("")
    except Exception:
        names = set()
    unknown = [name for name in allowed if names and name not in names and tool_descriptor(name) is None]
    if unknown: raise HTTPException(status_code=422, detail="Unknown runtime tool: %s" % unknown[0])
    disabled = [name for name in allowed if tool_descriptor(name) is not None and not tool_descriptor(name)["enabled"]]
    if disabled: raise HTTPException(status_code=501, detail=tool_descriptor(disabled[0])["safety"])
    job_payload = {
        "prompt": payload.prompt,
        "allowed_tools": allowed,
        "engine": payload.engine,
        "allow_code": payload.allow_code,
    }
    if payload.session_id:
        job_payload["session_id"] = payload.session_id
    requires = runtime.job_requires_approval("agent_run", job_payload)
    try: job = create_job("agent_run", job_payload, payload.run_at.isoformat() if payload.run_at else None, requires_approval=requires, user_id=user_id)
    except (ValueError, RuntimeError) as exc: raise HTTPException(status_code=422, detail=str(exc)) from exc
    if requires: create_approval(job["id"], "agent_run", job_payload, user_id); job = get_job(job["id"], user_id) or job
    return RuntimeAgentRun(job_id=job["id"], status=job["status"], prompt=payload.prompt, allowed_tools=allowed, engine=payload.engine, allow_code=payload.allow_code, session_id=payload.session_id)

@router.get("/api/v1/runtime/events")
async def runtime_events(request: Request, after: Optional[str] = Query(default=None, max_length=80)) -> StreamingResponse:
    # Resolve request ownership outside the event loop.  ``owner_id`` may
    # consult the auth/session cache and must not make a synchronous storage
    # call on an async SSE request.
    user_id = await asyncio.to_thread(owner_id, request)
    initial = after or request.headers.get("last-event-id") or now()
    async def events() -> Iterable[str]:
        cursor = initial; seen: set[str] = set(); yield sse_event("ready", {"after": cursor})
        while True:
            if await request.is_disconnected(): break
            rows = await asyncio.to_thread(list_activity, limit=100, user_id=user_id)
            rows.reverse(); emitted = False
            for item in rows:
                created, item_id = str(item.get("created_at", "")), str(item.get("id", ""))
                if created < cursor or (created == cursor and item_id in seen): continue
                if created == cursor: seen.add(item_id)
                else: cursor, seen = created, {item_id}
                emitted = True; yield sse_event("activity", item)
            if not emitted: yield ": keep-alive\n\n"
            try: await asyncio.wait_for(asyncio.sleep(1.0), timeout=2.0)
            except asyncio.CancelledError: break
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
