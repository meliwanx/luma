"""Early request-body limits for the file upload endpoint.

Starlette parses multipart requests before calling the route handler.  The
route-level size check therefore cannot prevent an oversized request from
being written to the multipart temporary directory first.  This middleware
enforces a bounded request-body size at the ASGI receive boundary while
leaving every other route (including streaming responses) untouched.
"""

from __future__ import annotations

import json
import tempfile
from typing import Any, Awaitable, Callable, Dict, Optional

import anyio
from starlette.concurrency import run_in_threadpool

from .services.files import max_upload_size


Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]


class UploadSizeLimitMiddleware:
    """Limit only ``POST /api/v1/files`` before multipart parsing starts.

    ``max_upload_size()`` describes the file bytes accepted by the route.  A
    one MiB allowance covers multipart framing and form fields, so the ASGI
    request-body limit is ``max_upload_size() + 1 MiB``.  The route keeps its
    own file-byte check as a second defence once parsing has completed.
    """

    _MULTIPART_OVERHEAD = 1024 * 1024
    _REPLAY_CHUNK_SIZE = 64 * 1024

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    @staticmethod
    def _content_length(scope: Dict[str, Any]) -> Optional[int]:
        for key, value in scope.get("headers", ()):
            if key.lower() != b"content-length":
                continue
            try:
                length = int(value.strip())
            except (TypeError, ValueError):
                return None
            return length if length >= 0 else None
        return None

    @staticmethod
    async def _reject(send: Send, maximum: int) -> None:
        body = json.dumps(
            {"detail": {"code": "file_too_large", "max_bytes": maximum}},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method", "").upper() != "POST" or scope.get("path") != "/api/v1/files":
            await self.app(scope, receive, send)
            return

        maximum = max_upload_size()
        request_limit = maximum + self._MULTIPART_OVERHEAD
        content_length = self._content_length(scope)
        if content_length is not None and content_length > request_limit:
            # Do not call receive at all for a request that advertises an
            # impossible body size.  This avoids multipart parsing and disk
            # writes in Starlette's request handling path.
            await self._reject(send, maximum)
            return

        # A receive wrapper alone lets the multipart parser process earlier
        # chunks before a later chunk exceeds the limit. Stage the bounded raw
        # body first, spilling to disk above one MiB, and only enter the app
        # once the entire request has passed the size check.
        staged = tempfile.SpooledTemporaryFile(max_size=self._MULTIPART_OVERHEAD, mode="w+b")
        try:
            received = 0
            rolled_to_disk = False
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    return
                body = message.get("body", b"") or b""
                received += len(body)
                if received > request_limit:
                    await self._reject(send, maximum)
                    return
                if body:
                    if received > self._MULTIPART_OVERHEAD and not rolled_to_disk:
                        # Rollover before writing: a single large ASGI message
                        # must not first be copied into the in-memory spool.
                        await run_in_threadpool(staged.rollover)
                        rolled_to_disk = True
                    await run_in_threadpool(staged.write, body)
                if not message.get("more_body", False):
                    break

            await run_in_threadpool(staged.seek, 0)
            remaining = received
            replay_complete = False

            async def replay_receive() -> Dict[str, Any]:
                nonlocal remaining, replay_complete
                if replay_complete:
                    # BaseHTTPMiddleware may still listen for disconnects
                    # after consuming the final replayed request message.
                    return await receive()
                body = await run_in_threadpool(staged.read, self._REPLAY_CHUNK_SIZE)
                remaining -= len(body)
                replay_complete = remaining == 0
                return {"type": "http.request", "body": body, "more_body": not replay_complete}

            await self.app(scope, replay_receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(staged.close)


__all__ = ["UploadSizeLimitMiddleware"]
