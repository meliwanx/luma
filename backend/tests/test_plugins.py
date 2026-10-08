"""Plugin loading, storage registration and deployment defaults."""

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import pg  # noqa: F401

from app import agent_runtime, main, mcp
from app.plugins import PluginError, load_plugins, mcp_presets, reset_plugins, shutdown_plugins, startup_plugins
from app.services import browser
from app.storage import StorageConfigurationError, StorageUnavailable, get_storage, storage_for_row, storage_registry


class PluginLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._drop_modules)

    def tearDown(self):
        reset_plugins()

    def _drop_modules(self):
        for name in list(sys.modules):
            if name.startswith("b6_plugin_") or name == "b6_order_sink":
                sys.modules.pop(name, None)

    def _write(self, filename, source):
        path = Path(self.tmp.name) / filename
        path.write_text(source, encoding="utf-8")
        return path

    def _env(self, plugins, **extra):
        values = {"LUMA_PLUGINS": plugins, "LUMA_PLUGIN_PATH": self.tmp.name}
        values.update(extra)
        return patch.dict(os.environ, values, clear=False)

    def test_plugins_register_in_order_and_serve_routes(self):
        self._write("b6_order_sink.py", "order = []\n")
        self._write("b6_plugin_first.py", """
from b6_order_sink import order
order.append("first")

def register_storage(registry):
    registry.register("plugfirst", lambda: object())

def register_routes(app):
    @app.get("/api/v1/plugin-demo/ping")
    def ping():
        return {"ok": True}

def register_mcp_presets(registry):
    registry.register("alpha", "https://alpha.example/mcp")

def on_startup():
    order.append("start-first")

def on_shutdown():
    order.append("stop-first")
""")
        self._write("b6_plugin_second.py", """
from b6_order_sink import order
order.append("second")

def register_storage(registry):
    registry.register("plugsecond", lambda: object())

def register_mcp_presets(registry):
    registry.register("beta", "https://beta.example/mcp")

def on_startup():
    order.append("start-second")

def on_shutdown():
    order.append("stop-second")
""")
        app = FastAPI()

        @app.get("/api/v1/health")
        def health():
            return {"core": True}

        with self._env("b6_plugin_first,b6_plugin_second"):
            load_plugins(app)
            asyncio.run(startup_plugins())
            asyncio.run(shutdown_plugins())
        from b6_order_sink import order
        self.assertEqual(order, ["first", "second", "start-first", "start-second", "stop-second", "stop-first"])
        self.assertIn("plugfirst", storage_registry.names())
        self.assertIn("plugsecond", storage_registry.names())
        self.assertEqual([item["name"] for item in mcp_presets()], ["alpha", "beta"])
        self.assertNotIn("token", str(mcp_presets()).lower())
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/plugin-demo/ping").json(), {"ok": True})
            self.assertEqual(client.get("/api/v1/health").json(), {"core": True})

    def test_plugin_path_is_required_to_import_an_outside_module(self):
        self._write("b6_plugin_outside.py", """
def register_storage(registry):
    registry.register("outside", lambda: object())
""")
        with patch.dict(os.environ, {"LUMA_PLUGINS": "b6_plugin_outside", "LUMA_PLUGIN_PATH": ""}, clear=False):
            with self.assertRaises(PluginError) as caught:
                load_plugins(None)
        self.assertIn("b6_plugin_outside", str(caught.exception))
        self.assertNotIn("outside", storage_registry.names())
        with self._env("b6_plugin_outside"):
            load_plugins(None)
        self.assertIn("outside", storage_registry.names())

    def test_import_failure_aborts_and_logs_only_the_exception_type(self):
        self._write("b6_plugin_bad.py", "raise RuntimeError('secret-import-value')\n")
        with self._env("b6_plugin_bad"), self.assertLogs("app.plugins", level="ERROR") as captured:
            with self.assertRaises(PluginError) as caught:
                load_plugins(None)
        output = "\n".join(captured.output)
        self.assertIn("module=b6_plugin_bad", output)
        self.assertIn("error=RuntimeError", output)
        self.assertNotIn("secret-import-value", output)
        self.assertNotIn("secret-import-value", str(caught.exception))
        self.assertIn("b6_plugin_bad", str(caught.exception))

    def test_startup_failure_aborts_and_lifespan_does_not_yield(self):
        self._write("b6_plugin_startup.py", """
def on_startup():
    raise RuntimeError("secret-startup-value")
""")
        with self._env("b6_plugin_startup"), self.assertLogs("app.plugins", level="ERROR") as captured:
            load_plugins(None)
            with self.assertRaises(PluginError) as caught:
                asyncio.run(startup_plugins())
        output = "\n".join(captured.output)
        self.assertIn("module=b6_plugin_startup", output)
        self.assertIn("error=RuntimeError", output)
        self.assertNotIn("secret-startup-value", output)
        self.assertNotIn("secret-startup-value", str(caught.exception))

        async def enter():
            with patch.object(main, "ensure_db"), patch.object(main, "get_storage"), patch.object(
                main.auth_module, "validate_auth_configuration"
            ), patch.object(main, "startup_plugins", side_effect=PluginError("插件加载失败: demo (RuntimeError)")):
                async with main.lifespan(main.app):
                    raise AssertionError("lifespan yielded")

        with self.assertRaises(PluginError):
            asyncio.run(enter())

    def test_routes_must_stay_under_api_and_cannot_override_core(self):
        self._write("b6_plugin_outside_route.py", """
def register_routes(app):
    @app.get("/plugin")
    def outside():
        return {"no": True}
""")
        self._write("b6_plugin_override.py", """
def register_routes(app):
    @app.get("/api/v1/health")
    def overridden():
        return {"plugin": True}
""")
        app = FastAPI()

        @app.get("/api/v1/health")
        def health():
            return {"core": True}

        with self._env("b6_plugin_outside_route"):
            with self.assertRaises(PluginError) as caught:
                load_plugins(app)
        self.assertIn("PluginRouteRejected", str(caught.exception))
        self.assertIn("路由必须挂在 /api/ 下", str(caught.exception))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/health").json(), {"core": True})
            self.assertEqual(client.get("/plugin").status_code, 404)

        with self._env("b6_plugin_override"):
            with self.assertRaises(PluginError) as caught:
                load_plugins(app)
        self.assertIn("不能覆盖核心路由", str(caught.exception))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/health").json(), {"core": True})

    def test_mcp_preset_rejects_credentials(self):
        self._write("b6_plugin_token.py", """
def register_mcp_presets(registry):
    registry.register("bad", "https://user:secret-token@mcp.example/mcp")
""")
        with self._env("b6_plugin_token"):
            with self.assertRaises(PluginError) as caught:
                load_plugins(None)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(mcp_presets(), [])

    def test_plugin_cannot_replace_builtin_storage(self):
        self._write("b6_plugin_builtin.py", """
def register_storage(registry):
    registry.register("local", lambda: object())
""")
        with self._env("b6_plugin_builtin"):
            with self.assertRaises(PluginError) as caught:
                load_plugins(None)
        self.assertIn("StorageConfigurationError", str(caught.exception))
        self.assertIn("local", storage_registry.names())


class FileservicePluginTests(unittest.TestCase):
    def tearDown(self):
        reset_plugins()

    def test_unregistered_name_is_explicit(self):
        with self.assertRaises(StorageUnavailable) as missing_row:
            storage_for_row({"storage": "fileservice"})
        self.assertIn("fileservice", str(missing_row.exception))
        with self.assertRaises(StorageConfigurationError) as missing_default:
            get_storage("fileservice")
        self.assertIn("fileservice", str(missing_default.exception))
        with self.assertRaises(StorageUnavailable) as blank:
            storage_for_row({"storage": ""})
        self.assertIn("未注册的存储后端", str(blank.exception))

    def test_example_plugin_registers_fileservice(self):
        values = {
            "LUMA_PLUGINS": "plugins_examples.fileservice",
            "LUMA_PLUGIN_PATH": "",
            "FILE_STORAGE": "fileservice",
            "FILE_SERVICE_URL": "https://files.example.test",
            "FILE_SERVICE_APP_KEY": "app-key",
            "FILE_SERVICE_APP_SECRET": "app-secret",
        }
        with patch.dict(os.environ, values, clear=False):
            load_plugins(None)
            storage = get_storage()
        self.assertEqual(storage.name, "fileservice")
        self.assertIn("fileservice", storage_registry.names())
        with patch.dict(os.environ, values, clear=False):
            self.assertEqual(storage_for_row({"storage": "fileservice"}).name, "fileservice")


class DeploymentDefaultTests(unittest.TestCase):
    def test_blocked_hosts_default_to_empty_and_source_has_no_address(self):
        with patch.dict(os.environ, {"MCP_BLOCKED_HOSTS": "", "BROWSER_BLOCKED_HOSTS": ""}, clear=False):
            self.assertEqual(mcp._blocked_hosts(), set())
            self.assertEqual(browser._blocked_hosts(), set())
        root = Path(__file__).resolve().parents[1] / "app"
        for relative in ("mcp.py", "services/browser.py"):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text), relative)
        runtime_source = (root / "agent_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("luma-code", runtime_source)
        self.assertNotIn("luma-browser", runtime_source)

    def test_runtime_region_and_tool_names_default_to_empty(self):
        values = {
            "AGENT_RUNTIME_ENABLED": "false",
            "AGENT_RUNTIME_REGION": "",
            "AGENT_RUNTIME_CODE_TOOL": "",
            "AGENT_RUNTIME_BROWSER_TOOL": "",
            "AGENT_RUNTIME_AIO_TOOL": "",
        }
        with patch.dict(os.environ, values, clear=False):
            settings = agent_runtime.config()
        self.assertEqual(settings.region, "")
        self.assertEqual(settings.code_tool, "")
        self.assertEqual(settings.browser_tool, "")
        self.assertEqual(agent_runtime.status()["reason"], "未配置")
        self.assertFalse(agent_runtime.sandbox_tools_configured())
        with self.assertRaises(agent_runtime.AgentRuntimeUnavailable) as code_error:
            agent_runtime._required_tool_name(SimpleNamespace(aio_tool="", code_tool="", browser_tool=""), "code")
        self.assertIn("AGENT_RUNTIME_CODE_TOOL", str(code_error.exception))
        with self.assertRaises(agent_runtime.AgentRuntimeUnavailable) as region_error:
            agent_runtime._require_region(SimpleNamespace(region=""))
        self.assertIn("AGENT_RUNTIME_REGION", str(region_error.exception))

    def test_browser_endpoint_requires_a_configured_suffix(self):
        box = SimpleNamespace(get_host=lambda _port: "9000-id.example.test", _envd_access_token="token")
        with patch.object(agent_runtime, "connect_user_sandbox", return_value=box), patch.dict(
            os.environ, {"SANDBOX_PREVIEW_HOST_SUFFIX": "", "E2B_DOMAIN": ""}, clear=False
        ):
            with self.assertRaises(agent_runtime.AgentRuntimeUnavailable) as caught:
                agent_runtime.connect_user_browser("user")
        self.assertIn("SANDBOX_PREVIEW_HOST_SUFFIX", str(caught.exception))

    def test_unconfigured_sandbox_tools_stay_hidden_and_name_the_variable(self):
        from app.agent import tools as agent_tools

        values = {
            "AGENT_RUNTIME_ENABLED": "true",
            "AGENT_RUNTIME_API_MODE": "e2b",
            "E2B_DOMAIN": "sandbox.example",
            "E2B_API_KEY": "test-key",
            "AGENT_RUNTIME_API_KEY": "",
            "AGENT_RUNTIME_CODE_TOOL": "",
            "AGENT_RUNTIME_BROWSER_TOOL": "",
            "AGENT_RUNTIME_AIO_TOOL": "",
            "AGENT_RUNTIME_REGION": "",
            "SANDBOX_PREVIEW_HOST_SUFFIX": "",
        }
        with patch.dict(os.environ, values, clear=False):
            self.assertFalse(agent_runtime.sandbox_tools_configured())
            payload = agent_runtime.status()
            self.assertIn("未配置", payload["reason"])
            self.assertIn("AGENT_RUNTIME_CODE_TOOL", payload["missing"])
            self.assertIn("AGENT_RUNTIME_BROWSER_TOOL", payload["missing"])
            with patch.object(agent_tools, "mcp_catalog", return_value=([], {}, [])):
                names = [item.name for item in agent_tools.registry_for("u", mode="interactive")[0]]
            self.assertNotIn("sandbox.python", names)
            self.assertNotIn("browser.open", names)
            settings = agent_runtime.config()
            with self.assertRaises(agent_runtime.AgentRuntimeUnavailable) as code_error:
                agent_runtime._e2b_start(settings, "code")
            self.assertIn("AGENT_RUNTIME_CODE_TOOL", str(code_error.exception))
            self.assertIn("未配置", str(code_error.exception))
            with self.assertRaises(agent_runtime.AgentRuntimeUnavailable) as browser_error:
                agent_runtime._required_tool_name(settings, "browser")
            self.assertIn("AGENT_RUNTIME_BROWSER_TOOL", str(browser_error.exception))

    def test_configured_tool_name_registers_tools_and_is_the_template(self):
        from app.agent import tools as agent_tools

        values = {
            "AGENT_RUNTIME_ENABLED": "true",
            "AGENT_RUNTIME_API_MODE": "e2b",
            "E2B_DOMAIN": "sandbox.example",
            "E2B_API_KEY": "test-key",
            "AGENT_RUNTIME_API_KEY": "",
            "AGENT_RUNTIME_CODE_TOOL": "your-code-tool-name",
            "AGENT_RUNTIME_BROWSER_TOOL": "your-browser-tool-name",
            "AGENT_RUNTIME_AIO_TOOL": "",
            "SANDBOX_PREVIEW_HOST_SUFFIX": ".sandbox.example",
        }
        box = SimpleNamespace(sandbox_id="box-1")
        created = MagicMock(return_value=box)
        with patch.dict(os.environ, values, clear=False), patch.object(
            agent_runtime, "_e2b_pause_lifecycle", return_value=None
        ), patch.dict(sys.modules, {"e2b": SimpleNamespace(Sandbox=SimpleNamespace(create=created))}):
            self.assertTrue(agent_runtime.sandbox_tools_configured())
            payload = agent_runtime.status()
            self.assertEqual(payload["reason"], "ready")
            self.assertEqual(payload["missing"], [])
            with patch.object(agent_tools, "mcp_catalog", return_value=([], {}, [])):
                names = [item.name for item in agent_tools.registry_for("u", mode="interactive")[0]]
            self.assertIn("sandbox.python", names)
            self.assertIn("browser.open", names)
            agent_runtime._e2b_start(agent_runtime.config(), "code")
            self.assertEqual(created.call_args.kwargs["template"], "your-code-tool-name")
            agent_runtime._e2b_start(agent_runtime.config(), "browser")
            self.assertEqual(created.call_args.kwargs["template"], "your-browser-tool-name")
