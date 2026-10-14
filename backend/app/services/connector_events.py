"""Owner-bound connector results and inbox notifications.

This is an application contract, not an MCP transport extension. A trusted
adapter authenticates its connector and constructs the binding before calling
this service. Persistence is mandatory and supplied by a transactional store;
user-editable notification metadata is deliberately not an execution ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Mapping, Protocol

from .notifications import create_notification, publish_notification

SCHEMA_VERSION = 1
TASK_TRANSITIONS = {
    "queued": frozenset({"running", "awaiting_confirmation", "failed", "canceled"}),
    "running": frozenset({"awaiting_confirmation", "succeeded", "failed", "canceled", "unknown"}),
    "awaiting_confirmation": frozenset({"running", "failed", "canceled"}),
    "unknown": frozenset({"succeeded", "failed", "canceled"}),
    "succeeded": frozenset(), "failed": frozenset(), "canceled": frozenset(),
}
BUSINESS_TRANSITIONS = {
    "pending": frozenset({"in_progress", "resolved", "rejected"}),
    "in_progress": frozenset({"resolved", "rejected"}),
    "resolved": frozenset(), "rejected": frozenset(),
}
NOTIFY_STATUSES = frozenset({"awaiting_confirmation", "succeeded", "failed", "canceled", "unknown"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SECRET = re.compile(r"(?i)(?:bearer|password|secret|token|api.?key)[=:]|^sk-|-----BEGIN|[?&#@]")
_SCOPE_FIELDS = ("source", "connector_id", "connection_id", "owner_id", "tenant_id")


class ConnectorEventError(ValueError):
    """Invalid input; messages never include the rejected payload."""


class ConnectorEventForbidden(ConnectorEventError):
    """A server-owned source or owner boundary did not match."""


class ConnectorEventConflict(ConnectorEventError):
    """An event ID, version, or state would overwrite an existing outcome."""


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or _SECRET.search(value):
        raise ConnectorEventError("Expected an opaque identifier")
    return value


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ConnectorEventError("Expected a UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ConnectorEventError("Expected a UUID") from None


def _version(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ConnectorEventError("Expected a positive version")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ConnectorEventError("Expected an aware timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.utcoffset() is None:
            raise ValueError()
        return timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise ConnectorEventError("Expected an aware timestamp") from None


@dataclass(frozen=True)
class ConnectorBinding:
    """Verified connection scope, constructed by server credential resolution."""

    source: str
    connector_id: str
    connection_id: str
    owner_id: str
    tenant_id: str
    connection_version: int = 1

    def __post_init__(self) -> None:
        for field in _SCOPE_FIELDS:
            _identifier(getattr(self, field))
        _version(self.connection_version)

    def scope(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in _SCOPE_FIELDS}


@dataclass(frozen=True)
class OwnerAcknowledgement:
    """Identity from an authenticated user request, never a connector payload."""

    owner_id: str
    tenant_id: str


class ConnectorEventTransaction(Protocol):
    """A store transaction locked for this binding and task.

    ``connection`` is the notifications database connection. Receipts and task
    state must commit atomically with writes through that same connection.
    Receipt lookup is global so an event UUID cannot be reused in another
    scope. Task lookup/writes must be scoped to the transaction's binding.
    """

    connection: Any

    def get_receipt(self, event_id: str) -> dict[str, Any] | None: ...
    def put_receipt(self, event_id: str, receipt: dict[str, Any]) -> None: ...
    def get_task(self, task_id: str) -> dict[str, Any] | None: ...
    def put_task(self, task_id: str, task: dict[str, Any]) -> None: ...


class ConnectorEventStore(Protocol):
    """Durable store seam; implementations must serialize concurrent writers.

    A production implementation must lock the scoped task and enforce a unique
    global event UUID, rollback on any error, and return only after commit.
    There is intentionally no process-local or notification-metadata fallback.
    """

    def transaction(self, binding: ConnectorBinding, task_id: str) -> ContextManager[ConnectorEventTransaction]: ...


def validate_event(binding: ConnectorBinding, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a small control envelope without accepting free-text results."""
    if not isinstance(raw, Mapping):
        raise ConnectorEventError("Expected an event object")
    base = {"schema_version", "event_id", "kind", "occurred_at", "task_id", "connection_version", *_SCOPE_FIELDS}
    fields = {
        "task.state": ({"version", "status"}, {"result_ref", "error_code"}),
        "business.state": ({"business_version", "business_status", "business_ref"}, set()),
        "delivery.ack": ({"notification_id", "ack"}, set()),
    }
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in fields:
        raise ConnectorEventError("Unsupported event kind")
    required, optional = fields[kind]
    if set(raw) - (base | required | optional) or not (base | required) <= set(raw):
        raise ConnectorEventError("Unsupported or missing event fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
        raise ConnectorEventError("Unsupported event schema")
    if any(raw[field] != value for field, value in binding.scope().items()):
        raise ConnectorEventForbidden("Event does not match its authenticated binding")
    if _version(raw["connection_version"]) != binding.connection_version:
        raise ConnectorEventConflict("Connection version changed; refresh the adapter binding")
    event = {
        **binding.scope(), "schema_version": SCHEMA_VERSION,
        "connection_version": binding.connection_version, "event_id": _uuid(raw["event_id"]),
        "task_id": _uuid(raw["task_id"]), "kind": kind, "occurred_at": _timestamp(raw["occurred_at"]),
    }
    if kind == "task.state":
        state = raw["status"]
        if not isinstance(state, str) or state not in TASK_TRANSITIONS:
            raise ConnectorEventError("Unsupported task state")
        event.update(version=_version(raw["version"]), status=state)
        for field in optional:
            if field in raw:
                event[field] = _identifier(raw[field])
        if state == "succeeded" and not event.get("result_ref"):
            raise ConnectorEventError("A successful task requires a source result reference")
        if state != "failed" and "error_code" in event:
            raise ConnectorEventError("Error codes belong to failed task states")
    elif kind == "business.state":
        state = raw["business_status"]
        if not isinstance(state, str) or state not in BUSINESS_TRANSITIONS:
            raise ConnectorEventError("Unsupported business state")
        event.update(business_version=_version(raw["business_version"]), business_status=state,
                     business_ref=_identifier(raw["business_ref"]))
    else:
        if not isinstance(raw["ack"], str) or raw["ack"] not in {"delivered", "read"}:
            raise ConnectorEventError("Unsupported acknowledgement")
        event.update(notification_id=_identifier(raw["notification_id"]), ack=raw["ack"])
    return event


def _advance(task: dict[str, Any], event: dict[str, Any]) -> None:
    kind = event["kind"]
    if kind == "task.state":
        old, new = task.get("status"), event["status"]
        if event["version"] != task.get("version", 0) + 1:
            raise ConnectorEventConflict("Task version is stale or out of order")
        if (old is None and new != "queued") or (old is not None and new not in TASK_TRANSITIONS[old]):
            raise ConnectorEventConflict("Invalid task state transition")
        task.update(status=new, version=event["version"])
        for field in ("result_ref", "error_code"):
            if field in event:
                task[field] = event[field]
    elif kind == "business.state":
        old, new = task.get("business_status"), event["business_status"]
        if event["business_version"] != task.get("business_version", 0) + 1:
            raise ConnectorEventConflict("Business version is stale or out of order")
        if task.get("business_ref", event["business_ref"]) != event["business_ref"]:
            raise ConnectorEventConflict("Business entity changed within a task")
        if (old is None and new != "pending") or (old is not None and new not in BUSINESS_TRANSITIONS[old]):
            raise ConnectorEventConflict("Invalid business state transition")
        task.update(business_status=new, business_version=event["business_version"], business_ref=event["business_ref"])


class NotificationBridge:
    """Join the ledger transaction; publish only after the store has committed."""

    def create(self, tx: ConnectorEventTransaction, binding: ConnectorBinding, event: dict[str, Any]) -> dict[str, Any]:
        # Deliberately no user/model title, reason, tool arguments, source result
        # text, action URL or temporary token in the notification.
        state = event["status"]
        notification = create_notification(
            binding.owner_id, "connector_task", "Connector task update",
            "Your connector task is %s. Open your conversation to review it." % state.replace("_", " "),
            conn=tx.connection, publish=False,
        )
        metadata = {"schema_version": SCHEMA_VERSION, "event_id": event["event_id"],
                    "task_id": event["task_id"], "status": state, "version": event["version"]}
        tx.connection.execute("UPDATE notifications SET metadata_json = ? WHERE id = ? AND user_id = ?",
                              (json.dumps(metadata, sort_keys=True), notification["id"], binding.owner_id))
        notification["metadata"] = metadata
        return notification

    def acknowledge(self, tx: ConnectorEventTransaction, binding: ConnectorBinding, task: dict[str, Any],
                    event: dict[str, Any]) -> None:
        notification_id = event["notification_id"]
        if notification_id not in task.get("notifications", {}):
            raise ConnectorEventForbidden("Notification is outside this task")
        row = tx.connection.execute("SELECT id FROM notifications WHERE id = ? AND user_id = ?",
                                    (notification_id, binding.owner_id)).fetchone()
        if row is None:
            raise ConnectorEventForbidden("Notification is unavailable for this owner")
        ack = task["notifications"][notification_id]
        field = event["ack"] + "_at"
        ack[field] = min(ack[field], event["occurred_at"]) if ack.get(field) else event["occurred_at"]
        if event["ack"] == "read":
            # A read receipt never invents a provider delivery receipt.
            tx.connection.execute("UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ? AND user_id = ?",
                                  (event["occurred_at"], notification_id, binding.owner_id))


class ConnectorEventService:
    def __init__(self, store: ConnectorEventStore, *, bridge: NotificationBridge | None = None,
                 publisher: Callable[[str, dict[str, Any]], None] = publish_notification):
        self.store, self.bridge, self.publisher = store, bridge or NotificationBridge(), publisher

    def ingest(self, binding: ConnectorBinding, raw: Mapping[str, Any], *,
               read_actor: OwnerAcknowledgement | None = None) -> dict[str, Any]:
        """Commit a result exactly once, then best-effort fan out its safe inbox row.

        Read ACKs require a separately authenticated owner. Connector delivery
        ACKs require the verified connector binding supplied by its adapter.
        No returned receipt claims that best-effort publish delivered or read.
        """
        event = validate_event(binding, raw)
        if event["kind"] == "delivery.ack" and event["ack"] == "read":
            if read_actor is None or (read_actor.owner_id, read_actor.tenant_id) != (binding.owner_id, binding.tenant_id):
                raise ConnectorEventForbidden("Read acknowledgements require the authenticated owner")
        fingerprint = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        notification = None
        with self.store.transaction(binding, event["task_id"]) as tx:
            existing = tx.get_receipt(event["event_id"])
            if existing:
                if existing["scope"] != binding.scope():
                    raise ConnectorEventForbidden("Event ID belongs to a different binding")
                if existing["fingerprint"] != fingerprint:
                    raise ConnectorEventConflict("Event ID already has different content")
                return {**existing["receipt"], "duplicate": True}
            task = tx.get_task(event["task_id"])
            if task is None:
                if event["kind"] != "task.state":
                    raise ConnectorEventConflict("Task must be queued before business states or acknowledgements")
                task = {"task_id": event["task_id"], "version": 0, "notifications": {}}
            _advance(task, event)
            if event["kind"] == "task.state" and event["status"] in NOTIFY_STATUSES:
                notification = self.bridge.create(tx, binding, event)
                task["notifications"][notification["id"]] = {"delivered_at": None, "read_at": None}
            elif event["kind"] == "delivery.ack":
                self.bridge.acknowledge(tx, binding, task, event)
            tx.put_task(event["task_id"], task)
            receipt = {"event_id": event["event_id"], "task_id": event["task_id"], "kind": event["kind"],
                       "accepted": True, "duplicate": False, "task_status": task.get("status"),
                       "task_version": task["version"], "business_status": task.get("business_status"),
                       "notification_id": notification["id"] if notification else event.get("notification_id")}
            tx.put_receipt(event["event_id"], {"scope": binding.scope(), "fingerprint": fingerprint, "receipt": receipt})
        if notification is not None:
            try:
                self.publisher(binding.owner_id, notification)
            except Exception:
                # Persistence/SSE still work. No payload or provider exception
                # is logged, and no delivery success is inferred.
                pass
        return receipt

    def task(self, binding: ConnectorBinding, task_id: str) -> dict[str, Any] | None:
        """Read a task in the verified binding's owner/tenant/connector scope."""
        with self.store.transaction(binding, _uuid(task_id)) as tx:
            task = tx.get_task(_uuid(task_id))
            return json.loads(json.dumps(task)) if task else None
