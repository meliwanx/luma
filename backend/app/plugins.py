"""Load private extensions without changing core modules.

``LUMA_PLUGINS`` is a comma-separated list of importable module names. Each
module may define any of ``register_storage``, ``register_routes``,
``register_mcp_presets``, ``on_startup`` and ``on_shutdown``. A failure aborts
startup. Logs record only the module name and the exception type.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .storage import clear_storage_cache, storage_registry

logger = logging.getLogger(__name__)

_loaded: List[Any] = []
_inserted_paths: List[str] = []


class PluginError(RuntimeError):
    """A configured plugin failed to load. The process must stop."""


class PluginRouteRejected(Exception):
    """A plugin route is outside /api/ or collides with an existing route."""


class McpPresetRegistry:
    """Name and URL templates for MCP connectors. Tokens are not accepted."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, str]] = {}

    def register(self, name: str, url: str) -> None:
        cleaned_name = str(name or "").strip()
        cleaned_url = str(url or "").strip()
        if not cleaned_name or len(cleaned_name) > 200 or any(char in cleaned_name for char in ("\r", "\n", "\x00")):
            raise ValueError("MCP 预置名称无效")
        parsed = urlsplit(cleaned_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MCP 预置地址必须是不含凭据的 https 地址")
        if cleaned_name in self._items:
            raise ValueError("MCP 预置已注册")
        self._items[cleaned_name] = {"name": cleaned_name, "url": cleaned_url}

    def items(self) -> List[Dict[str, str]]:
        return [dict(item) for item in self._items.values()]

    def clear(self) -> None:
        self._items.clear()


mcp_preset_registry = McpPresetRegistry()


def mcp_presets() -> List[Dict[str, str]]:
    """Return registered MCP templates. Each item has ``name`` and ``url`` only."""

    return mcp_preset_registry.items()


def reset_plugins() -> None:
    """Drop plugin registrations. Built-in storage backends stay in place."""

    storage_registry.unregister_plugins()
    clear_storage_cache()
    mcp_preset_registry.clear()
    _loaded.clear()
    for path in list(_inserted_paths):
        try:
            sys.path.remove(path)
        except ValueError:
            pass
    _inserted_paths.clear()


def _plugin_names() -> List[str]:
    return [item.strip() for item in os.getenv("LUMA_PLUGINS", "").split(",") if item.strip()]


def _apply_plugin_path() -> None:
    for item in os.getenv("LUMA_PLUGIN_PATH", "").split(","):
        raw = item.strip()
        if not raw:
            continue
        path = os.path.abspath(raw)
        if path not in sys.path:
            sys.path.insert(0, path)
            _inserted_paths.append(path)


def _fail(module_name: str, exc: BaseException, detail: str = "") -> None:
    logger.error("plugin load failed module=%s error=%s", module_name, type(exc).__name__)
    message = "插件加载失败: {} ({})".format(module_name, type(exc).__name__)
    if detail:
        message = message + ": " + detail
    raise PluginError(message) from None


def _call(module: Any, name: str, *args: Any) -> Any:
    function = getattr(module, name, None)
    if function is None:
        return None
    if not callable(function):
        raise TypeError(name)
    return function(*args)


def _route_key(route: Any) -> Optional[tuple]:
    path = getattr(route, "path", None) or ""
    if not path:
        return None
    methods = frozenset(getattr(route, "methods", None) or ())
    return path, methods


def _validate_routes(snapshot: List[Any], added: List[Any]) -> None:
    existing: Dict[str, set] = {}
    for route in snapshot:
        key = _route_key(route)
        if key is None:
            continue
        path, methods = key
        existing.setdefault(path, set()).update(methods)
    for route in added:
        key = _route_key(route)
        if key is None:
            raise PluginRouteRejected("路由必须挂在 /api/ 下")
        path, methods = key
        if not path.startswith("/api/"):
            raise PluginRouteRejected("路由必须挂在 /api/ 下")
        prior = existing.get(path)
        if prior is None:
            continue
        if not methods or not prior or methods & prior:
            raise PluginRouteRejected("不能覆盖核心路由")


def _register_routes(app: Any, module: Any) -> None:
    snapshot = list(app.routes)
    try:
        module.register_routes(app)
        known = {id(route) for route in snapshot}
        added = [route for route in app.routes if id(route) not in known]
        _validate_routes(snapshot, added)
    except Exception:
        app.routes[:] = snapshot
        raise


def _register_module(module: Any, app: Any) -> None:
    name = getattr(module, "__name__", "") or "<unknown>"
    try:
        _call(module, "register_storage", storage_registry)
        if getattr(module, "register_routes", None) is not None:
            if app is None:
                raise PluginRouteRejected("路由必须挂在 /api/ 下")
            _register_routes(app, module)
        _call(module, "register_mcp_presets", mcp_preset_registry)
    except PluginError:
        raise
    except PluginRouteRejected as exc:
        _fail(name, exc, str(exc))
    except Exception as exc:
        _fail(name, exc)


def load_plugins(app: Any = None) -> List[Any]:
    """Import ``LUMA_PLUGINS`` in order and run their registration hooks.

    Registration runs at process start, before the application serves traffic.
    ``on_startup`` is deferred to :func:`startup_plugins`.
    """

    reset_plugins()
    _apply_plugin_path()
    loaded: List[Any] = []
    for name in _plugin_names():
        try:
            module = importlib.import_module(name)
            _register_module(module, app)
        except PluginError:
            raise
        except Exception as exc:
            _fail(name, exc)
        loaded.append(module)
        _loaded.append(module)
    return list(_loaded)


async def _invoke(module: Any, hook: str) -> None:
    try:
        result = _call(module, hook)
        if inspect.isawaitable(result):
            await result
    except PluginError:
        raise
    except Exception as exc:
        _fail(getattr(module, "__name__", "") or "<unknown>", exc)


async def startup_plugins() -> None:
    """Call ``on_startup`` in plugin order. A failure aborts startup."""

    started: List[Any] = []
    try:
        for module in list(_loaded):
            await _invoke(module, "on_startup")
            started.append(module)
    except Exception:
        for module in reversed(started):
            await _shutdown_one(module)
        raise


async def _shutdown_one(module: Any) -> None:
    name = getattr(module, "__name__", "") or "<unknown>"
    try:
        result = _call(module, "on_shutdown")
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        logger.error("plugin shutdown failed module=%s error=%s", name, type(exc).__name__)


async def shutdown_plugins() -> None:
    """Call ``on_shutdown`` in reverse order. One failure does not skip the rest."""

    for module in reversed(list(_loaded)):
        await _shutdown_one(module)
