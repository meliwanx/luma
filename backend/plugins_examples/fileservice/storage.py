"""HTTPS file-service storage used by the example plugin."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Any, BinaryIO, Dict, Iterator, Optional
from urllib.parse import urlparse

import httpx

from app.storage import CHUNK_SIZE, StorageConfigurationError, _collect_limited, _safe_key

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.Client] = None
_http_client_lock = threading.RLock()


def _shared_http_client(timeout: float) -> httpx.Client:
    global _http_client
    with _http_client_lock:
        if _http_client is None:
            _http_client = httpx.Client(timeout=timeout, follow_redirects=False)
        return _http_client


def _positive_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid remote file id")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    else:
        raise ValueError("invalid remote file id")
    if result <= 0:
        raise ValueError("invalid remote file id")
    return result


class FileServiceStorage:
    name = "fileservice"

    def __init__(
        self,
        base_url: Optional[str] = None,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        folder_id: Optional[str] = None,
        timeout: Optional[float] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else os.getenv("FILE_SERVICE_URL", "")).strip().rstrip("/")
        self.app_key = (app_key if app_key is not None else os.getenv("FILE_SERVICE_APP_KEY", "")).strip()
        self.app_secret = (app_secret if app_secret is not None else os.getenv("FILE_SERVICE_APP_SECRET", "")).strip()
        self.folder_id = (folder_id if folder_id is not None else os.getenv("FILE_SERVICE_FOLDER_ID", "")).strip()
        if timeout is None:
            try:
                timeout = float(os.getenv("FILE_SERVICE_TIMEOUT", "60"))
            except (TypeError, ValueError):
                timeout = 60.0
        self.timeout = max(0.1, float(timeout))
        self._validate_url()
        self.client = client or _shared_http_client(self.timeout)

    @classmethod
    def from_env(cls) -> "FileServiceStorage":
        missing = [name for name in ("FILE_SERVICE_URL", "FILE_SERVICE_APP_KEY", "FILE_SERVICE_APP_SECRET") if not os.getenv(name, "").strip()]
        if missing:
            raise StorageConfigurationError("FileService 存储缺少配置变量: " + ", ".join(missing))
        return cls()

    def _validate_url(self) -> None:
        if not self.base_url:
            raise StorageConfigurationError("FileService 存储缺少配置变量: FILE_SERVICE_URL")
        parsed = urlparse(self.base_url)
        if parsed.username or parsed.password:
            raise StorageConfigurationError("FILE_SERVICE_URL 不能包含用户名或密码")
        allow_http = os.getenv("FILE_SERVICE_ALLOW_HTTP", "false").strip().lower() == "true"
        if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"):
            raise StorageConfigurationError("FILE_SERVICE_URL 必须使用 https；本地开发可设置 FILE_SERVICE_ALLOW_HTTP=true")
        if not parsed.netloc:
            raise StorageConfigurationError("FILE_SERVICE_URL 格式无效")

    def _headers(self) -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        signature = base64.b64encode(hmac.new(self.app_secret.encode("utf-8"), timestamp.encode("utf-8"), hashlib.sha256).digest()).decode("ascii")
        return {"X-App-Key": self.app_key, "X-Timestamp": timestamp, "X-Signature": signature}

    @staticmethod
    def _status_error(method: str, status: int, elapsed: float) -> RuntimeError:
        logger.warning("FileService %s status=%s elapsed_ms=%.1f", method, status, elapsed * 1000)
        return RuntimeError("FileService 请求失败")

    def _json_success(self, response: httpx.Response, method: str, started: float) -> Dict[str, Any]:
        elapsed = time.monotonic() - started
        logger.info("FileService %s status=%s elapsed_ms=%.1f", method, response.status_code, elapsed * 1000)
        if response.status_code < 200 or response.status_code >= 300:
            raise self._status_error(method, response.status_code, elapsed)
        try:
            payload = response.json()
        except Exception:
            raise RuntimeError("FileService 响应格式无效") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload.get("code") != 0:
            raise RuntimeError("FileService 响应失败")
        return payload

    def put(self, key_hint: str, fileobj: BinaryIO, size: int, content_type: str) -> str:
        key = _safe_key(key_hint)
        file_id = key.rsplit("/", 1)[-1]
        data: Dict[str, str] = {}
        if self.folder_id:
            data["folder_id"] = self.folder_id
        files = {"file": (file_id, fileobj, content_type or "application/octet-stream")}
        started = time.monotonic()
        try:
            response = self.client.post(
                self.base_url + "/api/fs/files/upload",
                headers=self._headers(),
                data=data,
                files=files,
                timeout=self.timeout,
                follow_redirects=False,
            )
            payload = self._json_success(response, "POST /api/fs/files/upload", started)
            remote_id = _positive_id((payload.get("data") or {}).get("id") if isinstance(payload.get("data"), dict) else None)
            logger.info("FileService POST /api/fs/files/upload id=%s status=%s elapsed_ms=%.1f", remote_id, response.status_code, (time.monotonic() - started) * 1000)
            return "fs:" + str(remote_id)
        except (StorageConfigurationError, ValueError):
            raise
        except RuntimeError:
            raise
        except Exception:
            # Do not retain the transport exception as a chained cause: a
            # custom HTTP client can include request headers in its message.
            raise RuntimeError("FileService 上传失败") from None

    def _remote_id(self, storage_key: str) -> int:
        if not isinstance(storage_key, str) or not storage_key.startswith("fs:"):
            raise ValueError("invalid FileService storage key")
        return _positive_id(storage_key[3:])

    def open_stream(self, storage_key: str) -> Iterator[bytes]:
        remote_id = self._remote_id(storage_key)
        url = self.base_url + "/api/fs/files/{}/download".format(remote_id)

        def iterator() -> Iterator[bytes]:
            started = time.monotonic()
            try:
                with self.client.stream(
                    "GET", url, headers=self._headers(), timeout=self.timeout, follow_redirects=False
                ) as response:
                    elapsed = time.monotonic() - started
                    logger.info("FileService GET /api/fs/files/%s/download status=%s elapsed_ms=%.1f", remote_id, response.status_code, elapsed * 1000)
                    if response.status_code == 404:
                        raise FileNotFoundError(storage_key)
                    if response.status_code < 200 or response.status_code >= 300:
                        raise self._status_error("GET /api/fs/files/{}/download".format(remote_id), response.status_code, elapsed)
                    for chunk in response.iter_bytes(CHUNK_SIZE):
                        if chunk:
                            yield chunk
            except FileNotFoundError:
                raise
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError("FileService 下载失败") from None

        return iterator()

    def get(self, storage_key: str, max_bytes: Optional[int] = None) -> bytes:
        return _collect_limited(self.open_stream(storage_key), max_bytes)

    def delete(self, storage_key: str) -> None:
        remote_id = self._remote_id(storage_key)
        started = time.monotonic()
        try:
            response = self.client.delete(
                self.base_url + "/api/fs/files/{}".format(remote_id),
                headers=self._headers(),
                timeout=self.timeout,
                follow_redirects=False,
            )
            elapsed = time.monotonic() - started
            logger.info("FileService DELETE /api/fs/files/%s status=%s elapsed_ms=%.1f", remote_id, response.status_code, elapsed * 1000)
            if response.status_code == 404:
                return
            if response.status_code < 200 or response.status_code >= 300:
                raise self._status_error("DELETE /api/fs/files/{}".format(remote_id), response.status_code, elapsed)
            # Delete responses use the common envelope when present.  Avoid
            # logging or propagating its contents.
            if response.content:
                try:
                    payload = response.json()
                except Exception:
                    raise RuntimeError("FileService 响应格式无效") from None
                if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload.get("code") != 0:
                    raise RuntimeError("FileService 删除失败")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("FileService 删除失败") from None
