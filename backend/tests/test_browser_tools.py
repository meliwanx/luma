"""Remote browser tools use mocks and never launch a local browser."""
import asyncio
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

from tests import pg  # noqa: F401
from app.agent import tools
from app.services import browser


class BrowserToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = MagicMock()
        self.page.url = "https://example.com/"
        self.page.is_closed.return_value = False
        self.page.goto = AsyncMock()
        self.page.title = AsyncMock(return_value="Example")
        self.observe = AsyncMock(return_value={"text": "readable " * 2000, "links": [{"text": "next", "url": "https://example.com/next"}] * 60, "fields": [{"name": "search", "type": "text"}]})
        async def evaluate_page(script):
            return await self.observe() if script == browser._PAGE_READ else None
        self.page.evaluate = AsyncMock(side_effect=evaluate_page)
        self.page.screenshot = AsyncMock(return_value=b"PNG")
        self.page.mouse.wheel = AsyncMock()
        self.locator = MagicMock()
        self.locator.evaluate = AsyncMock(return_value={"tag": "button", "type": "button", "in_form": False, "label": "Next", "html": "<button>Next</button>"})
        self.locator.click = AsyncMock()
        self.locator.fill = AsyncMock()
        async def evaluate_element(script, args=None):
            if script == browser._ELEMENT_INFO:
                return await self.locator.evaluate(script)
            await self.locator.click()
            return True
        self.element = SimpleNamespace(evaluate=AsyncMock(side_effect=evaluate_element))
        self.locator.element_handle = AsyncMock(return_value=self.element)
        self.page.locator.return_value.first = self.locator
        self.page.get_by_text.return_value.first = self.locator
        self.cdp = SimpleNamespace(send=AsyncMock())
        self.context = SimpleNamespace(pages=[self.page], route=AsyncMock(), new_cdp_session=AsyncMock(return_value=self.cdp), on=MagicMock(), add_init_script=AsyncMock())
        self.remote = SimpleNamespace(contexts=[self.context], is_connected=lambda: True, close=AsyncMock())
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=self.remote)), stop=AsyncMock())
        self.start = AsyncMock(return_value=self.playwright)
        self.box = SimpleNamespace(sandbox_id="remote-browser", _envd_access_token="unit-test-token")
        self.runtime_patch = patch("app.services.browser.agent_runtime.connect_user_browser", return_value=(self.box, "https://9000-test.ap-hongkong.tencentags.com/cdp?access_token=unit-test-token", "https://9000-test.ap-hongkong.tencentags.com/novnc/vnc_lite.html?access_token=unit-test-token"))
        self.runtime_patch.start()
        self.playwright_patch = patch.dict(sys.modules, {"playwright.async_api": SimpleNamespace(async_playwright=lambda: SimpleNamespace(start=self.start))})
        self.playwright_patch.start()
        self.distributed_lock = MagicMock()
        self.distributed_lock.acquire.return_value = True
        self.redis = SimpleNamespace(lock=MagicMock(return_value=self.distributed_lock))
        self.redis_patch = patch("app.services.browser._redis_client", return_value=self.redis)
        self.redis_patch.start()
        self.ctx = tools.AgentContext("browser-user", session_id="browser-session")

    async def asyncTearDown(self):
        await browser.close_connections()
        self.playwright_patch.stop()
        self.runtime_patch.stop()
        self.redis_patch.stop()

    async def test_open_read_reuse_remote_cdp_and_bound_untrusted_data(self):
        opened = await tools._browser_open(self.ctx, {"url": "https://example.com/"})
        read = await tools._browser_read(self.ctx, {})
        self.assertEqual(opened.status, "ok")
        self.assertEqual(len(opened.data["text"]), 8000)
        self.assertTrue(opened.data["untrusted"])
        self.assertEqual(len(read.data["links"]), 50)
        self.assertEqual(read.data["fields"], [{"name": "search", "type": "text"}])
        self.start.assert_awaited_once()
        connect = self.playwright.chromium.connect_over_cdp
        connect.assert_awaited_once()
        self.assertEqual(connect.await_args.kwargs["headers"], {"X-Access-Token": "unit-test-token"})
        self.page.goto.assert_awaited_once()

    async def test_screenshot_stores_authenticated_image_file(self):
        record = {"id": "screen-id", "filename": "browser-screenshot.png", "media_type": "image/png", "size_bytes": 3}
        with patch("app.agent.tools.files_service.store_file_bytes", return_value=record) as store:
            result = await tools._browser_screenshot(self.ctx, {"full_page": True})
        self.assertEqual(result.data["kind"], "file")
        self.assertEqual(result.data["media_type"], "image/png")
        self.assertEqual(store.call_args.args[:2], ("browser-user", "browser-session"))
        self.assertEqual(store.call_args.args[-1], b"PNG")
        self.page.screenshot.assert_awaited_once_with(type="png", full_page=True, timeout=20000)

    async def test_live_model_text_and_logs_contain_no_token(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        handler = Capture()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            with patch("app.services.browser.agent_runtime.config", return_value=SimpleNamespace(idle_ttl_seconds=300)):
                result = await tools._browser_live(self.ctx, {})
        finally:
            root_logger.removeHandler(handler)
        self.assertEqual(result.text, "已为用户打开实时画面")
        self.assertEqual(result.data["kind"], "browser_live")
        self.assertNotIn("unit-test-token", result.text)
        self.assertNotIn("unit-test-token", " ".join(records))

    async def test_route_blocks_redirects_subresources_and_self_domains(self):
        with patch.dict(os.environ, {"BROWSER_BLOCKED_HOSTS": "luma.example.org,203.0.113.10", "MCP_BLOCKED_HOSTS": "blocked.example.org"}):
            await browser.open_page("browser-user", "https://example.com/")
            blocked = next(call.args[1]["urls"] for call in self.cdp.send.await_args_list if call.args[0] == "Network.setBlockedURLs")
            self.assertIn("*://203.0.113.10/*", blocked)
            self.assertIn("*://luma.example.org/*", blocked)
            guard = self.context.route.await_args.args[1]
            for url in ("https://203.0.113.10/x", "https://luma.example.org/", "https://sub.blocked.example.org/"):
                route = SimpleNamespace(request=SimpleNamespace(url=url), abort=AsyncMock(), continue_=AsyncMock())
                await guard(route)
                route.abort.assert_awaited_once()
                route.continue_.assert_not_awaited()

    async def test_service_workers_are_enabled_before_stopping_remote_workers(self):
        enabled = False
        async def send(method, params=None):
            nonlocal enabled
            if method == "ServiceWorker.enable":
                enabled = True
            elif method == "ServiceWorker.stopAllWorkers" and not enabled:
                raise RuntimeError("ServiceWorker domain not enabled")
        self.cdp.send.side_effect = send
        connection = browser._slot("browser-user")
        self.assertIs(await browser._connect(connection, "browser-user"), self.page)
        methods = [call.args[0] for call in self.cdp.send.await_args_list]
        self.assertIn("ServiceWorker.stopAllWorkers", methods)
        self.assertLess(methods.index("ServiceWorker.enable"), methods.index("ServiceWorker.stopAllWorkers"))
        self.remote.close.assert_not_awaited()

    async def test_unsupported_service_worker_cdp_methods_do_not_fail_connection(self):
        for unsupported in ("Network.setBypassServiceWorker", "ServiceWorker.enable", "ServiceWorker.stopAllWorkers"):
            with self.subTest(method=unsupported):
                self.cdp.send.reset_mock()
                async def send(method, params=None):
                    if method == unsupported:
                        raise RuntimeError("unsupported method access_token=unit-test-token")
                self.cdp.send.side_effect = send
                connection = browser._slot("browser-user")
                with self.assertLogs(browser.logger.name, level="WARNING") as logs:
                    self.assertIs(await browser._connect(connection, "browser-user"), self.page)
                self.assertEqual([record.getMessage() for record in logs.records], [unsupported + ": RuntimeError"])
                methods = [call.args[0] for call in self.cdp.send.await_args_list]
                self.assertIn("Network.setBlockedURLs", methods)
                if unsupported == "ServiceWorker.enable":
                    self.assertNotIn("ServiceWorker.stopAllWorkers", methods)
                else:
                    self.assertLess(methods.index("ServiceWorker.enable"), methods.index("ServiceWorker.stopAllWorkers"))
                await browser._disconnect(connection)

    async def test_service_worker_timeout_does_not_block_connection_or_later_guards(self):
        waiting = asyncio.Event()
        async def send(method, params=None):
            if method == "Network.setBypassServiceWorker":
                await waiting.wait()
        self.cdp.send.side_effect = send
        with patch.object(browser, "_SERVICE_WORKER_TIMEOUT_SECONDS", 0.01):
            with self.assertLogs(browser.logger.name, level="WARNING") as logs:
                result = await browser.read_page("browser-user")
        self.assertEqual(result["title"], "Example")
        self.assertEqual([record.getMessage() for record in logs.records], ["Network.setBypassServiceWorker: TimeoutError"])
        self.cdp.send.assert_any_await("ServiceWorker.stopAllWorkers")
        self.page.evaluate.assert_any_await(browser._NO_SERVICE_WORKERS)
        self.page.evaluate.assert_any_await(browser._UNREGISTER_SERVICE_WORKERS)
        self.observe.assert_awaited_once()
        self.remote.close.assert_not_awaited()

    async def test_service_worker_init_script_failure_is_best_effort(self):
        self.context.add_init_script.side_effect = RuntimeError("unit-test-token")
        with self.assertLogs(browser.logger.name, level="WARNING") as logs:
            self.assertIs(await browser._connect(browser._slot("browser-user"), "browser-user"), self.page)
        self.assertEqual([record.getMessage() for record in logs.records], ["context.add_init_script: RuntimeError"])
        self.context.route.assert_awaited_once()
        self.remote.close.assert_not_awaited()

    async def test_page_and_frame_service_worker_evaluation_is_best_effort(self):
        async def evaluate_page(script):
            if script == browser._PAGE_READ:
                return await self.observe()
            raise RuntimeError("unit-test-token in service worker script")
        self.page.evaluate.side_effect = evaluate_page
        frame = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("unit-test-token")))
        self.page.frames = [self.page.main_frame, frame]
        with self.assertLogs(browser.logger.name, level="WARNING") as logs:
            result = await browser.read_page("browser-user")
        self.assertEqual(result["title"], "Example")
        self.assertEqual([record.getMessage() for record in logs.records], ["page.evaluate: RuntimeError"] * 2 + ["frame.evaluate: RuntimeError"] * 2)
        self.observe.assert_awaited_once()
        frame.evaluate.assert_any_await(browser._NO_SERVICE_WORKERS)
        frame.evaluate.assert_any_await(browser._UNREGISTER_SERVICE_WORKERS)
        self.remote.close.assert_not_awaited()

    async def test_network_blocking_failures_still_close_connection(self):
        for unsupported in ("Network.enable", "Network.setBlockedURLs"):
            with self.subTest(method=unsupported):
                self.remote.close.reset_mock()
                self.playwright.stop.reset_mock()
                async def send(method, params=None):
                    if method == unsupported:
                        raise RuntimeError("network guard unavailable access_token=unit-test-token")
                self.cdp.send.side_effect = send
                connection = browser._slot("browser-user")
                with self.assertLogs(browser.logger.name, level="WARNING") as logs:
                    with self.assertRaisesRegex(browser.BrowserError, "^浏览器连接失败$"):
                        await browser._connect(connection, "browser-user")
                self.assertNotIn("unit-test-token", " ".join(record.getMessage() for record in logs.records))
                self.remote.close.assert_awaited_once()
                self.playwright.stop.assert_awaited_once()
                self.assertIsNone(connection.browser)
                self.assertEqual(connection.access_token, "")
        self.page.goto.assert_not_awaited()

    async def test_route_installation_failure_still_closes_connection(self):
        self.context.route.side_effect = RuntimeError("route unavailable")
        with self.assertLogs(browser.logger.name, level="WARNING"):
            with self.assertRaisesRegex(browser.BrowserError, "^浏览器连接失败$"):
                await browser._connect(browser._slot("browser-user"), "browser-user")
        self.remote.close.assert_awaited_once()
        self.playwright.stop.assert_awaited_once()
        self.page.goto.assert_not_awaited()

    async def test_new_page_service_worker_failure_does_not_close_page(self):
        connection = browser._slot("browser-user")
        await browser._connect(connection, "browser-user")
        popup = MagicMock()
        popup.frames = []
        popup.evaluate = AsyncMock(side_effect=RuntimeError("unit-test-token"))
        popup.close = AsyncMock()
        on_page = self.context.on.call_args.args[1]
        with self.assertLogs(browser.logger.name, level="WARNING") as logs:
            on_page(popup)
            await asyncio.gather(*list(connection.page_tasks))
        self.assertEqual([record.getMessage() for record in logs.records], ["page.evaluate: RuntimeError"] * 2)
        popup.close.assert_not_awaited()
        popup.set_default_timeout.assert_called_once_with(20000)

    async def test_new_page_network_blocking_failure_closes_page(self):
        connection = browser._slot("browser-user")
        await browser._connect(connection, "browser-user")
        popup = SimpleNamespace(close=AsyncMock())
        async def send(method, params=None):
            if method == "Network.setBlockedURLs":
                raise RuntimeError("network guard unavailable")
        self.cdp.send.side_effect = send
        self.context.on.call_args.args[1](popup)
        await asyncio.gather(*list(connection.page_tasks))
        popup.close.assert_awaited_once()

    async def test_connect_failures_log_type_without_credentials(self):
        token = "unit-test/token+value="
        self.box._envd_access_token = token
        messages = (
            "CDP connection refused: " + token,
            "CDP connection refused: " + quote(token, safe=""),
            "CDP connection refused https://remote.example/cdp?access_token=url-secret&other=private-query",
            "CDP connection refused X-Access-Token: header-secret",
            "CDP connection refused {'X-Access-Token': 'dict-secret'}",
            "CDP connection refused Authorization: Bearer bearer-secret",
            'CDP connection refused {"Authorization": "Basic basic-secret"}',
        )
        for message in messages:
            with self.subTest(message=message):
                self.playwright.chromium.connect_over_cdp.side_effect = RuntimeError(message)
                with self.assertLogs(browser.logger.name, level="WARNING") as logs:
                    with self.assertRaisesRegex(browser.BrowserError, "^浏览器连接失败$"):
                        await browser._connect(browser._slot("browser-user"), "browser-user")
                logged = " ".join(record.getMessage() for record in logs.records)
                self.assertIn("browser connect failed: RuntimeError: CDP connection refused", logged)
                for secret in (token, quote(token, safe=""), "url-secret", "private-query", "header-secret", "dict-secret", "bearer-secret", "basic-secret"):
                    self.assertNotIn(secret, logged)

    async def test_runtime_connect_failure_is_logged_and_returns_generic_error(self):
        with patch("app.services.browser.agent_runtime.connect_user_browser", side_effect=RuntimeError("provider unavailable access_token=runtime-secret")):
            with self.assertLogs(browser.logger.name, level="WARNING") as logs:
                with self.assertRaisesRegex(browser.BrowserError, "^浏览器连接失败$"):
                    await browser._connect(browser._slot("browser-user"), "browser-user")
        logged = " ".join(record.getMessage() for record in logs.records)
        self.assertIn("browser connect failed: RuntimeError: provider unavailable", logged)
        self.assertNotIn("runtime-secret", logged)
        self.start.assert_not_awaited()

    async def test_observation_errors_log_safely_and_keep_model_error_generic(self):
        self.observe.side_effect = RuntimeError("page evaluation failed unit-test-token https://remote.example/read?access_token=query-secret X-Access-Token: header-secret\nCall log:\n  - reading private page")
        with self.assertLogs(browser.logger.name, level="WARNING") as logs:
            result = await tools._browser_read(self.ctx, {})
        self.assertEqual(result.status, "error")
        self.assertEqual(result.data, {"error": "读取页面失败"})
        self.assertEqual(self.observe.await_count, 2)
        self.assertEqual(self.start.await_count, 2)
        self.assertEqual(len(logs.records), 2)
        for record in logs.records:
            message = record.getMessage()
            self.assertIn("browser perform failed: RuntimeError: page evaluation failed", message)
            self.assertLessEqual(len(message), 200)
            for private in ("unit-test-token", "query-secret", "header-secret", "Call log", "private page", "\n"):
                self.assertNotIn(private, message)

    async def test_failure_logs_are_single_line_and_bounded(self):
        self.playwright.chromium.connect_over_cdp.side_effect = RuntimeError("connection refused\nCall log:\n  - private selector and token")
        with self.assertLogs(browser.logger.name, level="WARNING") as logs:
            with self.assertRaises(browser.BrowserError):
                await browser._connect(browser._slot("browser-user"), "browser-user")
        self.assertEqual(logs.records[0].getMessage(), "browser connect failed: RuntimeError: connection refused")
        self.playwright.chromium.connect_over_cdp.side_effect = RuntimeError("连接超时 " + "很长的诊断信息 " * 100 + "access_token=unit-test-token")
        with self.assertLogs(browser.logger.name, level="WARNING") as logs:
            with self.assertRaises(browser.BrowserError):
                await browser._connect(browser._slot("browser-user"), "browser-user")
        message = logs.records[0].getMessage()
        self.assertIn("browser connect failed: RuntimeError: 连接超时", message)
        self.assertEqual(len(message), 200)
        self.assertNotIn("unit-test-token", message)
        self.assertNotIn("\n", message)

    async def test_failed_read_reconnects_once_without_exception_details(self):
        self.observe.side_effect = [RuntimeError("unit-test-token in provider exception"), {"text": "reconnected", "links": [], "fields": []}]
        result = await tools._browser_read(self.ctx, {})
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.start.await_count, 2)
        self.assertNotIn("unit-test-token", result.text)

    async def test_click_rechecks_changed_form_before_execution(self):
        args = {"selector": "button"}
        info = await browser.inspect_click("browser-user", args)
        self.assertFalse(info["submits"])
        self.locator.evaluate.return_value = {"tag": "button", "type": "submit", "in_form": True, "label": "Submit", "html": "<button type=submit>Submit</button>"}
        result = await tools._browser_click(self.ctx, args)
        self.assertEqual(result.status, "needs_confirmation")
        self.locator.click.assert_not_awaited()

    async def test_submit_requires_matching_single_use_server_approval(self):
        args = {"selector": "button"}
        self.locator.evaluate.return_value = {"tag": "button", "type": "submit", "in_form": True, "label": "Submit", "html": "<button type=submit>Submit</button>"}
        info = await browser.inspect_click("browser-user", args)
        denied = await tools._browser_submit(self.ctx, args)
        self.assertEqual(denied.status, "needs_confirmation")
        self.ctx.browser_approval = {"action": "browser.submit", "arguments": args, "fingerprint": info["fingerprint"], "allow_submission": True, "confirmed": True}
        allowed = await tools._browser_submit(self.ctx, args)
        self.assertEqual(allowed.status, "ok")
        self.assertIsNone(self.ctx.browser_approval)
        again = await tools._browser_submit(self.ctx, args)
        self.assertEqual(again.status, "needs_confirmation")
        self.locator.click.assert_awaited_once()

    async def test_payment_words_always_need_current_confirmation(self):
        for label in ("pay", "purchase", "order", "checkout", "Place order", "购买", "支付", "下单"):
            self.locator.evaluate.return_value = {"tag": "button", "type": "button", "in_form": False, "label": label, "html": "<button>%s</button>" % label}
            info = await browser.inspect_click("browser-user", {"text": label})
            self.assertTrue(info["submits"], label)
            self.assertTrue(info["always_confirm"], label)

    async def test_write_timeout_reconnects_without_double_submission(self):
        args = {"selector": "button"}
        self.locator.evaluate.return_value = {"tag": "button", "type": "submit", "in_form": True, "label": "Submit", "html": "<button>Submit</button>"}
        info = await browser.inspect_click("browser-user", args)
        self.ctx.browser_approval = {"action": "browser.submit", "arguments": args, "fingerprint": info["fingerprint"], "allow_submission": True, "confirmed": True}
        self.locator.click.side_effect = TimeoutError("unit-test-token")
        result = await tools._browser_submit(self.ctx, args)
        self.assertEqual(result.status, "error")
        self.locator.click.assert_awaited_once()
        self.assertEqual(self.start.await_count, 2)
        self.assertNotIn("unit-test-token", result.text)

    async def test_user_actions_are_locked_and_users_have_separate_connections(self):
        active = 0
        peak = 0
        async def operation(page):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
        await asyncio.gather(browser._perform("browser-user", operation), browser._perform("browser-user", operation))
        self.assertEqual(peak, 1)
        await browser._perform("other-user", operation)
        self.assertEqual(self.start.await_count, 2)

    async def test_redis_lock_covers_operation_and_fails_closed(self):
        await browser.read_page("browser-user")
        self.redis.lock.assert_called_with("luma:browser:" + browser.agent_runtime._hash_user("browser-user"), timeout=180, thread_local=False)
        self.distributed_lock.release.assert_called_once()
        self.distributed_lock.acquire.return_value = False
        with self.assertRaises(browser.BrowserError):
            await browser.read_page("browser-user")
        self.assertEqual(self.observe.await_count, 1)

    async def test_changed_or_detached_element_is_never_clicked(self):
        async def changed_element(script, args=None):
            return await self.locator.evaluate(script) if script == browser._ELEMENT_INFO else False
        self.element.evaluate.side_effect = changed_element
        result = await tools._browser_click(self.ctx, {"text": "Next"})
        self.assertEqual(result.status, "needs_confirmation")
        self.locator.click.assert_not_awaited()

    async def test_type_scroll_and_non_submitting_click_execute(self):
        self.assertEqual((await tools._browser_type(self.ctx, {"selector": "input", "text": "hello"})).status, "ok")
        self.assertEqual((await tools._browser_scroll(self.ctx, {"direction": "down", "amount": 500})).status, "ok")
        self.assertEqual((await tools._browser_click(self.ctx, {"text": "Next"})).status, "ok")
        self.locator.fill.assert_awaited_once_with("hello", timeout=20000)
        self.page.mouse.wheel.assert_awaited_once_with(0, 500)


class BrowserUrlTests(unittest.TestCase):
    def test_forbidden_schemes_hosts_and_legacy_ip_forms(self):
        with patch.dict(os.environ, {"BROWSER_BLOCKED_HOSTS": "luma.example.org,203.0.113.10", "MCP_BLOCKED_HOSTS": "blocked.example.org"}):
            for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://example.com", "https://203.0.113.10/", "https://0xcb00710a/", "https://[::ffff:203.0.113.10]/", "https://luma.example.org/", "https://sub.luma.example.org/", "https://blocked.example.org/", "https://user:secret@example.com/"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    browser.validate_url(url)
        self.assertEqual(browser.validate_url("http://example.com/"), "http://example.com/")

    def test_browser_registry_exposes_all_eight_tools_when_runtime_enabled(self):
        with patch("app.agent.tools._sandbox_enabled", return_value=True), patch("app.agent.tools.mcp_catalog", return_value=([], {}, {})):
            registered, _ = tools.registry_for("browser-user", mode="interactive")
        selected = {tool.name: tool for tool in registered if tool.name.startswith("browser.")}
        self.assertEqual(set(selected), {"browser.open", "browser.read", "browser.screenshot", "browser.click", "browser.type", "browser.scroll", "browser.submit", "browser.live"})
        self.assertEqual(selected["browser.submit"].risk, "external_write")
        self.assertTrue(all(tool.risk == "read" for name, tool in selected.items() if name != "browser.submit"))
