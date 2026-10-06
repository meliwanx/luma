"""Private user file upload and download endpoints."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import tempfile
from itertools import chain
from typing import Iterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, File as FastAPIFile, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..db import get_connection
from ..models import StoredFile
from ..storage import Storage, StorageConfigurationError, StorageUnavailable, get_storage, storage_for_row
from ..services.files import FILE_CHUNK_SIZE, max_upload_size, safe_upload_filename, sweep_deleted_files, user_file_prefix
from ._common import make_stored_file, new_id, now, owner_id, require_session

logger = logging.getLogger(__name__)
router = APIRouter()


def _session_check(session_id: str, user_id: str) -> None:
    with get_connection() as conn:
        require_session(conn, session_id, user_id)


def _insert_file(data: dict[str, object]) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO files(id,user_id,session_id,filename,title,origin,media_type,size_bytes,sha256,storage,storage_key,created_at,updated_at) VALUES (:id,:user_id,:session_id,:filename,:title,:origin,:media_type,:size_bytes,:sha256,:storage,:storage_key,:created_at,:updated_at)",
            data,
        )


def _remove_object(storage: Storage, storage_key: str) -> None:
    try:
        storage.delete(storage_key)
    except Exception as exc:
        logger.warning("failed to remove orphaned file storage_key=%s error=%s", storage_key, type(exc).__name__)


@router.get("/api/v1/files", response_model=list[StoredFile])
def list_files(request: Request, session_id: Optional[str] = Query(default=None), limit: int = Query(default=100, ge=1, le=500)) -> list[StoredFile]:
    user_id = owner_id(request)
    with get_connection() as conn:
        if session_id:
            rows = conn.execute("SELECT * FROM files WHERE user_id = ? AND session_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?", (user_id, session_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM files WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [make_stored_file(dict(row), request) for row in rows]


@router.post("/api/v1/files", response_model=StoredFile, status_code=status.HTTP_201_CREATED)
async def upload_file(request: Request, upload: UploadFile = FastAPIFile(..., description="要上传到当前用户工作区的文件"), session_id: Optional[str] = Form(default=None)) -> StoredFile:
    try:
        user_id = await run_in_threadpool(owner_id, request)
        if session_id:
            await run_in_threadpool(_session_check, session_id, user_id)
        filename = safe_upload_filename(upload.filename)
    except Exception:
        await upload.close()
        raise
    media_type = (upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";", 1)[0].strip()[:200]
    if not media_type or any(ord(char) < 32 or ord(char) == 127 for char in media_type):
        media_type = "application/octet-stream"
    file_id = new_id("file")
    key_hint = "%s/%s" % (user_file_prefix(user_id), file_id)
    maximum = max_upload_size()
    temporary = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    digest = hashlib.sha256()
    total = 0
    storage: Optional[Storage] = None
    storage_key: Optional[str] = None
    try:
        try:
            storage = await run_in_threadpool(get_storage)
        except StorageConfigurationError as exc:
            raise HTTPException(status_code=503, detail={"code": "storage_unavailable"}) from exc
        while True:
            chunk = await upload.read(FILE_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail={"code": "file_too_large", "max_bytes": maximum})
            digest.update(chunk)
            await run_in_threadpool(temporary.write, chunk)
        await run_in_threadpool(temporary.seek, 0)
        try:
            storage_key = await run_in_threadpool(storage.put, key_hint, temporary, total, media_type)
        except StorageConfigurationError as exc:
            raise HTTPException(status_code=503, detail={"code": "storage_unavailable"}) from exc
        timestamp = now()
        data: dict[str, object] = {"id": file_id, "user_id": user_id, "session_id": session_id, "filename": filename, "title": filename, "origin": "upload", "media_type": media_type, "size_bytes": total, "sha256": digest.hexdigest(), "storage": storage.name, "storage_key": storage_key, "created_at": timestamp, "updated_at": timestamp}
        try:
            await run_in_threadpool(_insert_file, data)
        except Exception as exc:
            if storage_key:
                await run_in_threadpool(_remove_object, storage, storage_key)
            raise HTTPException(status_code=500, detail="文件元数据保存失败") from exc
        return make_stored_file(data, request)
    except HTTPException:
        raise
    except Exception as exc:
        if storage is not None and storage_key:
            await run_in_threadpool(_remove_object, storage, storage_key)
        logger.warning("file upload failed error=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="文件保存失败") from exc
    finally:
        await upload.close()
        # SpooledTemporaryFile.close may flush an on-disk rollover; keep that
        # synchronous filesystem work off the event loop as well.
        await run_in_threadpool(temporary.close)


@router.get("/api/v1/files/{file_id}", response_model=StoredFile)
def get_file(request: Request, file_id: str) -> StoredFile:
    user_id = owner_id(request)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (file_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return make_stored_file(dict(row), request)


def _stream_with_first(storage: Storage, storage_key: str) -> Iterator[bytes]:
    iterator = storage.open_stream(storage_key)
    try:
        first = next(iterator)
    except StopIteration:
        return
    yield first
    yield from iterator


def _content_disposition(filename: str) -> str:
    encoded = quote(filename or "upload", safe="!#$&+-.^_`|~")
    # Keep an ASCII fallback for older clients while preserving the exact
    # Unicode name through RFC 5987's filename* parameter.
    fallback = "".join(char if 32 <= ord(char) < 127 and char not in {'\\', '"', '/'} else "_" for char in (filename or "upload")) or "download"
    return 'attachment; filename="{}"; filename*=UTF-8\'\'{}'.format(fallback, encoded)


@router.get("/api/v1/files/{file_id}/content", name="download_file")
def download_file(request: Request, file_id: str) -> StreamingResponse:
    user_id = owner_id(request)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (file_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    record = dict(row)
    try:
        storage = storage_for_row(record)
        stream = _stream_with_first(storage, str(record.get("storage_key") or ""))
        first = next(stream)
        body: Iterator[bytes] = chain((first,), stream)
    except StopIteration:
        body = iter(())
    except StorageUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_unavailable"}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File content not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="文件存储路径无效") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "storage_unavailable"}) from exc
    headers = {"Content-Disposition": _content_disposition(str(record.get("filename") or "upload")), "X-Content-Type-Options": "nosniff"}
    media_type = str(record.get("media_type") or "application/octet-stream").split(";", 1)[0].strip()[:200]
    if not media_type or any(ord(char) < 32 or ord(char) == 127 for char in media_type):
        media_type = "application/octet-stream"
    return StreamingResponse(body, media_type=media_type, headers=headers)


@router.delete("/api/v1/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(request: Request, file_id: str) -> None:
    user_id = owner_id(request)
    timestamp = now()
    with get_connection() as conn:
        row = conn.execute("SELECT storage,storage_key FROM files WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (file_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="File not found")
        conn.execute("UPDATE files SET deleted_at = ?, updated_at = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (timestamp, timestamp, file_id, user_id))
    try:
        storage = storage_for_row(dict(row))
        storage.delete(str(row["storage_key"]))
    except Exception as exc:
        logger.warning("failed to delete file id=%s error=%s", file_id, type(exc).__name__)


__all__ = ["router", "list_files", "upload_file", "get_file", "download_file", "delete_file", "sweep_deleted_files"]
