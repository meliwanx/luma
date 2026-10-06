"""OpenAI-compatible provider adapter.

The async variants use one :class:`httpx.AsyncClient` per worker process and
stream SSE data without occupying a thread for the lifetime of a generation.
Synchronous functions remain for compatibility with older callers; new
streaming code should use ``acomplete``, ``astream`` and ``astream_chat``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx

from .model_calls import UsageTotals, call_context, observe, parse_usage

logger = logging.getLogger(__name__)
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_BASE_URL = "https://api.example.com/v1"
DEFAULT_MODEL = "your-model-name"


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 45.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class ProviderUnavailable(RuntimeError):
    """The provider is not configured."""


class ProviderStreamError(RuntimeError):
    """The upstream request or stream failed."""


_async_client: httpx.AsyncClient | None = None
_async_client_lock = asyncio.Lock()
_scoped_async_client: ContextVar[Any] = ContextVar("provider_async_client", default=None)
_usage_unsupported: set[tuple[str, str]] = set()


@contextmanager
def async_client_context(client: httpx.AsyncClient) -> Iterator[httpx.AsyncClient]:
    """Use a caller-owned client on a background run's independent event loop."""
    token = _scoped_async_client.set(client)
    try:
        yield client
    finally:
        _scoped_async_client.reset(token)


def _client_closed(client: Any) -> bool:
    """Read the ``is_closed`` flag without mistaking a mock for ``True``.

    ``httpx.AsyncClient.is_closed`` is a boolean property.  Keeping the check
    strict also makes the lazy singleton friendly to test doubles whose
    ``is_closed`` attribute is an ``AsyncMock`` (``bool(AsyncMock())`` is
    truthy and would otherwise recreate the client on every call).
    """

    value = getattr(client, "is_closed", False)
    return value if isinstance(value, bool) else False


async def get_async_client() -> httpx.AsyncClient:
    """Return the process-shared async HTTP client, creating it lazily."""

    global _async_client
    scoped = _scoped_async_client.get()
    if scoped is not None:
        return scoped
    if _async_client is None or _client_closed(_async_client):
        async with _async_client_lock:
            if _async_client is None or _client_closed(_async_client):
                _async_client = httpx.AsyncClient()
    return _async_client


# Private wrapper retained for callers/tests that used the first async
# implementation.  Calling through the public helper at runtime also keeps
# monkeypatching either name useful in tests.
async def _get_async_client() -> httpx.AsyncClient:
    return await get_async_client()


async def close_async_client() -> None:
    """Close the process-shared client during application shutdown."""

    global _async_client
    client, _async_client = _async_client, None
    if client is not None and not _client_closed(client):
        close = getattr(client, "aclose", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("LLM async client close failed", exc_info=True)


# Lifespan-friendly aliases.
startup_async_client = get_async_client
shutdown_async_client = close_async_client
aclose = close_async_client


def local_mode() -> bool:
    """Whether the caller explicitly selected deterministic local mode."""

    return os.getenv("LUMA_PROVIDER", "llm").strip().lower() == "local"


def _read_dotenv(path: Path = ENV_PATH) -> None:
    """Load simple KEY=VALUE entries without overwriting real env vars."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or name in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


def get_config() -> ProviderConfig:
    """Return current provider settings, refreshing ``backend/.env`` first."""

    _read_dotenv()
    try:
        timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", os.getenv("MIMO_TIMEOUT_SECONDS", "45")))
    except ValueError:
        timeout = 45.0
    return ProviderConfig(
        base_url=os.getenv("LLM_BASE_URL", os.getenv("MIMO_API_BASE_URL", DEFAULT_BASE_URL)).strip().rstrip("/"),
        api_key=os.getenv("LLM_API_KEY", os.getenv("MIMO_API_KEY", "")).strip(),
        model=os.getenv("LLM_MODEL", os.getenv("MIMO_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL,
        timeout_seconds=max(1.0, min(timeout, 120.0)),
    )


def _endpoint(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _request_body(
    config: ProviderConfig,
    messages: Sequence[dict[str, Any]],
    *,
    stream: bool,
    temperature: float,
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": config.model,
        "messages": list(messages),
        "temperature": temperature,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
    if response_format is not None:
        body["response_format"] = response_format
    if stream and (config.base_url, config.model) not in _usage_unsupported:
        body["stream_options"] = {"include_usage": True}
    return body


def _content_from_response(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    content = message.get("content") if isinstance(message, dict) else first.get("text")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict)]
        joined = "".join(part for part in parts if isinstance(part, str)).strip()
        return joined or None
    return None


def _delta_from_payload(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    content = delta.get("content") if isinstance(delta, dict) else first.get("text")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict)]
        joined = "".join(part for part in parts if isinstance(part, str))
        return joined or None
    return None


def _raise_for_status(response: Any) -> None:
    checker = getattr(response, "raise_for_status", None)
    if callable(checker):
        checker()


def _iter_sse_payloads(response: Any) -> Iterator[dict[str, Any]]:
    """Parse SSE data across arbitrary byte fragmentation."""

    buffer = bytearray()
    read_line = getattr(response, "readline", None)
    read = read_line if callable(read_line) else lambda: response.read(4096)
    while True:
        chunk = read()
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[: newline + 1]
            if not raw_line.startswith(b"data:"):
                continue
            raw_data = raw_line[5:].lstrip()
            if raw_data == b"[DONE]":
                return
            if not raw_data:
                continue
            try:
                payload = json.loads(raw_data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProviderStreamError("invalid upstream SSE payload") from exc
            if isinstance(payload, dict):
                yield payload
    raw_line = bytes(buffer).rstrip(b"\r")
    if raw_line.startswith(b"data:"):
        raw_data = raw_line[5:].lstrip()
        if raw_data and raw_data != b"[DONE]":
            try:
                payload = json.loads(raw_data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProviderStreamError("invalid upstream SSE payload") from exc
            if isinstance(payload, dict):
                yield payload


async def _aiter_sse_payloads(response: Any) -> AsyncIterator[dict[str, Any]]:
    """Parse SSE events from an ``httpx`` async response."""

    async def parse_line(line: str) -> AsyncIterator[dict[str, Any]]:
        if not isinstance(line, str) or not line.startswith("data:"):
            return
        raw_data = line[5:].lstrip()
        if not raw_data or raw_data == "[DONE]":
            return
        try:
            payload = json.loads(raw_data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderStreamError("invalid upstream SSE payload") from exc
        if isinstance(payload, dict):
            yield payload

    aiter_lines = getattr(response, "aiter_lines", None)
    if callable(aiter_lines):
        async for line in aiter_lines():
            if isinstance(line, bytes):
                try:
                    line = line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProviderStreamError("invalid upstream SSE payload") from exc
            if isinstance(line, str) and line.startswith("data:") and line[5:].lstrip() == "[DONE]":
                return
            async for payload in parse_line(line):
                yield payload
        return
    aiter_bytes = getattr(response, "aiter_bytes", None)
    if not callable(aiter_bytes):
        raise ProviderStreamError("upstream response is not streamable")
    buffer = bytearray()
    async for chunk in aiter_bytes():
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[: newline + 1]
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProviderStreamError("invalid upstream SSE payload") from exc
            if line.startswith("data:") and line[5:].lstrip() == "[DONE]":
                return
            async for payload in parse_line(line):
                yield payload
    if buffer:
        try:
            line = bytes(buffer).rstrip(b"\r").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderStreamError("invalid upstream SSE payload") from exc
        if line.startswith("data:") and line[5:].lstrip() == "[DONE]":
            return
        async for payload in parse_line(line):
            yield payload


def _rejects_usage(status: Any, text: str = "") -> bool:
    return status == 400 or (
        isinstance(status, int) and 400 <= status < 500
        and any(name in text.lower() for name in ("stream_options", "include_usage"))
    )


@asynccontextmanager
async def _async_stream_response(config: ProviderConfig, body: dict[str, Any]) -> AsyncIterator[Any]:
    client = await _get_async_client()
    for attempt in range(2):
        async with client.stream(
            "POST", _endpoint(config.base_url), json=body,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"},
            timeout=config.timeout_seconds,
        ) as response:
            code = getattr(response, "status_code", None)
            detail = ""
            if isinstance(code, int) and code >= 400 and code != 400 and "stream_options" in body:
                read = getattr(response, "aread", None)
                if callable(read):
                    data = await read()
                    detail = data[:4096].decode("utf-8", errors="replace") if isinstance(data, bytes) else ""
            if attempt == 0 and "stream_options" in body and _rejects_usage(code, detail):
                _usage_unsupported.add((config.base_url, config.model))
                body = {key: value for key, value in body.items() if key != "stream_options"}
                continue
            _raise_for_status(response)
            yield response
            return


@contextmanager
def _sync_stream_response(config: ProviderConfig, body: dict[str, Any]) -> Iterator[Any]:
    response: Any = None
    try:
        for attempt in range(2):
            request = Request(
                _endpoint(config.base_url), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"},
                method="POST",
            )
            try:
                response = urlopen(request, timeout=config.timeout_seconds)
            except HTTPError as exc:
                detail = ""
                if exc.code != 400 and "stream_options" in body:
                    detail = exc.read(4096).decode("utf-8", errors="replace")
                if attempt == 0 and "stream_options" in body and _rejects_usage(exc.code, detail):
                    exc.close()
                    _usage_unsupported.add((config.base_url, config.model))
                    body = {key: value for key, value in body.items() if key != "stream_options"}
                    continue
                exc.close()
                raise
            yield response
            return
    finally:
        if response is not None:
            try:
                response.close()
            except (OSError, AttributeError):
                pass


def _chat_chunk(payload: dict[str, Any], calls: dict[int, dict[str, str]]) -> str | None:
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    delta = first.get("delta") if isinstance(first, dict) else {}
    if not isinstance(delta, dict):
        return None
    chunks = delta.get("tool_calls")
    for chunk in chunks if isinstance(chunks, list) else []:
        if not isinstance(chunk, dict):
            continue
        try:
            index = int(chunk.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if isinstance(chunk.get("id"), str):
            call["id"] += chunk["id"]
        fn = chunk.get("function") if isinstance(chunk.get("function"), dict) else {}
        for key in ("name", "arguments"):
            if isinstance(fn.get(key), str):
                call[key] += fn[key]
    return _delta_from_payload(payload)


async def acomplete(messages: Sequence[dict[str, str]], *, temperature: float = 0.2,
                    response_format: dict[str, Any] | None = None,
                    client: httpx.AsyncClient | None = None) -> str | None:
    """Asynchronously call the provider and return answer text."""
    config = get_config()
    response: Any = None
    with observe(config.model, messages, stream=False) as call:
        try:
            if not config.configured:
                raise ProviderUnavailable("LLM API key is not configured")
            if client is None:
                client = await _get_async_client()
            response = await client.post(
                _endpoint(config.base_url), json=_request_body(config, messages, stream=False, temperature=temperature, response_format=response_format),
                headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json", "Accept": "application/json"},
                timeout=config.timeout_seconds,
            )
            _raise_for_status(response)
            payload = response.json()
            call.accept(payload)
            return _content_from_response(payload) if isinstance(payload, dict) else None
        except ProviderUnavailable:
            raise
        except Exception as exc:
            logger.warning("LLM provider request failed: %s", type(exc).__name__)
            raise ProviderStreamError("LLM provider request failed") from exc
        finally:
            close = getattr(response, "aclose", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    pass


async def astream(messages: Sequence[dict[str, str]], *, temperature: float = 0.2) -> AsyncIterator[str]:
    """Yield provider answer deltas without database work on the stream."""
    config = get_config()
    with observe(config.model, messages, stream=True) as call:
        try:
            if not config.configured:
                raise ProviderUnavailable("LLM API key is not configured")
            body = _request_body(config, messages, stream=True, temperature=temperature)
            async with _async_stream_response(config, body) as response:
                async for payload in _aiter_sse_payloads(response):
                    call.accept(payload)
                    delta = _delta_from_payload(payload)
                    if delta:
                        yield delta
        except (ProviderUnavailable, ProviderStreamError):
            raise
        except Exception as exc:
            logger.warning("LLM stream request failed: %s", type(exc).__name__)
            raise ProviderStreamError("LLM stream request failed") from exc


async def astream_chat(messages: Sequence[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                       temperature: float = 0.2) -> AsyncIterator[dict[str, Any]]:
    """Stream text and assembled tool calls, observing usage-only chunks too."""
    config = get_config()
    calls: dict[int, dict[str, str]] = {}
    with observe(config.model, messages, stream=True, tools=tools) as call:
        try:
            if not config.configured:
                raise ProviderUnavailable("LLM API key is not configured")
            body = _request_body(config, messages, stream=True, temperature=temperature, tools=tools)
            async with _async_stream_response(config, body) as response:
                async for payload in _aiter_sse_payloads(response):
                    call.accept(payload)
                    content = _chat_chunk(payload, calls)
                    if content:
                        yield {"type": "text", "content": content}
            if calls:
                yield {"type": "tool_calls", "calls": [calls[index] for index in sorted(calls)]}
        except (ProviderUnavailable, ProviderStreamError):
            raise
        except Exception as exc:
            logger.warning("LLM tool stream request failed: %s", type(exc).__name__)
            raise ProviderStreamError("LLM stream request failed") from exc


def complete(messages: Sequence[dict[str, str]], *, temperature: float = 0.2,
             response_format: dict[str, Any] | None = None) -> str | None:
    """Synchronous compatibility wrapper for legacy and background callers."""
    config = get_config()
    with observe(config.model, messages, stream=False) as call:
        if not config.configured:
            call.status, call.error_type = "error", "ProviderUnavailable"
            return None
        body = _request_body(config, messages, stream=False, temperature=temperature, response_format=response_format)
        request = Request(
            _endpoint(config.base_url), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            call.accept(payload)
            return _content_from_response(payload) if isinstance(payload, dict) else None
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            call.failed(exc)
            logger.warning("LLM provider request failed: %s", type(exc).__name__)
            return None


def stream(messages: Sequence[dict[str, str]], *, temperature: float = 0.2) -> Iterator[str]:
    """Synchronous compatibility stream; use :func:`astream` for new code."""
    config = get_config()
    with observe(config.model, messages, stream=True) as call:
        try:
            if not config.configured:
                raise ProviderUnavailable("LLM API key is not configured")
            body = _request_body(config, messages, stream=True, temperature=temperature)
            with _sync_stream_response(config, body) as response:
                for payload in _iter_sse_payloads(response):
                    call.accept(payload)
                    delta = _delta_from_payload(payload)
                    if delta:
                        yield delta
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("LLM stream request failed: %s", type(exc).__name__)
            raise ProviderStreamError("LLM stream request failed") from exc


def stream_chat(messages: Sequence[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                temperature: float = 0.2) -> Iterator[dict[str, Any]]:
    """Synchronous compatibility tool-call stream."""
    config = get_config()
    calls: dict[int, dict[str, str]] = {}
    with observe(config.model, messages, stream=True, tools=tools) as call:
        try:
            if not config.configured:
                raise ProviderUnavailable("LLM API key is not configured")
            body = _request_body(config, messages, stream=True, temperature=temperature, tools=tools)
            with _sync_stream_response(config, body) as response:
                for payload in _iter_sse_payloads(response):
                    call.accept(payload)
                    content = _chat_chunk(payload, calls)
                    if content:
                        yield {"type": "text", "content": content}
            if calls:
                yield {"type": "tool_calls", "calls": [calls[index] for index in sorted(calls)]}
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("LLM tool stream request failed: %s", type(exc).__name__)
            raise ProviderStreamError("LLM stream request failed") from exc


def status() -> dict[str, Any]:
    """Safe, non-secret provider status for diagnostics and the UI."""

    config = get_config()
    return {
        "provider": "llm",
        "configured": config.configured,
        "base_url_configured": bool(config.base_url),
        "model": config.model,
    }
