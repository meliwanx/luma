"""Small Streamable HTTP MCP fixture used by backend tests."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import uvicorn


TOOLS = [
    {"name": "query_sample", "title": "查询示例记录", "description": "查询示例记录 示例用量", "inputSchema": {"type": "object", "properties": {"record_id": {"type": "string"}}, "required": ["record_id"]}, "annotations": {"readOnlyHint": True}},
    {"name": "reset_sample", "title": "重置示例记录", "description": "重置示例记录", "inputSchema": {"type": "object", "properties": {"record_id": {"type": "string"}}, "required": ["record_id"]}},
]


def make_app(counter: dict[str, int] | None = None) -> FastAPI:
    app = FastAPI()
    sessions: set[str] = set()

    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == "Bearer test-token"

    @app.post("/")
    async def rpc(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        method = body.get("method")
        request_id = body.get("id")
        if method == "initialize":
            session = "mock-" + uuid.uuid4().hex
            sessions.add(session)
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "mock", "version": "1"}}}, headers={"Mcp-Session-Id": session})
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            cursor = (body.get("params") or {}).get("cursor")
            page = TOOLS[:1] if not cursor else TOOLS[1:]
            result: dict[str, Any] = {"tools": page}
            if not cursor:
                result["nextCursor"] = "page-2"
            payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
            return StreamingResponse(iter(["event: message\ndata: " + json.dumps(payload) + "\n\n"]), media_type="text/event-stream")
        if method == "tools/call":
            params = body.get("params") or {}
            if counter is not None:
                counter[params.get("name", "")] = counter.get(params.get("name", ""), 0) + 1
            if params.get("name") == "query_sample":
                text = "record_id=" + str((params.get("arguments") or {}).get("record_id", "")) + " 示例用量 1.2GB"
            else:
                text = "已重置"
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}]}})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})

    @app.delete("/")
    async def close(request: Request):
        return Response(status_code=204)

    return app


class MockMCPServer:
    def __init__(self, counter: dict[str, int] | None = None):
        self.counter = counter if counter is not None else {}
        self.config = uvicorn.Config(make_app(self.counter), host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.time() + 5
        while not self.server.started and self.thread.is_alive() and time.time() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("uvicorn could not bind a test port")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
