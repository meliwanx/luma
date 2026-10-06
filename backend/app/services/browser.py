"""Remote browser connections and bounded, untrusted page observations."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict
from urllib.parse import quote, urlsplit

from .. import agent_runtime
from ..db import _redis_client


logger = logging.getLogger(__name__)


class BrowserError(RuntimeError):
    """Safe browser failure; provider details must never reach users or models."""


class BrowserConfirmationRequired(BrowserError):
    """The current element no longer matches the approved browser action."""


@dataclass
class _Connection:
    lock: Any = field(default_factory=asyncio.Lock)
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    sandbox_id: str = ""
    access_token: str = field(default="", repr=False)
    touched_at: float = 0
    page_tasks: set = field(default_factory=set)


# A worker owns its CDP connections. Separate loop maps also keep short-lived
# test/request event loops from sharing Playwright transports or asyncio locks.
_connections: Any = weakref.WeakKeyDictionary()
_TIMEOUT_MS = 20_000
_SERVICE_WORKER_TIMEOUT_SECONDS = 2
_SUBMIT_TEXT = re.compile(r"提交|购买|支付|下单|确认订单|\b(?:submit|pay|buy|purchase|order|checkout|place\s+order)\b", re.I)
_LOG_URL = re.compile(r"\b(?:https?|wss?)://[^\s<>\"']+", re.I)
_LOG_CREDENTIAL = re.compile(
    r'''\b(?:[\w-]*token|authorization|(?:x[-_])?api[-_]?key|password|secret|cookie|set-cookie)["']?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n]*)''',
    re.I,
)
_LOG_AUTH = re.compile(r"\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]+", re.I)


def _log_failure(method: str, exc: Exception, access_token: str = "") -> None:
    message = str(exc)
    # Redact before truncation, including a provider's bare credential echo.
    for value in (access_token, quote(access_token, safe="")):
        if value:
            message = message.replace(value, "[redacted]")
    message = _LOG_URL.sub("[url]", message)
    message = _LOG_CREDENTIAL.sub("[redacted]", message)
    message = _LOG_AUTH.sub("[redacted]", message)
    # Playwright's subsequent Call log can echo selectors, input and URLs.
    message = next((line for line in message.splitlines() if line.strip()), "")
    message = " ".join(message.split())
    logger.warning("%s", ("browser %s failed: %s: %s" % (method, type(exc).__name__, message))[:200])


async def _try_service_worker_guard(method: str, operation: Any) -> bool:
    try:
        await asyncio.wait_for(operation, timeout=_SERVICE_WORKER_TIMEOUT_SECONDS)
        return True
    except Exception as exc:
        logger.warning("%s: %s", method, type(exc).__name__)
        return False


def _blocked_hosts() -> set:
    hosts = set()
    for name in ("MCP_BLOCKED_HOSTS", "BROWSER_BLOCKED_HOSTS"):
        hosts.update(item.strip().lower().rstrip(".").lstrip("*.") for item in os.getenv(name, "").split(",") if item.strip())
    return hosts


def validate_url(url: str) -> str:
    """Validate cloud navigation, without imposing the host's SSRF policy."""
    if not isinstance(url, str) or len(url) > 8000 or any(char in url for char in ("\r", "\n", "\\", "\x00")):
        raise ValueError("浏览器地址无效")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("浏览器只允许 http 和 https 地址")
    try:
        parsed.port
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise ValueError("浏览器地址无效") from None
    # Chromium accepts legacy numeric IPv4 spellings (decimal/hex/short form).
    # Normalize those too, so the blocked server IP cannot be disguised.
    try:
        address = ipaddress.ip_address(host)
        host = str(getattr(address, "ipv4_mapped", None) or address)
    except ValueError:
        try:
            host = socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            pass
    if any(host == blocked or host.endswith("." + blocked.lstrip("*.")) for blocked in _blocked_hosts()):
        raise ValueError("浏览器不能访问 Luma 服务或被禁止的地址")
    return url


_PAGE_READ = """() => {
    const body = document.body ? document.body.cloneNode(true) : document.createElement('div');
    body.querySelectorAll('script,style,noscript,template').forEach(node => node.remove());
    const text = (body.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 8000);
    const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 50).map(a => ({text: (a.innerText || '').slice(0, 200), url: a.href}));
    const fields = Array.from(document.querySelectorAll('input,textarea,select')).slice(0, 100).map(e => ({name: (e.name || e.id || '').slice(0, 200), type: (e.type || e.tagName.toLowerCase()).slice(0, 40)}));
    return {text, links, fields};
}"""
_ELEMENT_INFO = """e => { e = e.closest('button,input,a,[role="button"]') || e; return ({
    tag: e.tagName.toLowerCase(), type: e.type || '',
    in_form: !!e.closest('form') || !!e.form,
    label: (e.innerText || e.value || e.getAttribute('aria-label') || '').slice(0, 500),
    name: e.name || '', id: e.id || '', href: e.href || '',
    action: (e.form || e.closest('form'))?.action || '',
    method: (e.form || e.closest('form'))?.method || '',
    html: e.outerHTML.slice(0, 2000)
}); }"""
_GUARDED_CLICK = """(e, args) => {
    if (!e.isConnected) return false;
    e = e.closest('button,input,a,[role="button"]') || e;
    const inspect = INSPECT;
    if (!e.isConnected || JSON.stringify(inspect(e)) !== JSON.stringify(args.expected)) return false;
    if (args.submit && e.tagName.toLowerCase() === 'form') e.requestSubmit();
    else e.click();
    return true;
}""".replace("INSPECT", _ELEMENT_INFO)
_NO_SERVICE_WORKERS = """(() => {
    if (typeof ServiceWorkerContainer !== 'undefined') {
        const descriptor = Object.getOwnPropertyDescriptor(ServiceWorkerContainer.prototype, 'register');
        if (descriptor && !descriptor.configurable) {
            if (!descriptor.writable && descriptor.value.name === 'lumaDisabledServiceWorkerRegistration') return;
            throw new Error('Cannot guard service worker registration');
        }
        Object.defineProperty(ServiceWorkerContainer.prototype, 'register', {
            value: function lumaDisabledServiceWorkerRegistration() { return Promise.reject(new Error('Service workers are disabled')); },
            configurable: false, writable: false
        });
    }
})()"""
_UNREGISTER_SERVICE_WORKERS = """async () => {
    if (navigator.serviceWorker) {
        const items = await navigator.serviceWorker.getRegistrations();
        await Promise.all(items.map(item => item.unregister()));
    }
}"""


def _slot(user_id: str) -> _Connection:
    loop = asyncio.get_running_loop()
    slots = _connections.setdefault(loop, {})
    return slots.setdefault(agent_runtime._hash_user(user_id), _Connection())


async def _disconnect(connection: _Connection) -> None:
    browser, playwright = connection.browser, connection.playwright
    connection.browser = connection.playwright = connection.context = connection.page = None
    connection.sandbox_id = ""
    connection.access_token = ""
    for task in list(connection.page_tasks):
        task.cancel()
    if connection.page_tasks:
        await asyncio.gather(*connection.page_tasks, return_exceptions=True)
        connection.page_tasks.clear()
    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=5)
        except Exception:
            pass
    if playwright is not None:
        try:
            await asyncio.wait_for(playwright.stop(), timeout=5)
        except Exception:
            pass


async def close_connections() -> None:
    slots = _connections.pop(asyncio.get_running_loop(), {})
    for connection in slots.values():
        async with connection.lock:
            await _disconnect(connection)


async def reap_connections() -> None:
    slots = _connections.get(asyncio.get_running_loop(), {})
    ttl = agent_runtime.config().idle_ttl_seconds
    for connection in list(slots.values()):
        if connection.browser is not None and not connection.lock.locked() and time.monotonic() - connection.touched_at >= ttl:
            async with connection.lock:
                await _disconnect(connection)


async def _guard_page(context: Any, page: Any) -> None:
    session = await context.new_cdp_session(page)
    patterns = []
    for host in sorted(_blocked_hosts()):
        host = host.lstrip("*.")
        patterns.extend(["*://" + host + "/*", "*://" + host + ":*/*", "*://*." + host + "/*", "*://*." + host + ":*/*"])
    # CDP blocking also covers redirect destinations and must remain required.
    await session.send("Network.enable")
    await session.send("Network.setBlockedURLs", {"urls": patterns})
    # Best effort: avoid old SW caches/interception affecting page reads or
    # bypassing context.route (which cannot intercept SW-handled requests).
    # Reuse the remote context to preserve its session; disable registrations
    # and bypass/unregister existing workers where the remote browser supports it.
    await _try_service_worker_guard("Network.setBypassServiceWorker", session.send("Network.setBypassServiceWorker", {"bypass": True}))
    if await _try_service_worker_guard("ServiceWorker.enable", session.send("ServiceWorker.enable")):
        await _try_service_worker_guard("ServiceWorker.stopAllWorkers", session.send("ServiceWorker.stopAllWorkers"))
    await _try_service_worker_guard("page.evaluate", page.evaluate(_NO_SERVICE_WORKERS))
    await _try_service_worker_guard("page.evaluate", page.evaluate(_UNREGISTER_SERVICE_WORKERS))
    for frame in page.frames:
        if frame != page.main_frame:
            await _try_service_worker_guard("frame.evaluate", frame.evaluate(_NO_SERVICE_WORKERS))
            await _try_service_worker_guard("frame.evaluate", frame.evaluate(_UNREGISTER_SERVICE_WORKERS))
    page.set_default_timeout(_TIMEOUT_MS)


async def _connect(connection: _Connection, user_id: str, force: bool = False) -> Any:
    failure_message = "浏览器连接失败"
    try:
        sandbox, cdp_url, _live_url = await asyncio.get_running_loop().run_in_executor(None, agent_runtime.connect_user_browser, user_id)
        sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
        if not force and connection.browser is not None and connection.sandbox_id == sandbox_id and connection.browser.is_connected():
            pages = [page for page in connection.context.pages if not page.is_closed()]
            if pages:
                connection.page = pages[-1]
                connection.touched_at = time.monotonic()
                return connection.page
        await _disconnect(connection)
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            failure_message = "浏览器依赖尚未安装"
            raise
        connection.access_token = str(sandbox._envd_access_token)
        connection.playwright = await async_playwright().start()
        connection.browser = await connection.playwright.chromium.connect_over_cdp(
            cdp_url, headers={"X-Access-Token": connection.access_token}, timeout=_TIMEOUT_MS,
        )
        connection.context = connection.browser.contexts[0]
        await _try_service_worker_guard("context.add_init_script", connection.context.add_init_script(_NO_SERVICE_WORKERS))
        async def guard(route: Any) -> None:
            try:
                validate_url(route.request.url)
            except ValueError:
                await route.abort("blockedbyclient")
            else:
                await route.continue_()
        # Includes popups, frames, redirects and subresources, so a click or
        # server redirect cannot navigate back into Luma with browser cookies.
        await connection.context.route("**/*", guard)
        async def guard_new_page(page: Any) -> None:
            try:
                await _guard_page(connection.context, page)
            except Exception:
                # If a popup cannot be guarded it must not stay usable.
                try:
                    await page.close()
                except Exception:
                    pass
        def on_page(page: Any) -> None:
            task = asyncio.create_task(guard_new_page(page))
            connection.page_tasks.add(task)
            task.add_done_callback(connection.page_tasks.discard)
        connection.context.on("page", on_page)
        for existing_page in connection.context.pages:
            await _guard_page(connection.context, existing_page)
        connection.page = connection.context.pages[0] if connection.context.pages else await connection.context.new_page()
        connection.sandbox_id = sandbox_id
        connection.touched_at = time.monotonic()
        return connection.page
    except Exception as exc:
        _log_failure("connect", exc, connection.access_token)
        await _disconnect(connection)
        raise BrowserError(failure_message) from None


async def _perform(user_id: str, operation: Any, *, retry: bool = True) -> Any:
    connection = _slot(user_id)
    async with connection.lock, _shared_lock(user_id):
        for attempt in range(2):
            operation_started = False
            try:
                page = await asyncio.wait_for(_connect(connection, user_id, force=bool(attempt)), timeout=25)
                operation_started = True
                return await asyncio.wait_for(operation(page), timeout=25)
            except (ValueError, BrowserConfirmationRequired):
                raise
            except Exception as exc:
                _log_failure("perform", exc, connection.access_token)
                await _disconnect(connection)
                if operation_started and not retry:
                    # Restore the transport once, but never replay a write
                    # whose outcome is unknown after a timeout/disconnection.
                    try:
                        await asyncio.wait_for(_connect(connection, user_id, force=True), timeout=25)
                    except Exception:
                        pass
                if attempt or (operation_started and not retry):
                    raise BrowserError("浏览器操作失败，请重试") from None


@asynccontextmanager
async def _shared_lock(user_id: str) -> Any:
    # Redis serializes operations across the two API workers without holding
    # a scarce PostgreSQL connection while CDP or a website is responding.
    lock = None
    acquired = False
    try:
        def acquire() -> Any:
            item = _redis_client().lock("luma:browser:" + agent_runtime._hash_user(user_id), timeout=180, thread_local=False)
            return item, item.acquire(blocking=True, blocking_timeout=20)
        lock, acquired = await asyncio.get_running_loop().run_in_executor(None, acquire)
        if not acquired:
            raise BrowserError("浏览器正在处理其他操作，请重试")
    except Exception:
        raise BrowserError("浏览器操作锁暂不可用，请重试") from None
    try:
        yield
    finally:
        if lock is not None and acquired:
            try:
                await asyncio.get_running_loop().run_in_executor(None, lock.release)
            except Exception:
                pass


async def _observation(page: Any, details: bool = True) -> Dict[str, Any]:
    validate_url(page.url)
    value = await page.evaluate(_PAGE_READ)
    data = {"title": str(await page.title())[:500], "url": page.url, "text": str(value.get("text") or "")[:8000], "untrusted": True}
    if details:
        links = []
        for item in value.get("links", [])[:50]:
            try:
                validate_url(item["url"])
            except (ValueError, KeyError):
                continue
            links.append({"text": str(item.get("text") or "")[:200], "url": item["url"]})
        fields = [{"name": str(item.get("name") or "")[:200], "type": str(item.get("type") or "")[:40]} for item in value.get("fields", [])[:100] if isinstance(item, dict)]
        data.update(links=links, fields=fields)
    return data


async def open_page(user_id: str, url: str) -> Dict[str, Any]:
    validate_url(url)
    async def operation(page: Any) -> Dict[str, Any]:
        await page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        return await _observation(page, False)
    return await _perform(user_id, operation)


async def read_page(user_id: str) -> Dict[str, Any]:
    return await _perform(user_id, _observation)


async def screenshot(user_id: str, full_page: bool = False) -> bytes:
    async def operation(page: Any) -> bytes:
        validate_url(page.url)
        return await page.screenshot(type="png", full_page=bool(full_page), timeout=_TIMEOUT_MS)
    return await _perform(user_id, operation)


def _locator(page: Any, args: Dict[str, Any]) -> Any:
    selector = str(args.get("selector") or "").strip()
    text = str(args.get("text") or "").strip()
    if selector and len(selector) <= 2000:
        return page.locator(selector).first
    if text and len(text) <= 500:
        return page.get_by_text(text, exact=True).first
    raise ValueError("请提供 selector 或 text")


async def _element_inspection(page: Any, args: Dict[str, Any]) -> Any:
    validate_url(page.url)
    element = await _locator(page, args).element_handle(timeout=_TIMEOUT_MS)
    if element is None:
        raise ValueError("浏览器元素不存在")
    info = await element.evaluate(_ELEMENT_INFO)
    description = " ".join(str(info.get(key) or "") for key in ("label", "name", "id", "href", "action"))
    from .permissions import is_always_forbidden
    always = is_always_forbidden(description) or bool(re.search(r"购买|支付|下单|确认订单|\bbuy\b", description, re.I))
    submits = info.get("type", "").lower() == "submit" or bool(info.get("in_form")) or bool(_SUBMIT_TEXT.search(description))
    fingerprint = hashlib.sha256(json.dumps({"url": page.url, "element": info}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return element, info, {"submits": bool(submits or always), "always_confirm": always, "fingerprint": fingerprint}


async def _inspect(page: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    _element, _snapshot, result = await _element_inspection(page, args)
    return result


async def inspect_click(user_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return await _perform(user_id, lambda page: _inspect(page, args))


def _approval_matches(approval: Any, action: str, args: Dict[str, Any], info: Dict[str, Any]) -> bool:
    return isinstance(approval, dict) and approval.get("allow_submission") is True and approval.get("action") == action and approval.get("arguments") == args and approval.get("fingerprint") == info["fingerprint"] and (not info["always_confirm"] or approval.get("confirmed") is True)


async def click(user_id: str, args: Dict[str, Any], approval: Any = None, *, submit: bool = False) -> Dict[str, Any]:
    async def operation(page: Any) -> Dict[str, Any]:
        element, snapshot, info = await _element_inspection(page, args)
        if (submit or info["submits"]) and not _approval_matches(approval, "browser.submit" if submit else "browser.click", args, info):
            raise BrowserConfirmationRequired("页面已变化，请重新确认提交")
        # The identity and attributes are checked in the same synchronous
        # browser task as the click, so a selector cannot resolve to a newly
        # inserted Pay button after its read-only inspection.
        clicked = await element.evaluate(_GUARDED_CLICK, {"expected": snapshot, "submit": submit})
        if clicked is not True:
            raise BrowserConfirmationRequired("页面已变化，请重新确认提交")
        return {"url": page.url, "clicked": True, "untrusted": True}
    # A submitting click must never be replayed after an uncertain timeout.
    return await _perform(user_id, operation, retry=False)


async def type_text(user_id: str, selector: str, text: str) -> Dict[str, Any]:
    if not selector or len(selector) > 2000 or len(text) > 20_000:
        raise ValueError("输入字段或文本无效")
    async def operation(page: Any) -> Dict[str, Any]:
        validate_url(page.url)
        await page.locator(selector).first.fill(text, timeout=_TIMEOUT_MS)
        return {"typed": True}
    return await _perform(user_id, operation)


async def scroll(user_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
    direction = str(args.get("direction") or "down")
    if direction not in {"up", "down", "left", "right"}:
        raise ValueError("滚动方向无效")
    amount = min(max(int(args.get("amount", 600)), 1), 5000)
    x = amount * (-1 if direction == "left" else 1) if direction in {"left", "right"} else 0
    y = amount * (-1 if direction == "up" else 1) if direction in {"up", "down"} else 0
    async def operation(page: Any) -> Dict[str, Any]:
        validate_url(page.url)
        await page.mouse.wheel(x, y)
        return {"scrolled": True}
    return await _perform(user_id, operation)


async def live(user_id: str) -> Dict[str, Any]:
    async def ready(page: Any) -> None:
        return None
    await _perform(user_id, ready)
    _sandbox, _cdp, url = await asyncio.get_running_loop().run_in_executor(None, agent_runtime.connect_user_browser, user_id)
    return {"kind": "browser_live", "url": url, "expires_in": agent_runtime.config().idle_ttl_seconds}
