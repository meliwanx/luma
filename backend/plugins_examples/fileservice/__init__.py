"""Example plugin: HTTPS file-service storage.

Enable it with ``LUMA_PLUGINS=plugins_examples.fileservice``. Core builds do
not load it, so ``FILE_STORAGE=fileservice`` stays unavailable until then.
Requests use ``FILE_SERVICE_*`` credentials and a timestamp HMAC. The plugin
stores only the remote id; it never returns the service URL to clients.
"""

from __future__ import annotations

from .storage import FileServiceStorage

_CONFIG_KEYS = (
    "FILE_SERVICE_URL",
    "FILE_SERVICE_APP_KEY",
    "FILE_SERVICE_APP_SECRET",
    "FILE_SERVICE_FOLDER_ID",
    "FILE_SERVICE_TIMEOUT",
    "FILE_SERVICE_ALLOW_HTTP",
)


def register_storage(registry) -> None:
    """Register the ``fileservice`` backend on the shared storage registry."""

    registry.register("fileservice", FileServiceStorage.from_env, config_keys=_CONFIG_KEYS)


__all__ = ["FileServiceStorage", "register_storage"]
