"""Tenant-scoped resource library and bounded, structured file previews."""

from __future__ import annotations

import base64
import binascii
import codecs
import csv
import io
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..auth import current_user_id
from ..db import get_connection
from ..storage import StorageUnavailable, storage_for_row
from ._common import now


router = APIRouter()
LIBRARY_TYPES = ("document", "sheet", "web", "image", "code", "archive", "other")
TEXT_PREVIEW_BYTES = 2 * 1024 * 1024
MARKDOWN_PREVIEW_BYTES = 200 * 1024
CSV_MAX_ROWS = 200
CSV_MAX_COLUMNS = 50

# Use the same rules for returned items, filters, and grouped counts. Suffixes
# take precedence over generic MIME types (a Python file may be text/plain).
TYPE_EXTENSIONS = {
    "document": (".md", ".markdown", ".txt", ".pdf", ".doc", ".docx"),
    "sheet": (".csv", ".xlsx", ".xls"),
    "web": (".html", ".htm"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp"),
    "code": (".py", ".js", ".ts", ".json", ".sql", ".sh"),
    "archive": (".zip", ".tar.gz", ".tgz"),
}
TYPE_MEDIA_TYPES = {
    "document": ("text/plain", "text/markdown", "application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "sheet": ("text/csv", "application/csv", "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "web": ("text/html", "application/xhtml+xml"),
    "code": ("application/json", "text/javascript", "application/javascript", "application/typescript", "text/typescript", "text/x-python", "application/x-python", "application/sql", "text/x-sql", "application/x-sh", "text/x-shellscript"),
    "archive": ("application/zip", "application/x-zip-compressed", "application/gzip", "application/x-gzip", "application/x-tar"),
}


def infer_type(filename: str, media_type: str) -> str:
    name = (filename or "").lower()
    for kind, suffixes in TYPE_EXTENSIONS.items():
        if name.endswith(suffixes):
            return kind
    media = (media_type or "").split(";", 1)[0].strip().lower()
    if media.startswith("image/"):
        return "image"
    for kind, values in TYPE_MEDIA_TYPES.items():
        if media in values:
            return kind
    return "other"


def _type_expression() -> str:
    clauses = []
    for kind, suffixes in TYPE_EXTENSIONS.items():
        conditions = ["RIGHT(LOWER(f.filename), {}) = '{}'".format(len(suffix), suffix) for suffix in suffixes]
        clauses.append("WHEN ({}) THEN '{}'".format(" OR ".join(conditions), kind))
    media = "LOWER(TRIM(SPLIT_PART(f.media_type, ';', 1)))"
    clauses.append("WHEN LEFT({}, 6) = 'image/' THEN 'image'".format(media))
    for kind, values in TYPE_MEDIA_TYPES.items():
        options = ",".join("'{}'".format(value) for value in values)
        clauses.append("WHEN {} IN ({}) THEN '{}'".format(media, options, kind))
    return "CASE {} ELSE 'other' END".format(" ".join(clauses))


_TYPE_SQL = _type_expression()
_TITLE_SQL = "COALESCE(NULLIF(f.title, ''), f.filename)"
_JOIN_SQL = " LEFT JOIN sessions AS s ON s.id = f.session_id AND s.user_id = f.user_id"
_SELECT_SQL = "SELECT f.*, s.title AS session_title FROM files AS f" + _JOIN_SQL
_SORT_SQL = {
    "recent": "CAST(COALESCE(f.last_opened_at, f.created_at) AS TIMESTAMPTZ)",
    "created": "CAST(f.created_at AS TIMESTAMPTZ)",
    "title": "LOWER({})".format(_TITLE_SQL),
}


def _owner_id(request: Request) -> str:
    user_id = current_user_id(request, required=True)
    request.state.luma_user_id = user_id
    return user_id


def library_item(row: Any) -> dict[str, Any]:
    record = dict(row)
    return {
        "id": record["id"],
        "title": record.get("title") or record["filename"],
        "filename": record["filename"],
        "media_type": record["media_type"],
        "type": infer_type(record["filename"], record["media_type"]),
        "size_bytes": int(record["size_bytes"]),
        "origin": record.get("origin") or "upload",
        "session_id": record.get("session_id"),
        "session_title": record.get("session_title"),
        "message_id": record.get("message_id"),
        "created_at": record["created_at"],
        "last_opened_at": record.get("last_opened_at"),
        "pinned": bool(record.get("pinned", False)),
    }


def _decode_cursor(cursor: str, sort: str, kind: str, query: str) -> dict[str, str]:
    try:
        decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data = json.loads(decoded.decode("utf-8"))
        if not isinstance(data, dict) or data.get("sort") != sort or data.get("type") != kind or data.get("q") != query:
            raise ValueError("cursor scope mismatch")
        if not isinstance(data.get("key"), str) or not isinstance(data.get("id"), str) or not data["id"]:
            raise ValueError("invalid cursor key")
        return data
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="分页游标无效") from exc


def _encode_cursor(row: Any, sort: str, kind: str, query: str) -> str:
    data = {"sort": sort, "type": kind, "q": query, "key": str(row["sort_key"]), "id": row["id"]}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@router.get("/api/v1/library")
def list_library(
    request: Request,
    type: str = Query(default="all", pattern="^(all|document|sheet|web|image|code|archive|other)$"),
    q: str = Query(default="", max_length=200),
    sort: str = Query(default="recent", pattern="^(recent|created|title)$"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None, max_length=4096),
) -> dict[str, Any]:
    user_id = _owner_id(request)
    query = q.strip()
    conditions = "f.user_id = ? AND f.deleted_at IS NULL"
    params: list[Any] = [user_id]
    if query:
        # POSITION is a literal substring search: user-entered wildcards never
        # become LIKE operators or literal percent signs in psycopg2 SQL.
        conditions += " AND (POSITION(LOWER(?) IN LOWER({})) > 0 OR POSITION(LOWER(?) IN LOWER(f.filename)) > 0)".format(_TITLE_SQL)
        params.extend([query, query])
    item_conditions = conditions
    item_params = list(params)
    if type != "all":
        item_conditions += " AND ({}) = ?".format(_TYPE_SQL)
        item_params.append(type)
    sort_key = _SORT_SQL[sort]
    direction = "ASC" if sort == "title" else "DESC"
    if cursor:
        data = _decode_cursor(cursor, sort, type, query)
        comparison = ">" if sort == "title" else "<"
        item_conditions += " AND ({key} {op} ? OR ({key} = ? AND f.id {op} ?))".format(key=sort_key, op=comparison)
        item_params.extend([data["key"], data["key"], data["id"]])
    with get_connection() as conn:
        counts_rows = conn.execute(
            "SELECT {} AS type, COUNT(*) AS count FROM files AS f WHERE {} GROUP BY 1".format(_TYPE_SQL, conditions),
            tuple(params),
        ).fetchall()
        rows = conn.execute(
            "SELECT f.*, s.title AS session_title, {} AS sort_key FROM files AS f".format(sort_key)
            + _JOIN_SQL + " WHERE " + item_conditions
            + " ORDER BY {} {}, f.id {} LIMIT ?".format(sort_key, direction, direction),
            tuple(item_params + [limit + 1]),
        ).fetchall()
    counts = {kind: 0 for kind in ("all",) + LIBRARY_TYPES}
    for row in counts_rows:
        counts[row["type"]] = int(row["count"])
        counts["all"] += int(row["count"])
    selected = rows[:limit]
    return {
        "items": [library_item(row) for row in selected],
        "next_cursor": _encode_cursor(selected[-1], sort, type, query) if len(rows) > limit else None,
        "counts": counts,
    }


def _owned_file(file_id: str, user_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(_SELECT_SQL + " WHERE f.id = ? AND f.user_id = ? AND f.deleted_at IS NULL", (file_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return dict(row)


def _decode_text(raw: bytes, truncated: bool) -> str:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            return decoder.decode(raw, final=not truncated)
        except UnicodeError:
            continue
    return raw.decode("utf-8-sig", errors="replace")


def _read_preview(record: dict[str, Any], maximum: int) -> tuple[str, bool]:
    try:
        storage = storage_for_row(record)
        raw = storage.get(str(record.get("storage_key") or ""), max_bytes=maximum)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File content not found") from exc
    except (StorageUnavailable, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_unavailable"}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="文件存储路径无效") from exc
    truncated = int(record.get("size_bytes") or 0) > maximum
    return _decode_text(raw, truncated), truncated


def _csv_preview(text: str, truncated: bool) -> dict[str, Any]:
    # CSV's default field limit is smaller than our bounded input. Permit a
    # single large cell within the same 2 MB budget, without reading more data.
    csv.field_size_limit(max(csv.field_size_limit(), TEXT_PREVIEW_BYTES))
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        columns = next(reader, [])
        truncated = truncated or len(columns) > CSV_MAX_COLUMNS
        rows = []
        total_rows = 0
        for values in reader:
            total_rows += 1
            truncated = truncated or len(values) > CSV_MAX_COLUMNS
            if len(rows) < CSV_MAX_ROWS:
                rows.append(values[:CSV_MAX_COLUMNS])
        truncated = truncated or total_rows > CSV_MAX_ROWS
    except csv.Error as exc:
        raise HTTPException(status_code=422, detail="CSV 格式无法预览") from exc
    return {"kind": "csv", "columns": columns[:CSV_MAX_COLUMNS], "rows": rows, "total_rows": total_rows, "truncated": truncated}


_CODE_LANGUAGES = {".py": "python", ".js": "javascript", ".ts": "typescript", ".json": "json", ".sql": "sql", ".sh": "bash"}
_MEDIA_LANGUAGES = {"application/json": "json", "text/javascript": "javascript", "application/javascript": "javascript", "application/typescript": "typescript", "text/typescript": "typescript", "text/x-python": "python", "application/x-python": "python", "application/sql": "sql", "text/x-sql": "sql", "application/x-sh": "bash", "text/x-shellscript": "bash"}


@router.get("/api/v1/library/{file_id}/preview")
def preview_file(request: Request, file_id: str) -> dict[str, Any]:
    user_id = _owner_id(request)
    record = _owned_file(file_id, user_id)
    timestamp = now()
    with get_connection() as conn:
        result = conn.execute(
            "UPDATE files SET last_opened_at = ?, updated_at = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL RETURNING id",
            (timestamp, timestamp, file_id, user_id),
        ).fetchone()
    if result is None:
        raise HTTPException(status_code=404, detail="File not found")
    kind = infer_type(record["filename"], record["media_type"])
    suffix = Path(record["filename"]).suffix.lower()
    media = record["media_type"].split(";", 1)[0].strip().lower()
    if kind == "image":
        return {"kind": "image"}
    if kind == "sheet":
        if suffix in {".xlsx", ".xls"} or (suffix != ".csv" and media not in {"text/csv", "application/csv"}):
            return {"kind": "none"}
        text, truncated = _read_preview(record, TEXT_PREVIEW_BYTES)
        return _csv_preview(text, truncated)
    if kind == "web":
        text, truncated = _read_preview(record, TEXT_PREVIEW_BYTES)
        return {"kind": "html_source", "text": text, "truncated": truncated}
    if suffix in {".md", ".markdown"} or (kind == "document" and media == "text/markdown" and suffix not in {".pdf", ".doc", ".docx"}):
        text, truncated = _read_preview(record, MARKDOWN_PREVIEW_BYTES)
        return {"kind": "markdown", "text": text, "truncated": truncated}
    if kind == "code" or suffix == ".txt" or (kind not in {"document", "archive"} and media.startswith("text/")) or (kind == "document" and media == "text/plain" and suffix not in {".pdf", ".doc", ".docx"}):
        text, truncated = _read_preview(record, TEXT_PREVIEW_BYTES)
        language = _CODE_LANGUAGES.get(suffix) or _MEDIA_LANGUAGES.get(media) or "text"
        return {"kind": "text", "text": text, "language": language, "truncated": truncated}
    return {"kind": "none"}


class LibraryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default=None, min_length=1, max_length=200, strict=True)
    pinned: Optional[StrictBool] = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            raise ValueError("title must contain 1-200 characters")
        return value


@router.patch("/api/v1/library/{file_id}")
def patch_file(request: Request, file_id: str, payload: LibraryPatch) -> dict[str, Any]:
    user_id = _owner_id(request)
    fields = payload.model_fields_set
    if "pinned" in fields and payload.pinned is None:
        raise HTTPException(status_code=422, detail="pinned 必须为布尔值")
    updates = []
    params: list[Any] = []
    for field in ("title", "pinned"):
        if field in fields:
            updates.append(field + " = ?")
            params.append(getattr(payload, field))
    with get_connection() as conn:
        row = conn.execute(_SELECT_SQL + " WHERE f.id = ? AND f.user_id = ? AND f.deleted_at IS NULL FOR UPDATE OF f", (file_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="File not found")
        record = dict(row)
        if updates:
            conn.execute("UPDATE files SET " + ", ".join(updates) + ", updated_at = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL", tuple(params + [now(), file_id, user_id]))
            for field in fields:
                record[field] = getattr(payload, field)
    return library_item(record)
