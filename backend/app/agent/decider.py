"""Optional low-latency decision points.

The default implementation is deliberately local and side-effect free.  The
HTTP implementation is an opt-in optimisation for non-security decisions; it
can only return a bounded probability and always falls back to the caller's
default when anything is uncertain.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any, Dict, Optional, Set

import httpx

from ..model_calls import call_context, observe

logger = logging.getLogger(__name__)

ALLOWED_POINTS: Set[str] = {
    "needs_tools",
    "model_route",
    "memory_worth_extracting",
    "routine_notify",
}

_INSTRUCTIONS = {
    "needs_tools": "判断这一轮是否需要工具定义。仅返回 0 到 1 的概率。",
    "model_route": "判断是否应交给快模型。仅返回 0 到 1 的概率。",
    "memory_worth_extracting": "判断本轮是否值得做记忆提取。仅返回 0 到 1 的概率。",
    "routine_notify": "判断例程结果是否值得推送通知。仅返回 0 到 1 的概率。",
}


def _default_probability(default: float) -> float:
    try:
        value = float(default)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, min(1.0, value))


def _enabled_points() -> Set[str]:
    return {item.strip() for item in os.getenv("DECIDER_POINTS", "").split(",") if item.strip()}


class NullDecider:
    async def decide(self, point: str, state: str, *, default: float) -> float:
        if point not in ALLOWED_POINTS:
            raise ValueError("unknown decision point: %s" % point)
        return _default_probability(default)


class HttpDecider:
    """OpenAI-compatible ``systemone`` decision endpoint.

    A single client is shared by the process.  The client is intentionally
    created lazily so a normal deployment with no endpoint performs no network
    setup at import time.
    """

    def __init__(self, endpoint: str, api_key: str, model: str, points: Set[str]) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.points = points
        self._client: Optional[httpx.AsyncClient] = None
        self._lock: Optional[asyncio.Lock] = None

    def _state_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_client(self, timeout: float) -> httpx.AsyncClient:
        # AsyncClient construction is cheap, but sharing one keeps connection
        # pooling bounded and matches the provider's existing async usage.
        async with self._state_lock():
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=timeout)
            return self._client

    async def decide(self, point: str, state: str, *, default: float) -> float:
        if point not in ALLOWED_POINTS:
            raise ValueError("unknown decision point: %s" % point)
        fallback = _default_probability(default)
        if point not in self.points:
            return fallback
        started = time.perf_counter()
        downgraded = False
        timeout_ms = 150
        try:
            configured = int(os.getenv("DECIDER_TIMEOUT_MS", "150"))
            timeout_ms = max(1, min(150, configured))
        except (TypeError, ValueError):
            timeout_ms = 150
        timeout = timeout_ms / 1000.0
        payload: Dict[str, Any] = {
            "model": self.model,
            "state": state,
            "questions": {point: {"type": "noul", "instructions": _INSTRUCTIONS.get(point, "")}},
        }
        with call_context(purpose="decider"), observe(self.model, [payload], stream=False) as call:
            try:
                client = await self._get_client(timeout)
                # ``httpx`` enforces its own timeout in production.  The explicit
                # wait_for also bounds test doubles and custom transports that may
                # ignore the per-request timeout argument.
                response = await asyncio.wait_for(
                    client.post(
                        self.endpoint,
                        headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                        json=payload,
                        timeout=timeout,
                    ),
                    timeout=timeout,
                )
                response.raise_for_status()
                body = response.json()
                call.accept(body)
                answers = body.get("answers") if isinstance(body, dict) else None
                call._text(json.dumps(answers or {}, ensure_ascii=False))
                value = answers.get(point, {}).get("noul") if isinstance(answers, dict) else None
                if isinstance(value, bool):
                    value = float(value)
                elif isinstance(value, str):
                    value = float(value.strip())
                if not isinstance(value, (int, float)):
                    raise ValueError("invalid probability")
                probability = float(value)
                if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
                    raise ValueError("probability outside range")
                return probability
            except Exception as exc:
                call.failed(exc)
                downgraded = True
                return fallback
            finally:
                logger.info(
                    "agent decision point=%s duration_ms=%d downgraded=%s",
                    point,
                    int((time.perf_counter() - started) * 1000),
                    downgraded,
                )


_NULL = NullDecider()
_HTTP: Optional[HttpDecider] = None
_HTTP_CONFIG: Optional[tuple[str, str, str, tuple[str, ...]]] = None


def _current_decider() -> Any:
    global _HTTP, _HTTP_CONFIG
    endpoint = os.getenv("DECIDER_ENDPOINT", "").strip()
    api_key = os.getenv("DECIDER_API_KEY", "").strip()
    model = os.getenv("DECIDER_MODEL", "").strip() or "decision-model"
    points = tuple(sorted(_enabled_points() & ALLOWED_POINTS))
    if not endpoint or not api_key or not points:
        return _NULL
    config = (endpoint, api_key, model, points)
    if _HTTP is None or _HTTP_CONFIG != config:
        _HTTP = HttpDecider(endpoint, api_key, model, set(points))
        _HTTP_CONFIG = config
    return _HTTP


async def decide(point: str, state: str, *, default: float) -> float:
    """Return a bounded probability for an allow-listed optional point."""
    if point not in ALLOWED_POINTS:
        raise ValueError("unknown decision point: %s" % point)
    return await _current_decider().decide(point, state, default=default)


__all__ = ["ALLOWED_POINTS", "NullDecider", "HttpDecider", "decide"]
