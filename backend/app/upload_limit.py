"""Early request-body limits for the file upload endpoint.

Starlette parses multipart requests before calling the route handler.  The
route-level size check therefore cannot prevent an oversized request from
being written to the multipart temporary directory first.  This middleware
enforces a bounded request-body size at the ASGI receive boundary while
leaving every other route (including streaming responses) untouched.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, Optional

from .services.files import max_upload_size


Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]


class _UploadTooLarge(BaseException):
    """Internal signal used to unwind the downstream app on body overflow."""


def _contains_upload_too_large(error: BaseException) -> bool:
    """Find the signal when AnyIO wraps it in a BaseExceptionGroup."""

    if isinstance(error, _UploadTooLarge):
        return True
    return any(
        _contains_upload_too_large(child)
        for child in getattr(error, "exceptions", ())
        if isinstance(child, BaseException)
    )


class UploadSizeLimitMiddleware:
    """Limit only ``POST /api/v1/files`` before multipart parsing starts.

    ``max_upload_size()`` describes the file bytes accepted by the route.  A
    one MiB allowance covers multipart framing and form fields, so the ASGI
    request-body limit is ``max_upload_size() + 1 MiB``.  The route keeps its
    own file-byte check as a second defence once parsing has completed.
    """

    _MULTIPART_OVERHEAD = 1024 * 1024

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

        received = 0
        response_started = False

        async def limited_receive() -> Dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") != "http.request":
                return message
            body = message.get("body", b"") or b""
            received += len(body)
            if received > request_limit:
                raise _UploadTooLarge()
            return message

        async def tracked_send(message: Dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BaseException as error:
            if not _contains_upload_too_large(error):
                raise
            if response_started:
                # A response has already begun; propagating the exception
                # causes the ASGI server to terminate the connection instead
                # of attempting a second response.
                raise
            await self._reject(send, maximum)


__all__ = ["UploadSizeLimitMiddleware"]
