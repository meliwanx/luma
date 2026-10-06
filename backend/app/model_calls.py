"""Content-free model-call measurements shared by provider adapters."""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Optional


PURPOSES = {"chat_round", "final_answer", "summary", "voice_cleanup", "decider", "memory_extract", "other", "ideas", "proactive", "feed"}
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class UsageTotals:
    def __init__(self, initial: Optional[dict[str, Any]] = None) -> None:
        self._lock = threading.Lock()
        self._values = {key: _number((initial or {}).get(key)) or 0 for key in TOKEN_FIELDS + ("calls", "estimated_calls")}

    def add(self, row: dict[str, Any]) -> None:
        with self._lock:
            for key in TOKEN_FIELDS:
                self._values[key] += row[key]
            self._values["calls"] += 1
            self._values["estimated_calls"] += int(row["estimated"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._values)
        result["estimated"] = result["estimated_calls"] > 0
        return result


@dataclass(frozen=True)
class CallContext:
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    purpose: str = "other"
    totals: Optional[UsageTotals] = None
    deadline: Optional[float] = None


_context: ContextVar[CallContext] = ContextVar("model_call_context", default=CallContext())


@contextmanager
def call_context(*, user_id: Optional[str] = None, session_id: Optional[str] = None,
                 message_id: Optional[str] = None, purpose: Optional[str] = None,
                 totals: Optional[UsageTotals] = None, timeout_seconds: Optional[float] = None) -> Iterator[CallContext]:
    parent = _context.get()
    context = CallContext(
        user_id if user_id is not None else parent.user_id,
        session_id if session_id is not None else parent.session_id,
        message_id if message_id is not None else parent.message_id,
        purpose if purpose in PURPOSES else parent.purpose,
        totals if totals is not None else parent.totals,
        time.monotonic() + timeout_seconds if timeout_seconds is not None else parent.deadline,
    )
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    return cjk + int(math.ceil((len(text) - cjk) / 4.0))


def _number(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def parse_usage(payload: Any, prompt_estimate: int, completion_estimate: int) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt, completion = _number(usage.get("prompt_tokens")), _number(usage.get("completion_tokens"))
    estimated = prompt is None or completion is None
    prompt = prompt_estimate if prompt is None else prompt
    completion = completion_estimate if completion is None else completion
    total = _number(usage.get("total_tokens"))
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": total if total is not None and not estimated else prompt + completion,
        "cached_tokens": (_number(prompt_details.get("cached_tokens")) or 0) if isinstance(prompt_details, dict) else 0,
        "reasoning_tokens": (_number(completion_details.get("reasoning_tokens")) or 0) if isinstance(completion_details, dict) else 0,
        "estimated": estimated,
    }


class ModelCall:
    """Keep only counts while streaming; enqueue exactly once on completion."""

    def __init__(self, model: str, messages: Any, *, stream: bool, tools: Any = None) -> None:
        self.context = _context.get()
        self.model, self.stream = model, stream
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.started = time.perf_counter()
        self.first_token_ms: Optional[float] = None
        self.prompt_estimate = estimate_tokens(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))
        self.completion_cjk = self.completion_other = 0
        self.usage: dict[str, Any] = {}
        self.tool_indexes: set[Any] = set()
        self.status, self.error_type = "ok", None

    def _text(self, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    self._text(item.get("text"))
        if not isinstance(value, str) or not value:
            return
        if self.first_token_ms is None:
            self.first_token_ms = (time.perf_counter() - self.started) * 1000
        cjk = len(_CJK.findall(value))
        self.completion_cjk += cjk
        self.completion_other += len(value) - cjk

    def accept(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("usage"), dict) and payload["usage"]:
            self.usage = {"usage": payload["usage"]}
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        if not isinstance(choice, dict):
            return
        message = choice.get("delta") if self.stream else choice.get("message")
        message = message if isinstance(message, dict) else {}
        self._text(message.get("content", choice.get("text")))
        self._text(message.get("reasoning_content"))
        calls = message.get("tool_calls")
        for index, tool in enumerate(calls if isinstance(calls, list) else []):
            if not isinstance(tool, dict):
                continue
            try:
                tool_index = int(tool.get("index", index))
            except (TypeError, ValueError):
                tool_index = index
            self.tool_indexes.add(tool_index)
            function = tool.get("function")
            if isinstance(function, dict):
                self._text(function.get("name"))
                self._text(function.get("arguments"))

    def failed(self, exc: BaseException) -> None:
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        reason = getattr(cause, "reason", None)
        if isinstance(reason, BaseException):
            cause = reason
        name = type(cause).__name__
        self.status = "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else (
            "timeout" if isinstance(cause, TimeoutError) or "Timeout" in name else "error")
        self.error_type = name
        if self.status == "cancelled" and self.context.deadline is not None and time.monotonic() >= self.context.deadline:
            self.status, self.error_type = "timeout", "TimeoutError"
        response = getattr(cause, "response", None)
        code = getattr(response, "status_code", getattr(cause, "code", None))
        if isinstance(code, int):
            self.error_type = "http_%d" % code

    def finish(self) -> None:
        # Unowned diagnostic calls must never be charged to a default user.
        if not self.context.user_id:
            return
        duration = (time.perf_counter() - self.started) * 1000
        usage = parse_usage(self.usage, self.prompt_estimate,
                            self.completion_cjk + int(math.ceil(self.completion_other / 4.0)))
        output_ms = duration - (self.first_token_ms or 0) if self.stream else duration
        row = {
            "id": uuid.uuid4().hex, "user_id": self.context.user_id,
            "session_id": self.context.session_id, "message_id": self.context.message_id,
            "purpose": self.context.purpose, "model": self.model, "stream": self.stream,
            **usage, "first_token_ms": self.first_token_ms, "duration_ms": duration,
            "tokens_per_sec": usage["completion_tokens"] * 1000 / max(output_ms, 1) if usage["completion_tokens"] else None,
            "status": self.status, "error_type": self.error_type,
            "tool_calls_count": len(self.tool_indexes), "created_at": self.created_at,
        }
        if self.context.totals is not None:
            self.context.totals.add(row)
        from .telemetry import record_model_call
        record_model_call(row)


@contextmanager
def observe(model: str, messages: Any, *, stream: bool, tools: Any = None) -> Iterator[ModelCall]:
    call = ModelCall(model, messages, stream=stream, tools=tools)
    try:
        yield call
    except BaseException as exc:
        call.failed(exc)
        raise
    finally:
        call.finish()


def merged_metadata_sql(old: str = "messages.metadata_json::jsonb", new: str = "incoming.metadata") -> str:
    """Atomic monotone totals when callbacks and stream checkpoints race.

    Arguments are repository-owned SQL expressions, never request values.
    Guard numeric casts because legacy client metadata can contain any JSON.
    """
    old, new = "(" + old + ")", "(" + new + ")"
    fields = []
    for key in TOKEN_FIELDS + ("calls", "estimated_calls"):
        def number(source: str) -> str:
            value = source + "->'usage'->'" + key + "'"
            return "CASE WHEN jsonb_typeof(" + value + ") = 'number' THEN (" + value + ")::numeric ELSE 0 END"
        fields.extend(("'" + key + "'", "GREATEST(0, " + number(old) + ", " + number(new) + ")"))
    fields.extend(("'estimated'", "COALESCE(" + old + "->'usage'->'estimated' = 'true'::jsonb, FALSE) OR "
                   "COALESCE(" + new + "->'usage'->'estimated' = 'true'::jsonb, FALSE)"))
    return (
        "(CASE WHEN jsonb_exists(" + old + ", 'usage') OR jsonb_exists(" + new + ", 'usage') "
        "THEN jsonb_set(" + new + ", '{usage}', jsonb_build_object(" + ", ".join(fields) + ")) "
        "ELSE " + new + " END)::text"
    )
