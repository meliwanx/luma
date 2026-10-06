"""Keep pasted JSON secrets encrypted without interpreting connection intent."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import mcp
from ..db import get_connection


_REFERENCE = re.compile(r"\{\{secret:(sec_[A-Za-z0-9_-]{16})\}\}")
_SECRET_KEYS = {"headers", "env", "authorization", "token", "apikey", "password", "secret", "accesstoken", "accesskey", "clientsecret"}
_KEY = re.compile(r'"(?:[^"\\]|\\.)*"\s*:', re.S)
_DECODER = json.JSONDecoder()
_EXPIRED = "令牌引用已失效，请让用户重新粘贴配置"


def _key_name(value: str) -> str:
    return value.lower().replace("_", "").replace("-", "")


def _object_spans(content: str) -> list[tuple[int, int]]:
    """Find balanced objects, respecting JSON strings and escaped quotes."""
    spans = []
    start, depth = -1, 0
    quoted = escaped = False
    for index, char in enumerate(content):
        if start < 0:
            if char == "{":
                start, depth = index, 1
                quoted = escaped = False
            continue
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    spans.append((start, index + 1))
                    start = -1
    if start >= 0:
        spans.append((start, len(content)))
    return spans


def _sensitive_spans(raw: str, strings: Optional[list[tuple[int, int, str]]] = None) -> list[tuple[int, int, Any]]:
    """Locate value tokens so whitespace and all other JSON stay untouched."""
    found = []

    def whitespace(index: int) -> int:
        while index < len(raw) and raw[index].isspace():
            index += 1
        return index

    def walk(index: int, private: bool = False) -> int:
        index = whitespace(index)
        value, end = _DECODER.raw_decode(raw, index)
        if isinstance(value, dict):
            cursor = whitespace(index + 1)
            while raw[cursor] != "}":
                key, key_end = _DECODER.raw_decode(raw, cursor)
                cursor = whitespace(whitespace(key_end) + 1)
                child, child_end = _DECODER.raw_decode(raw, cursor)
                name = _key_name(key)
                if private or name in _SECRET_KEYS:
                    if name in {"headers", "env"} and isinstance(child, dict) and not private:
                        walk(cursor, True)
                    else:
                        found.append((cursor, child_end, child))
                else:
                    walk(cursor)
                cursor = whitespace(child_end)
                if raw[cursor] == ",":
                    cursor = whitespace(cursor + 1)
        elif isinstance(value, list):
            cursor = whitespace(index + 1)
            while raw[cursor] != "]":
                cursor = whitespace(walk(cursor))
                if raw[cursor] == ",":
                    cursor = whitespace(cursor + 1)
        elif isinstance(value, str) and strings is not None:
            strings.append((index, end, value))
        return end

    walk(0)
    return found


def protect_message(
    content: str, user_id: str, session_id: str, metadata: Optional[dict[str, Any]] = None,
) -> str:
    """Vault sensitive JSON values before storage, returning only references."""
    replacements = []
    rows = []
    redaction_values = {}
    timestamp = datetime.now(timezone.utc)
    expires_at = timestamp + timedelta(days=7)

    def collect(value: Any) -> None:
        if isinstance(value, str):
            if value and not _REFERENCE.fullmatch(value):
                redaction_values[str(len(redaction_values))] = value
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for start, end in _object_spans(content):
        raw = content[start:end]
        try:
            json.loads(raw)
            spans = _sensitive_spans(raw)
        except (ValueError, TypeError, RecursionError):
            # A truncated secret-bearing config cannot safely be passed to a
            # model. Never echo parse errors, which can include source text.
            has_secret = False
            for match in _KEY.finditer(raw):
                try:
                    has_secret = has_secret or _key_name(json.loads(match.group().rsplit(":", 1)[0])) in _SECRET_KEYS
                except (ValueError, TypeError):
                    continue
            if has_secret:
                replacements.append((start, end, "[配置 JSON 不完整，秘密已隐藏，请重新粘贴完整配置]"))
            continue
        for local_start, local_end, value in spans:
            if isinstance(value, str) and _REFERENCE.fullmatch(value):
                continue
            secret_id = "sec_" + secrets.token_hex(8)
            # Reuse MCP's Fernet key and strict string-map encryption format.
            ciphertext = mcp.encrypt_headers({"value": json.dumps(value, ensure_ascii=False)})
            rows.append((secret_id, user_id, session_id, ciphertext, timestamp, expires_at))
            replacements.append((start + local_start, start + local_end, json.dumps("{{secret:" + secret_id + "}}")))
            collect(value)
    if not replacements:
        return content
    safe = content
    for start, end, replacement in reversed(sorted(replacements)):
        safe = safe[:start] + replacement + safe[end:]
    # Remove echoes without corrupting JSON escapes or the new references.
    # Very short values must match a complete string, otherwise a password
    # such as "a" would erase JSON keys, URLs and references indiscriminately.
    def redact_text(text: str, prose: bool = False) -> str:
        if _REFERENCE.fullmatch(text):
            return text
        long_values = {key: value for key, value in redaction_values.items() if len(value) >= 6}
        if text in redaction_values.values():
            return "***"
        redacted = mcp.redact_header_values(text, long_values)
        if prose:
            for secret in sorted(set(redaction_values.values()), key=len, reverse=True):
                if len(secret) < 6:
                    redacted = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(secret) + r"(?![A-Za-z0-9_])", "***", redacted)
        return redacted

    safe_parts = []
    cursor = 0
    for start, end in _object_spans(safe):
        raw = safe[start:end]
        safe_parts.append(redact_text(safe[cursor:start], prose=True))
        strings: list[tuple[int, int, str]] = []
        try:
            _sensitive_spans(raw, strings)
        except (ValueError, TypeError, RecursionError, IndexError):
            strings = []
        # JSON keys remain unchanged even when a short credential happens to
        # equal a field name. Only decoded value tokens are eligible.
        for local_start, local_end, original in reversed(strings):
            redacted = redact_text(original, prose=not re.match(r"^https?://", original, re.I))
            if redacted != original:
                raw = raw[:local_start] + json.dumps(redacted, ensure_ascii=False) + raw[local_end:]
        safe_parts.append(raw)
        cursor = end
    safe_parts.append(redact_text(safe[cursor:], prose=True))
    safe = "".join(safe_parts)
    if metadata is not None:
        files = metadata.get("files", [])
        metadata.clear()
        if isinstance(files, list):
            metadata["files"] = [
                {"id": item["id"]} for item in files
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and re.fullmatch(r"file_[A-Za-z0-9_-]+", item["id"])
                and mcp.redact_header_values(item["id"], redaction_values) == item["id"]
            ]
    if rows:
        with get_connection() as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO chat_secrets(id,user_id,session_id,ciphertext,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                    row,
                )
    return safe


def resolve_secrets(value: Any, user_id: str) -> Any:
    """Resolve owner-scoped, unexpired references only inside the server."""
    cache = {}

    def lookup(secret_id: str) -> Any:
        if secret_id not in cache:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT ciphertext FROM chat_secrets WHERE id = ? AND user_id = ? AND expires_at > CURRENT_TIMESTAMP",
                    (secret_id, user_id),
                ).fetchone()
            if row is None:
                raise mcp.MCPError(_EXPIRED)
            try:
                cache[secret_id] = json.loads(mcp.decrypt_headers(row["ciphertext"])["value"])
            except (mcp.MCPError, KeyError, ValueError, TypeError):
                raise mcp.MCPError(_EXPIRED) from None
        return cache[secret_id]

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            match = _REFERENCE.fullmatch(item)
            if match:
                return lookup(match.group(1))
            if _REFERENCE.search(item):
                try:
                    parsed = json.loads(item)
                except (ValueError, TypeError):
                    return _REFERENCE.sub(lambda found: str(lookup(found.group(1))), item)
                return json.dumps(walk(parsed), ensure_ascii=False)
        elif isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        elif isinstance(item, list):
            return [walk(child) for child in item]
        return item

    return walk(value)


def cleanup_expired_secrets() -> int:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM chat_secrets WHERE expires_at <= CURRENT_TIMESTAMP")
        return cursor.rowcount
