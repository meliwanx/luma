"""Pydantic request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


Role = Literal["system", "user", "assistant", "tool"]
MessageStatus = Literal["streaming", "complete", "incomplete", "error"]
TaskStatus = Literal["todo", "in_progress", "done", "cancelled"]
RuntimeJobType = Literal["create_task", "create_memory", "briefing", "agent_run", "files", "shell", "sandbox_job"]
RuntimeEngine = Literal["auto"]
RuntimeJobStatus = Literal["queued", "running", "waiting_approval", "paused", "succeeded", "failed", "cancelled"]
ApprovalStatus = Literal["pending", "approved", "rejected", "expired", "consumed"]
GoalStatus = Literal["active", "completed", "paused", "cancelled", "archived"]
ArtifactStatus = Literal["draft", "ready", "archived"]
NotificationLevel = Literal["info", "success", "warning", "error"]
PermissionMode = Literal["ask", "always"]
PermissionCategory = Literal["mcp", "luma", "sandbox", "browser"]


class SessionCreate(BaseModel):
    title: str = Field(default="新旁聊", min_length=1, max_length=200)
    kind: Literal["side"] = "side"


class SessionUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Session(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    kind: Literal["main", "side"]
    created_at: datetime
    updated_at: datetime
    last_message_preview: Optional[str] = None
    last_message_at: Optional[datetime] = None
    message_count: int = 0


class MessageCreate(BaseModel):
    role: Role = "user"
    content: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    id: str
    session_id: str
    role: Role
    content: str
    status: MessageStatus = "complete"
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    category: str = Field(default="general", min_length=1, max_length=80)
    importance: int = Field(default=3, ge=1, le=5)
    pinned: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=20_000)
    category: Optional[str] = Field(default=None, min_length=1, max_length=80)
    importance: Optional[int] = Field(default=None, ge=1, le=5)
    pinned: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class MemoryConfirm(BaseModel):
    kind: Optional[Literal["fact", "preference"]] = None
    category: Optional[Literal["fact", "preference"]] = None


class MemorySettings(BaseModel):
    auto_extract: bool


class Memory(BaseModel):
    id: str
    content: str
    category: str
    importance: int
    pinned: bool = False
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    status: TaskStatus = "todo"
    due_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = Field(default=None, max_length=20_000)
    status: Optional[TaskStatus] = None
    due_at: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None


class Task(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus
    due_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutineCreate(BaseModel):
    """A scheduled, user-owned assistant routine."""

    title: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1, max_length=20_000)
    schedule: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    enabled: bool = True


class RoutineUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=20_000)
    schedule: Optional[str] = Field(default=None, min_length=1, max_length=100)
    timezone: Optional[str] = Field(default=None, min_length=1, max_length=100)
    enabled: Optional[bool] = None


class Routine(BaseModel):
    id: str
    title: str
    prompt: str
    schedule: str
    timezone: str
    enabled: bool
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)
    status: GoalStatus = "active"
    progress: int = Field(default=0, ge=0, le=100)
    due_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GoalUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    description: Optional[str] = Field(default=None, max_length=20_000)
    status: Optional[GoalStatus] = None
    progress: Optional[int] = Field(default=None, ge=0, le=100)
    due_at: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None


class Goal(BaseModel):
    id: str
    title: str
    description: str
    status: GoalStatus
    progress: int
    due_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    kind: str = Field(default="document", min_length=1, max_length=80)
    content: str = Field(default="", max_length=1_000_000)
    uri: Optional[str] = Field(default=None, max_length=2_000)
    status: ArtifactStatus = "draft"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    kind: Optional[str] = Field(default=None, min_length=1, max_length=80)
    content: Optional[str] = Field(default=None, max_length=1_000_000)
    uri: Optional[str] = Field(default=None, max_length=2_000)
    status: Optional[ArtifactStatus] = None
    metadata: Optional[dict[str, Any]] = None


class Artifact(BaseModel):
    id: str
    title: str
    kind: str
    content: str
    uri: Optional[str]
    status: ArtifactStatus
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoredFile(BaseModel):
    """A user-owned uploaded file stored behind the assistant API.

    ``storage_key`` and the host filesystem path are intentionally omitted
    from this public model.  Clients receive a stable file id and use the
    authenticated content endpoint instead of learning server paths.
    """

    id: str
    session_id: Optional[str] = None
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    updated_at: datetime
    download_url: str


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=80)
    endpoint: Optional[str] = Field(default=None, max_length=2_000)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    kind: Optional[str] = Field(default=None, min_length=1, max_length=80)
    endpoint: Optional[str] = Field(default=None, max_length=2_000)
    capabilities: Optional[list[str]] = Field(default=None, max_length=100)
    config: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None


class Connector(BaseModel):
    id: str
    name: str
    kind: str
    endpoint: Optional[str]
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotificationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20_000)
    kind: str = Field(default="info", min_length=1, max_length=80)
    level: NotificationLevel = "info"
    action_url: Optional[str] = Field(default=None, max_length=2_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NotificationUpdate(BaseModel):
    read: Optional[bool] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    body: Optional[str] = Field(default=None, max_length=20_000)
    metadata: Optional[dict[str, Any]] = None


class Notification(BaseModel):
    id: str
    title: str
    body: str
    kind: str
    level: NotificationLevel
    action_url: Optional[str]
    read_at: Optional[datetime]
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class PushDeviceCreate(BaseModel):
    """A platform push token registered for the authenticated user."""

    platform: str = Field(min_length=1, max_length=32)
    token: str = Field(min_length=1, max_length=4096)


class PushDevice(BaseModel):
    id: str
    platform: str
    token_suffix: str
    created_at: datetime
    updated_at: datetime


class RuntimeJobCreate(BaseModel):
    """A durable, server-executed job from a trusted built-in runtime action."""

    type: RuntimeJobType
    payload: dict[str, Any] = Field(default_factory=dict)
    run_at: Optional[datetime] = None


class RuntimeAgentRunCreate(BaseModel):
    """Start a durable, provider-independent assistant run.

    The runtime accepts a prompt and an explicit allow-list of built-in tools.
    File reads are bounded to the current user's uploads; shell execution is
    only available through the approval-gated per-user sandbox. Browser and
    network tools remain disabled.
    """

    prompt: str = Field(min_length=1, max_length=20_000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=16)
    engine: RuntimeEngine = "auto"
    # Background code execution is only accepted when this flag is carried in
    # a separately approved job payload; policy still enforces the approval.
    allow_code: bool = False
    # Durable business session id used by the shared agent loop.
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    run_at: Optional[datetime] = None


class RuntimeToolCallCreate(BaseModel):
    """Queue one registered runtime tool; arbitrary tool names are rejected."""

    payload: dict[str, Any] = Field(default_factory=dict)
    run_at: Optional[datetime] = None


class RuntimeTool(BaseModel):
    name: str
    description: str
    kind: Literal["read", "write", "disabled"]
    requires_approval: bool = False
    enabled: bool = True
    safety: str


class RuntimeSandboxCreate(BaseModel):
    """Request a short-lived per-user Agent Runtime sandbox lease."""

    # A Tencent sandbox instance is created from one tool template. Start a
    # second lease explicitly when a request needs both code and browser.
    capabilities: list[Literal["code", "browser"]] = Field(default_factory=lambda: ["code"], max_length=1)


class RuntimeSandbox(BaseModel):
    id: str
    provider: str
    provider_runtime_id: str
    status: str
    capabilities: list[str] = Field(default_factory=list)
    endpoint: Optional[str] = None
    started_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeAgentRun(BaseModel):
    job_id: str
    status: RuntimeJobStatus
    prompt: str
    allowed_tools: list[str] = Field(default_factory=list)
    engine: RuntimeEngine = "auto"
    allow_code: bool = False
    session_id: Optional[str] = None


class RuntimeJob(BaseModel):
    id: str
    type: str
    status: RuntimeJobStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    attempts: int = 0
    run_at: datetime
    created_at: datetime
    updated_at: datetime


class RuntimeApproval(BaseModel):
    id: str
    job_id: str
    action: str
    status: ApprovalStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    decision_note: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None


class RuntimeApprovalCreate(BaseModel):
    job_id: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeApprovalDecision(BaseModel):
    """A user decision for a pending runtime action."""

    decision: Literal["approve", "reject"]
    note: Optional[str] = Field(default=None, max_length=500)
    remember: bool = False


class ToolPermission(BaseModel):
    """One user-scoped long-lived tool permission."""

    key: str
    label: str
    description: str
    category: PermissionCategory
    mode: PermissionMode = "ask"
    allow_always: bool = True


class ToolPermissionUpdate(BaseModel):
    mode: PermissionMode


class RuntimeActivity(BaseModel):
    id: str
    kind: str
    title: str
    detail: str
    job_id: Optional[str] = None
    approval_id: Optional[str] = None
    created_at: datetime
