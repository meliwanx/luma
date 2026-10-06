"""Secure Streamable HTTP MCP connector support."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse
from typing import Any, Iterator

import httpcore
import httpx
from cryptography.fernet import Fernet, InvalidToken

from .tool_results import tool_result_text


class MCPError(RuntimeError):
    """A safe, user-facing MCP error without upstream secrets."""


class SecretConfigError(MCPError):
    pass


def _fernet() -> Fernet:
    key = os.getenv("LUMA_SECRETS_KEY", "").strip()
    if not key:
        raise SecretConfigError("服务端未配置密钥加密（LUMA_SECRETS_KEY）")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise SecretConfigError("服务端密钥加密配置无效") from exc


def encrypt_headers(headers: dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(headers, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decrypt_headers(ciphertext: str) -> dict[str, str]:
    try:
        value = json.loads(_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
        raise MCPError("连接器密钥不可用") from exc
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise MCPError("连接器密钥不可用")
    return value


def redact_header_values(value: Any, headers: dict[str, str]) -> Any:
    """Remove credentials even if a server echoes them in its tool catalog."""
    if isinstance(value, str):
        secrets = set(headers.values())
        for header in headers.values():
            if header.lower().startswith(("bearer ", "basic ", "token ")):
                secrets.add(header.split(" ", 1)[1])
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        return {redact_header_values(key, headers): redact_header_values(item, headers) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_header_values(item, headers) for item in value]
    return value


MCP_INSTRUCTIONS_MAX_CHARS = 8000
MCP_TOOL_DESCRIPTION_MAX_CHARS = 4000
_INSTRUCTIONS_TRUNCATED = "\n[服务端使用说明已截断]"


def sanitize_instructions(value: Any, headers: dict[str, str]) -> str:
    """Bound untrusted server guidance only after removing echoed credentials."""
    if not isinstance(value, str):
        return ""
    value = redact_header_values(value, headers)
    if len(value) > MCP_INSTRUCTIONS_MAX_CHARS:
        value = value[:MCP_INSTRUCTIONS_MAX_CHARS - len(_INSTRUCTIONS_TRUNCATED)] + _INSTRUCTIONS_TRUNCATED
    return value



def header_hint(value: str) -> str:
    # No credential fragments may leave encrypted secret storage, including
    # connector metadata shown in settings or included in exports.
    return "***"


def header_hints(headers: dict[str, str]) -> dict[str, str]:
    return {key: header_hint(value) for key, value in headers.items()}


_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie"}


def validate_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict) or len(headers) > 10:
        raise MCPError("请求头格式无效")
    result: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not _HEADER_NAME.fullmatch(key) or key.lower() in _FORBIDDEN_HEADERS:
            raise MCPError("请求头名称不允许")
        if not isinstance(value, str) or len(value) > 4096 or "\r" in value or "\n" in value:
            raise MCPError("请求头值不允许")
        result[key] = value
    return result


def _blocked_hosts() -> set[str]:
    return {item.strip().rstrip(".").lower() for item in os.getenv("MCP_BLOCKED_HOSTS", "").split(",") if item.strip()}


def allowed_addresses(host: str, port: int) -> list[str]:
    """Resolve a host and return its addresses only if every one of them is allowed."""
    host = host.rstrip(".").lower()
    blocked = _blocked_hosts()
    if host in blocked:
        raise MCPError("地址不允许：该地址已被阻止")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except (OSError, ValueError):
            raise MCPError("地址不允许：无法解析主机")
    if not addresses or any(str(addr) in blocked for addr in addresses):
        raise MCPError("地址不允许：该地址已被阻止")
    if any(not addr.is_global for addr in addresses):
        raise MCPError("地址不允许：只支持公网 https 地址")
    return list(dict.fromkeys(str(addr) for addr in addresses))


def validate_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2000:
        raise MCPError("地址不允许：只支持公网 https 地址")
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.scheme.lower() != "https":
        raise MCPError("地址不允许：只支持公网 https 地址")
    if not parsed.hostname:
        raise MCPError("地址不允许：只支持公网 https 地址")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        raise MCPError("地址不允许：端口无效")
    allowed_addresses(parsed.hostname, port)
    return url


class _PinnedBackend(httpcore.SyncBackend):
    """Re-check the resolved address at connect time so DNS rebinding cannot reach private hosts."""

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                    socket_options: Any = None) -> httpcore.NetworkStream:
        last: Exception | None = None
        # TLS still verifies against the original hostname; only the TCP target is pinned.
        for address in allowed_addresses(host, port):
            try:
                return super().connect_tcp(address, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError("no address")


def _http_client() -> httpx.Client:
    transport = httpx.HTTPTransport(verify=True, trust_env=False)
    transport._pool._network_backend = _PinnedBackend()
    return httpx.Client(transport=transport, follow_redirects=False, trust_env=False)


def parse_config(config_text: str) -> list[dict[str, Any]]:
    try:
        root = json.loads(config_text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MCPError("配置不是有效的 JSON") from exc
    if not isinstance(root, dict):
        raise MCPError("配置不是有效的 MCP 配置")
    if "command" in root or "args" in root:
        raise MCPError("暂不支持本地命令型 MCP，只支持 HTTP 地址")
    if isinstance(root.get("mcpServers"), dict):
        entries = list(root["mcpServers"].items())
    elif "url" in root:
        entries = [(root.get("name") or "mcp", root)]
    else:
        entries = list(root.items())
    all_headers = {str(index): value for index, value in enumerate(
        value for _, raw in entries if isinstance(raw, dict) and isinstance(raw.get("headers"), dict)
        for value in raw["headers"].values() if isinstance(value, str)
    )}
    result = []
    for name, raw in entries:
        if not isinstance(raw, dict):
            continue
        if "command" in raw or "args" in raw:
            raise MCPError("暂不支持本地命令型 MCP，只支持 HTTP 地址")
        if raw.get("type") not in (None, "http", "streamable-http", "sse"):
            raise MCPError("暂不支持本地命令型 MCP，只支持 HTTP 地址")
        if "url" not in raw:
            continue
        url = validate_url(raw["url"])
        headers = validate_headers(raw.get("headers", {}))
        result.append({"name": redact_header_values(str(name), all_headers)[:200] or urlparse(url).hostname or "mcp", "url": url, "headers": headers})
    if not result:
        raise MCPError("配置不是有效的 MCP 配置")
    return result


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, MCPError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return "连接失败：HTTP %s，请检查令牌" % exc.response.status_code
    if isinstance(exc, httpx.HTTPError):
        return "连接失败：网络错误"
    return "这不是有效的 MCP 服务"


_MAX_RESPONSE = 2 * 1024 * 1024


class MCPClient:
    def __init__(self, endpoint: str, headers: dict[str, str]):
        self.endpoint = validate_url(endpoint)
        self.headers = validate_headers(headers)
        self.client = _http_client()
        self.session_id: str | None = None
        self.protocol_version = "2025-06-18"
        self.server_info: dict[str, Any] = {}
        self.instructions = ""
        self._request_id = 0

    def __enter__(self) -> "MCPClient":
        self.initialize()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        value = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            value["Mcp-Session-Id"] = self.session_id
            value["MCP-Protocol-Version"] = self.protocol_version
        return value

    def _post(self, method: str, params: Any = None, *, timeout: float = 15.0, notification: bool = False) -> Any:
        self._request_id += 1
        request_id = self._request_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = request_id
        if params is not None:
            body["params"] = params
        deadline = time.monotonic() + timeout
        value: Any = None
        with self.client.stream("POST", self.endpoint, headers=self._headers(), json=body, timeout=httpx.Timeout(timeout, connect=5.0)) as response:
            session_header = response.headers.get("Mcp-Session-Id")
            if session_header:
                self.session_id = session_header
            if notification:
                if response.status_code not in {200, 202, 204}:
                    response.raise_for_status()
                return None
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            received = 0
            if "text/event-stream" in content_type:
                # Stop at the matching response; servers may keep the stream open afterwards.
                for line in response.iter_lines():
                    received += len(line) + 1
                    if received > _MAX_RESPONSE or time.monotonic() > deadline:
                        raise MCPError("MCP 响应过大" if received > _MAX_RESPONSE else "连接超时")
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(payload, dict) and payload.get("id") == request_id:
                        value = payload
                        break
                else:
                    raise MCPError("这不是有效的 MCP 服务")
            else:
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE or time.monotonic() > deadline:
                        raise MCPError("MCP 响应过大" if received > _MAX_RESPONSE else "连接超时")
                    chunks.append(chunk)
                try:
                    value = json.loads(b"".join(chunks).decode("utf-8"))
                except (ValueError, UnicodeError) as exc:
                    raise MCPError("这不是有效的 MCP 服务") from exc
        if isinstance(value, dict) and value.get("error"):
            raise MCPError("MCP 工具请求失败")
        if isinstance(value, dict) and "result" in value:
            return value["result"]
        return value

    def initialize(self) -> dict[str, Any]:
        result = self._post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Luma", "version": "0.1.0"}}, timeout=15)
        # The session id is an HTTP response header and is intentionally only
        # kept in this short-lived client.
        if isinstance(result, dict):
            self.protocol_version = str(result.get("protocolVersion") or self.protocol_version)
            if isinstance(result.get("serverInfo"), dict):
                self.server_info = result["serverInfo"]
            instructions = result.get("instructions")
            self.instructions = instructions if isinstance(instructions, str) else ""
        self._post("notifications/initialized", {}, notification=True)
        return result if isinstance(result, dict) else {}

    def list_tools(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: Any = None
        server: dict[str, Any] = dict(self.server_info)
        for _ in range(5):
            params = {"cursor": cursor} if cursor else {}
            result = self._post("tools/list", params)
            if not isinstance(result, dict):
                raise MCPError("这不是有效的 MCP 服务")
            if isinstance(result.get("server"), dict):
                server = result["server"]
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise MCPError("这不是有效的 MCP 服务")
            tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        if cursor:
            raise MCPError("MCP 工具列表过长")
        return tools[:64], server

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str | dict[str, Any]:
        result = self._post("tools/call", {"name": name, "arguments": arguments}, timeout=30)
        if isinstance(result, dict) and result.get("isError"):
            # Redact before bounding text so truncation cannot expose a
            # partial credential echoed by the remote tool.
            return {"isError": True, "text": result_to_text(redact_header_values(result, self.headers))}
        return result_to_text(result)

    def close(self) -> None:
        try:
            if self.session_id:
                self.client.delete(self.endpoint, headers=self._headers(), timeout=httpx.Timeout(5.0, connect=5.0))
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass


def result_to_text(result: Any) -> str:
    if not isinstance(result, dict):
        return tool_result_text(result)
    parts: list[str] = []
    for item in result.get("content", []) if isinstance(result.get("content"), list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "audio":
            parts.append("[音频]")
        elif kind == "resource" and isinstance(item.get("resource"), dict) and isinstance(item["resource"].get("text"), str):
            parts.append(item["resource"]["text"])
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return tool_result_text("\n".join(parts))


# Names are a fallback only when the server supplies no annotations. A
# destructive name or hint always takes precedence, including over a read hint.
MCP_READ_ONLY_NAME_PREFIXES = ("get", "list", "search", "query", "describe", "validate", "my", "export")
MCP_READ_ONLY_NAME_PARTS = ("capabilities", "detail", "schema", "lineage")
MCP_WRITE_NAME_PARTS = ("add", "append", "create", "disable", "enable", "insert", "modify", "patch", "propose", "put", "reset", "save", "set", "update", "write")
MCP_CONFIRM_NAME_PARTS = ("delete", "remove", "drop", "bulk", "batch", "pay", "purchase", "order", "checkout", "transfer")


def tool_requires_confirmation(info: dict[str, Any]) -> bool:
    name = str(info.get("tool") or info.get("name") or "").lower()
    annotations = info.get("annotations") if isinstance(info.get("annotations"), dict) else {}
    return annotations.get("destructiveHint") is True or any(part in name for part in MCP_CONFIRM_NAME_PARTS)


def tool_risk(info: dict[str, Any]) -> str:
    """Classify both synchronized tools and older persisted connector metadata."""
    if tool_requires_confirmation(info):
        return "external_write"
    annotations = info.get("annotations") if isinstance(info.get("annotations"), dict) else {}
    if annotations.get("readOnlyHint") is True:
        return "read"
    if annotations or info.get("annotations_declared"):
        return "external_write"
    # Older catalogs kept only read_only, so reuse true hints and infer the
    # missing hints at read time without changing user permission rows.
    if info.get("read_only") is True:
        return "read"
    name = str(info.get("tool") or info.get("name") or "")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    parts = re.split(r"[^a-z0-9]+", name)
    if any(part in MCP_WRITE_NAME_PARTS for part in parts):
        return "external_write"
    if any(part in MCP_READ_ONLY_NAME_PREFIXES + MCP_READ_ONLY_NAME_PARTS for part in parts):
        return "read"
    return "external_write"


def normalize_tools(raw_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for raw in raw_tools[:64]:
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = raw.get("inputSchema") or {"type": "object"}
        try:
            if len(json.dumps(schema, ensure_ascii=False)) > 16 * 1024:
                continue
        except (TypeError, ValueError):
            continue
        annotations = raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {}
        title = raw.get("title") or annotations.get("title") or name
        hints = {key: annotations[key] for key in ("readOnlyHint", "destructiveHint") if isinstance(annotations.get(key), bool)}
        info = {"name": name, "title": str(title)[:200], "description": str(raw.get("description") or "")[:MCP_TOOL_DESCRIPTION_MAX_CHARS], "annotations": hints, "annotations_declared": bool(annotations), "enabled": True, "input_schema": schema}
        info["read_only"] = tool_risk(info) == "read"
        result.append(info)
    return result


@contextmanager
def session_pool(connectors: list[dict[str, Any]]) -> Iterator[dict[str, MCPClient]]:
    pool: dict[str, MCPClient] = {}
    try:
        yield pool
    finally:
        # Closing sends a DELETE per session; do it in the background so a slow
        # server cannot hold up the chat stream that is finishing.
        clients = [client for client in pool.values() if hasattr(client, "close")]
        if clients:
            threading.Thread(target=lambda: [client.close() for client in clients], daemon=True).start()


def safe_error(exc: BaseException) -> str:
    return _safe_error(exc)
