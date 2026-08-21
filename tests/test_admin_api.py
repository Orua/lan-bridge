import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from code_cn_bridge import admin_api, server


class CodexSwitchTests(unittest.TestCase):
    def test_admin_guard_allows_loopback_only(self):
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            connection = SimpleNamespace(client=SimpleNamespace(host=host))
            admin_api._require_local_admin(connection)

        for host in ("192.168.1.20", "10.0.0.2", "testclient", ""):
            connection = SimpleNamespace(client=SimpleNamespace(host=host))
            with self.assertRaises(admin_api.HTTPException) as raised:
                admin_api._require_local_admin(connection)
            self.assertEqual(raised.exception.status_code, 403)

    def test_manual_reload_refreshes_config_and_catalog_without_restart(self):
        config = self._config()
        config.model_mapping = {
            "deepseek-v4-pro": {
                "target": "deepseek-v4-pro",
                "provider": "deepseek",
            },
        }
        with patch.object(server, "reload_config", return_value=config) as reload_call, patch.object(
            server, "_refresh_codex_model_catalog_if_active"
        ) as refresh:
            reloaded = server._reload_runtime_config()

        self.assertIs(reloaded, config)
        reload_call.assert_called_once_with()
        refresh.assert_called_once_with(config)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.toml_path = Path(self.temp_dir.name) / "config.toml"
        self.bridge_settings_path = Path(self.temp_dir.name) / "codex-bridge-settings.yaml"
        self.catalog_path = Path(self.temp_dir.name) / "merged-models.json"
        self.toml_path.write_text(
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            'name = "custom"\n'
            'base_url = "http://127.0.0.1:8765/v1"\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _config(self, proxy_url=""):
        return SimpleNamespace(
            _data={"server": {"codex_official_proxy_url": proxy_url}, "model_slots": {}},
            server_host="127.0.0.1",
            server_port=8765,
            model_mapping={},
            providers={},
        )

    def test_codex_catalog_prefers_official_account_cache(self):
        home = Path(self.temp_dir.name) / "home"
        cache_path = home / ".codex" / "models_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(
            json.dumps({
                "fetched_at": "2026-08-17T08:33:23Z",
                "models": [
                    {"slug": "gpt-5.4", "visibility": "list"},
                    {"slug": "gpt-5.3-codex-spark", "visibility": "list"},
                ],
            }),
            encoding="utf-8",
        )

        with patch.object(admin_api.Path, "home", return_value=home), patch.object(
            admin_api, "_codex_cli_candidates"
        ) as candidates:
            payload = admin_api._load_codex_catalog()

        self.assertEqual(
            [model["slug"] for model in payload["models"]],
            ["gpt-5.4", "gpt-5.3-codex-spark"],
        )
        candidates.assert_not_called()

    def test_codex_catalog_falls_back_to_bundled_when_cache_is_invalid(self):
        home = Path(self.temp_dir.name) / "home"
        cache_path = home / ".codex" / "models_cache.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text("{not-json", encoding="utf-8")
        executable = Path(self.temp_dir.name) / "codex.exe"
        executable.touch()
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"models": [{"slug": "gpt-bundled"}]}),
        )

        with patch.object(admin_api.Path, "home", return_value=home), patch.object(
            admin_api, "_codex_cli_candidates", return_value=[executable]
        ), patch.object(admin_api.subprocess, "run", return_value=completed) as run:
            payload = admin_api._load_codex_catalog()

        self.assertEqual(payload["models"][0]["slug"], "gpt-bundled")
        self.assertIn("--bundled", run.call_args.args[0])

    def test_restore_official_preserves_unrelated_user_sections(self):
        self.toml_path.write_text(
            'model = "gpt-5.4-mini"\n'
            'model_provider = "custom"\n'
            'model_context_window = 256000\n'
            'model_auto_compact_token_limit = 160000\n'
            'tool_output_token_limit = 12000\n'
            '[model_providers.custom]\n'
            'name = "custom"\n'
            '[mcp_servers.example]\n'
            'url = "https://example.invalid/mcp"\n'
            '[projects."C:\\\\work"]\n'
            'trust_level = "trusted"\n',
            encoding="utf-8",
        )
        with patch.object(admin_api, "_CODEX_TOML", self.toml_path), patch.object(
            admin_api, "get_config", return_value=self._config("http://127.0.0.1:7890")
        ), patch.object(
            admin_api, "_codex_desktop_process"
        ), patch.object(
            admin_api, "_stop_codex_desktop"
        ) as stop, patch.object(
            admin_api, "_restart_codex_after_response"
        ) as restart:
            response = asyncio.run(admin_api.codex_switch_to_official())

        content = self.toml_path.read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.example]", content)
        self.assertIn('[projects."C:\\\\work"]', content)
        self.assertNotIn("[features.network_proxy]", content)
        self.assertNotIn("proxy_url", content)
        self.assertIn('model = "gpt-5.6-sol"', content)
        self.assertNotIn("model_provider =", content)
        self.assertNotIn("openai_base_url =", content)
        self.assertNotIn("model_context_window =", content)
        self.assertNotIn("model_auto_compact_token_limit =", content)
        self.assertNotIn("tool_output_token_limit =", content)
        self.assertNotIn("enable_request_compression", content)
        self.assertFalse(response["codex_restarted"])
        self.assertIn("保留 MCP", response["message"])
        self.assertTrue(response["preserved_settings"])
        self.assertEqual(response["official_proxy_url"], "")
        self.assertEqual(len(list((self.toml_path.parent / "backups").glob("config.toml.*.bak"))), 1)
        stop.assert_not_called()
        restart.assert_not_called()

    def test_enable_unified_removes_proxy_without_touching_desktop_process(self):
        self.toml_path.write_text(
            'model = "gpt-5.4-mini"\n'
            '[features.network_proxy]\n'
            'enabled = true\n'
            'proxy_url = "http://127.0.0.1:7890"\n',
            encoding="utf-8",
        )

        self.bridge_settings_path.write_text(
            "model_context_window: 512000\n"
            "model_auto_compact_token_limit: 384000\n"
            "tool_output_token_limit: 12000\n",
            encoding="utf-8",
        )
        with patch.object(admin_api, "_CODEX_TOML", self.toml_path), patch.object(
            admin_api, "get_config", return_value=self._config()
        ), patch.object(
            admin_api, "_codex_bridge_settings_path", return_value=self.bridge_settings_path
        ), patch.object(
            admin_api, "get_model_catalog_path", return_value=None
        ), patch.object(
            admin_api, "_refresh_codex_model_catalog", return_value=self.catalog_path
        ), patch.object(
            admin_api, "_codex_desktop_process", return_value=None
        ), patch.object(
            admin_api, "_stop_codex_desktop"
        ) as stop, patch.object(
            admin_api, "_restart_codex_after_response"
        ) as restart:
            response = asyncio.run(admin_api.codex_switch_to_custom())

        content = self.toml_path.read_text(encoding="utf-8")
        self.assertNotIn("model_provider =", content)
        self.assertIn('model = "gpt-5.4-mini"', content)
        self.assertIn('openai_base_url = "http://127.0.0.1:8765/v1"', content)
        self.assertIn(f'model_catalog_json = "{str(self.catalog_path).replace(chr(92), chr(92) * 2)}"', content)
        self.assertIn("[features]", content)
        self.assertIn("enable_request_compression = false", content)
        self.assertNotIn("[model_providers.cnbridge]", content)
        self.assertNotIn("model_context_window =", content)
        self.assertNotIn("model_auto_compact_token_limit =", content)
        self.assertIn("tool_output_token_limit = 12000", content)
        self.assertNotIn("[features.network_proxy]", content)
        self.assertFalse(response["codex_restarted"])
        self.assertIn("当前运行实例未被关闭", response["message"])
        stop.assert_not_called()
        restart.assert_not_called()

    def test_bridge_base_url_uses_loopback_for_wildcard_bind(self):
        config = self._config()
        config.server_host = "0.0.0.0"

        with patch.object(admin_api, "get_config", return_value=config):
            base_url = admin_api._bridge_base_url()

        self.assertEqual(base_url, "http://127.0.0.1:8765/v1")

    def test_switch_to_custom_does_not_duplicate_model_when_config_has_bom(self):
        self.toml_path.write_bytes(
            (
                '\ufeffmodel = "gpt-5.4-mini"\n'
                "[features.network_proxy]\n"
                "enabled = true\n"
            ).encode("utf-8")
        )
        with patch.object(admin_api, "_CODEX_TOML", self.toml_path), patch.object(
            admin_api, "get_config", return_value=self._config()
        ), patch.object(
            admin_api, "_codex_bridge_settings_path", return_value=self.bridge_settings_path
        ), patch.object(
            admin_api, "get_model_catalog_path", return_value=None
        ), patch.object(
            admin_api, "_refresh_codex_model_catalog", return_value=self.catalog_path
        ), patch.object(
            admin_api, "_codex_desktop_process", return_value=None
        ), patch.object(
            admin_api, "_stop_codex_desktop"
        ), patch.object(
            admin_api, "_restart_codex_after_response"
        ):
            asyncio.run(admin_api.codex_switch_to_custom())

        raw = self.toml_path.read_bytes()
        content = raw.decode("utf-8")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(content.count('model = "gpt-5.4-mini"'), 1)
        self.assertNotIn("model_provider =", content)
        self.assertIn('openai_base_url = "http://127.0.0.1:8765/v1"', content)
        self.assertIn("model_catalog_json =", content)
        self.assertNotIn("model_context_window =", content)
        self.assertNotIn("model_auto_compact_token_limit =", content)

    def test_codex_bridge_settings_file_is_created_with_defaults(self):
        with patch.object(admin_api, "_codex_bridge_settings_path", return_value=self.bridge_settings_path):
            settings = admin_api._load_codex_bridge_settings()

        self.assertEqual(settings["tool_output_token_limit"], 12000)
        content = self.bridge_settings_path.read_text(encoding="utf-8")
        self.assertNotIn("model_context_window:", content)
        self.assertNotIn("model_auto_compact_token_limit:", content)
        self.assertIn("tool_output_token_limit: 12000", content)

    def test_codex_bridge_settings_are_user_scoped(self):
        home = Path(self.temp_dir.name) / "home"
        with patch.object(admin_api.Path, "home", return_value=home):
            path = admin_api._codex_bridge_settings_path()

        self.assertEqual(
            path,
            home / ".codex" / "lan-bridge" / "codex-bridge-settings.yaml",
        )

    def test_legacy_codex_bridge_settings_are_migrated(self):
        legacy_path = Path(self.temp_dir.name) / "legacy" / "codex-bridge-settings.yaml"
        target_path = Path(self.temp_dir.name) / "new" / "codex-bridge-settings.yaml"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text("tool_output_token_limit: 24000\n", encoding="utf-8")

        with patch.object(
            admin_api, "_codex_bridge_settings_path", return_value=target_path
        ), patch.object(
            admin_api, "_legacy_codex_bridge_settings_path", return_value=legacy_path
        ):
            settings = admin_api._load_codex_bridge_settings()

        self.assertEqual(settings["tool_output_token_limit"], 24000)
        self.assertEqual(target_path.read_text(encoding="utf-8"), legacy_path.read_text(encoding="utf-8"))

    def test_desktop_process_ignores_internal_app_server(self):
        output = json.dumps(
            [
                {
                    "ProcessId": 111,
                    "ExecutablePath": r"C:\Apps\Codex\app\resources\codex.exe",
                    "CommandLine": r'"C:\Apps\Codex\app\resources\codex.exe" app-server',
                },
                {
                    "ProcessId": 222,
                    "ExecutablePath": r"C:\Apps\Codex\app\Codex.exe",
                    "CommandLine": r'"C:\Apps\Codex\app\Codex.exe"',
                },
            ]
        )
        completed = SimpleNamespace(returncode=0, stdout=output)
        with patch.object(admin_api.subprocess, "run", return_value=completed):
            process = admin_api._codex_desktop_process()

        self.assertEqual(process, (222, r"C:\Apps\Codex\app\Codex.exe"))

    def test_slots_are_inferred_from_existing_model_mapping(self):
        config = self._config()
        config.model_mapping = {
            "gpt-5.5": {"target": "deepseek-v4-pro", "provider": "deepseek"},
            "deepseek-v4-pro-responses": {"target": "deepseek-v4-pro", "provider": "deepseek", "wire_api": "responses", "is_reasoning_text": True},
            "gpt-5": {"target": "qwen3.6-plus", "provider": "qwen", "is_multimodal": True},
        }
        config.providers = {
            "deepseek": {"adapter": "deepseek", "base_url": "https://api.deepseek.com", "api_key": "key"},
            "qwen": {"adapter": "qwen", "base_url": "https://example.invalid/v1", "api_key": "key"},
        }
        with patch.object(admin_api, "get_config", return_value=config):
            response = asyncio.run(admin_api.list_slots())

        slots = {slot["slot_id"]: slot for slot in response["slots"]}
        self.assertEqual(slots["text"]["target_model"], "deepseek-v4-pro")
        self.assertEqual(slots["text"]["provider"], "deepseek")
        self.assertEqual(slots["vision"]["target_model"], "qwen3.6-plus")
        self.assertEqual(slots["vision"]["provider"], "qwen")
        self.assertEqual(slots["responses"]["alias"], "deepseek-v4-pro-responses")
        self.assertEqual(slots["responses"]["wire_api"], "responses")
        self.assertEqual(slots["text"]["alias"], "gpt-5.5")

    def test_slot_selects_existing_model_without_removing_previous_mapping(self):
        config = self._config()
        mapping = {
            "old-text": {"target": "old-model", "provider": "deepseek"},
            "new-text": {"target": "new-model", "provider": "deepseek"},
        }
        config.model_mapping = mapping
        config.providers = {"deepseek": {"adapter": "deepseek", "api_key": "key"}}
        config._data["model_mapping"] = mapping
        config._data["providers"] = config.providers
        config._data["model_slots"] = {
            "text": {"alias": "old-text", "target": "old-model", "provider": "deepseek", "enabled": True},
        }
        config.save = Mock()
        registry = SimpleNamespace(list=lambda: ["deepseek"])

        with patch.object(admin_api, "get_config", return_value=config), patch.object(
            admin_api, "get_registry", return_value=registry
        ):
            response = asyncio.run(admin_api.update_slot("text", {
                "alias": "new-text",
                "target_model": "new-model",
                "provider": "deepseek",
                "adapter": "deepseek",
            }))

        self.assertEqual(response["status"], "ok")
        self.assertIn("old-text", mapping)
        self.assertIn("new-text", mapping)
        self.assertEqual(config._data["model_slots"]["text"]["alias"], "new-text")
        self.assertEqual(config.providers["deepseek"]["adapter"], "deepseek")
        config.save.assert_called_once()

    def test_model_list_exposes_reasoning_capability(self):
        config = self._config()
        config.native_models = {}
        config.model_mapping = {
            "deepseek-v4-pro": {
                "target": "deepseek-v4-pro",
                "provider": "deepseek",
                "is_reasoning_text": True,
            },
        }
        config.providers = {
            "deepseek": {"adapter": "openai", "base_url": "https://api.deepseek.com"},
        }
        registry = SimpleNamespace(list=lambda: ["openai"])

        with patch.object(admin_api, "get_config", return_value=config), patch.object(
            admin_api, "get_registry", return_value=registry
        ):
            response = asyncio.run(admin_api.list_models())

        self.assertTrue(response["models"][0]["is_reasoning_text"])

    def test_model_list_exposes_effective_context_defaults_without_persisting_them(self):
        config = self._config()
        config.native_models = {}
        config.model_mapping = {
            "deepseek-v4-pro": {
                "target": "deepseek-v4-pro",
                "provider": "deepseek",
                "capabilities": {},
            },
        }
        config.providers = {"deepseek": {"adapter": "openai"}}
        registry = SimpleNamespace(list=lambda: ["openai"])

        with patch.object(admin_api, "get_config", return_value=config), patch.object(
            admin_api, "get_registry", return_value=registry
        ):
            response = asyncio.run(admin_api.list_models())

        model = response["models"][0]
        self.assertEqual(model["capabilities"], {})
        self.assertEqual(model["default_context_window"], 1048576)
        self.assertEqual(model["effective_context_window"], 1048576)
        self.assertEqual(model["default_auto_compact_token_limit"], 900000)
        self.assertEqual(model["effective_auto_compact_token_limit"], 900000)

    def test_model_update_preserves_reasoning_capability(self):
        config = self._config()
        config.model_mapping = {
            "deepseek-v4-pro": {
                "target": "deepseek-v4-pro",
                "provider": "deepseek",
                "is_reasoning_text": True,
            },
        }
        config.providers = {"deepseek": {"adapter": "openai"}}
        config._data["model_mapping"] = config.model_mapping
        config._data["providers"] = config.providers
        config.save = Mock()

        with patch.object(admin_api, "get_config", return_value=config):
            asyncio.run(admin_api.update_model("deepseek-v4-pro", {"display_name": "DeepSeek V4 Pro"}))

        self.assertTrue(config.model_mapping["deepseek-v4-pro"]["is_reasoning_text"])

    def test_model_update_saves_model_wire_api_without_changing_shared_provider(self):
        config = self._config()
        config.model_mapping = {
            "grok-build-0.1": {
                "target": "grok-build-0.1",
                "provider": "grok",
            },
        }
        config.providers = {"grok": {"adapter": "openai"}}
        config._data["model_mapping"] = config.model_mapping
        config._data["providers"] = config.providers
        config.save = Mock()

        with patch.object(admin_api, "get_config", return_value=config):
            asyncio.run(admin_api.update_model("grok-build-0.1", {"wire_api": "chat"}))

        self.assertEqual(config.model_mapping["grok-build-0.1"]["wire_api"], "chat")
        self.assertNotIn("wire_api", config.providers["grok"])

    def test_model_update_saves_model_proxy_settings(self):
        config = self._config()
        config.model_mapping = {
            "grok-build-0.1-xai-direct": {
                "target": "grok-build-0.1",
                "provider": "xai_direct",
            },
        }
        config.providers = {"xai_direct": {"adapter": "openai"}}
        config._data["model_mapping"] = config.model_mapping
        config._data["providers"] = config.providers
        config.save = Mock()

        with patch.object(admin_api, "get_config", return_value=config):
            asyncio.run(admin_api.update_model(
                "grok-build-0.1-xai-direct",
                {
                    "use_proxy": True,
                    "proxy_url": "http://127.0.0.1:19828",
                },
            ))

        model = config.model_mapping["grok-build-0.1-xai-direct"]
        self.assertTrue(model["use_proxy"])
        self.assertEqual(model["proxy_url"], "http://127.0.0.1:19828")
        self.assertNotIn("proxy_url", config.providers["xai_direct"])

    def test_model_update_rejects_enabled_proxy_without_url(self):
        config = self._config()
        config.model_mapping = {
            "grok": {"target": "grok", "provider": "xai_direct"},
        }
        config.providers = {"xai_direct": {"adapter": "openai"}}
        config._data["model_mapping"] = config.model_mapping
        config._data["providers"] = config.providers
        config.save = Mock()

        with patch.object(admin_api, "get_config", return_value=config):
            response = asyncio.run(admin_api.update_model(
                "grok",
                {"use_proxy": True, "proxy_url": ""},
            ))

        self.assertEqual(response[1], 400)
        self.assertIn("代理地址", response[0]["error"])
        config.save.assert_not_called()

    def test_image_slot_connection_test_omits_size_for_ark_defaults(self):
        config = self._config()
        config.model_mapping = {
            "dall-e-3": {
                "target": "doubao-seedream-4-5-251128",
                "provider": "ark_image",
                "is_image_gen": True,
            }
        }
        config.providers = {
            "ark_image": {
                "adapter": "openai",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": "key",
            }
        }
        config._data["model_mapping"] = config.model_mapping
        config._data["providers"] = config.providers
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "{}"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        with patch.object(admin_api, "get_config", return_value=config), patch.object(
            admin_api.httpx, "AsyncClient", return_value=FakeClient()
        ):
            response = asyncio.run(admin_api.test_connection("dall-e-3"))

        self.assertEqual(response["status"], "ok")
        self.assertEqual(captured["url"], "https://ark.cn-beijing.volces.com/api/v3/images/generations")
        self.assertNotIn("size", captured["json"])

    def test_slot_connection_test_preserves_provider_adapter(self):
        config = self._config()
        config.model_mapping = {
            "dall-e-3": {
                "target": "qwen-image",
                "provider": "qwen",
                "is_image_gen": True,
            }
        }
        config._data["model_mapping"] = config.model_mapping
        config._data["model_slots"] = {
            "image_gen": {
                "alias": "dall-e-3",
                "target": "qwen-image",
                "provider": "qwen",
                "enabled": True,
            }
        }
        config.providers = {
            "qwen": {
                "adapter": "qwen",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "key",
            }
        }
        config._data["providers"] = config.providers
        captured = {}

        class FakeResponse:
            status_code = 200
            text = "{}"

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResponse()

        with patch.object(admin_api, "get_config", return_value=config), patch.object(
            admin_api.httpx, "AsyncClient", return_value=FakeClient()
        ):
            response = asyncio.run(admin_api.test_slot("image_gen", {"provider": "qwen"}))

        self.assertEqual(response["status"], "ok")
        self.assertEqual(
            captured["url"],
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        )
        self.assertIn("input", captured["json"])
        self.assertNotIn("prompt", captured["json"])

    def test_packaged_codex_restarts_through_app_activation(self):
        process = (
            123,
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.519.5221.0_x64__2p2nqsd0c76g0\app\Codex.exe",
        )
        with patch.object(admin_api.subprocess, "Popen") as popen:
            restarted = admin_api._restart_codex_desktop(process, "http://127.0.0.1:7890")

        self.assertTrue(restarted)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "explorer.exe")
        self.assertEqual(command[1], r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App")

    def test_packaged_chatgpt_restarts_through_codex_app_activation(self):
        process = (
            123,
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.8479.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe",
        )
        with patch.object(admin_api.subprocess, "Popen") as popen:
            restarted = admin_api._restart_codex_desktop(process)

        self.assertTrue(restarted)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "explorer.exe")
        self.assertEqual(command[1], r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App")

    def test_restore_official_never_attempts_process_control(self):
        with patch.object(admin_api, "_CODEX_TOML", self.toml_path), patch.object(
            admin_api, "get_config", return_value=self._config("http://127.0.0.1:19828")
        ), patch.object(
            admin_api, "_codex_desktop_process"
        ), patch.object(
            admin_api, "_stop_codex_desktop"
        ) as stop, patch.object(
            admin_api.subprocess, "run"
        ), patch.object(
            admin_api, "_restart_codex_after_response"
        ) as restart:
            response = asyncio.run(admin_api.codex_switch_to_official())

        content = self.toml_path.read_text(encoding="utf-8")
        self.assertNotIn("proxy_url", content)
        self.assertFalse(response["using_bridge"])
        stop.assert_not_called()
        restart.assert_not_called()

    def test_restore_official_recovers_from_malformed_toml(self):
        self.toml_path.write_text(
            'model_provider = "custom"\n[broken\nproxy_url = "http://bad.invalid"\n',
            encoding="utf-8",
        )
        with patch.object(admin_api, "_CODEX_TOML", self.toml_path), patch.object(
            admin_api, "get_config", return_value=self._config("http://bad.invalid")
        ):
            response = asyncio.run(admin_api.codex_switch_to_official())

        content = self.toml_path.read_text(encoding="utf-8")
        self.assertEqual(content, "# Emergency reset by LAN BRIDGE: use Codex built-in OpenAI defaults.\n")
        self.assertEqual(response["status"], "ok")
        self.assertFalse(response["preserved_settings"])
        backups = list((self.toml_path.parent / "backups").glob("config.toml.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertIn("[broken", backups[0].read_text(encoding="utf-8"))

    def test_restore_official_recovers_from_invalid_utf8(self):
        original = b'\xff\xfemodel_provider = "custom"\n'
        self.toml_path.write_bytes(original)

        with patch.object(admin_api, "_CODEX_TOML", self.toml_path):
            response = asyncio.run(admin_api.codex_switch_to_official())

        self.assertEqual(response["status"], "ok")
        self.assertFalse(response["preserved_settings"])
        self.assertEqual(
            self.toml_path.read_text(encoding="utf-8"),
            "# Emergency reset by LAN BRIDGE: use Codex built-in OpenAI defaults.\n",
        )
        backups = list((self.toml_path.parent / "backups").glob("config.toml.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_toml_atomic_replace_failure_preserves_original(self):
        original = self.toml_path.read_bytes()

        with patch("code_cn_bridge.admin_api.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                admin_api._write_toml_lines(self.toml_path, ["model = \"gpt-5.6-sol\""])

        self.assertEqual(self.toml_path.read_bytes(), original)
        self.assertEqual(list(self.toml_path.parent.glob(".config.toml.*.tmp")), [])

    def test_toml_backup_retention_is_bounded(self):
        with patch.object(admin_api, "_CODEX_TOML", self.toml_path):
            for index in range(admin_api._CODEX_CONFIG_BACKUP_LIMIT + 3):
                self.toml_path.write_text(f'model_provider = "custom-{index}"\n', encoding="utf-8")
                asyncio.run(admin_api.codex_switch_to_official())

        backups = list((self.toml_path.parent / "backups").glob("config.toml.*.bak"))
        self.assertEqual(len(backups), admin_api._CODEX_CONFIG_BACKUP_LIMIT)

    def test_web_search_endpoint_returns_enabled_bocha_without_exposing_key(self):
        config = self._config()
        config._data["web_search"] = {
            "enabled": True,
            "active_provider": "bocha",
            "providers": {
                "bocha": {
                    "adapter": "bocha",
                    "enabled": True,
                    "api_key": "secret",
                    "base_url": "https://api.bocha.cn/v1/web-search",
                }
            },
        }
        with patch.object(admin_api, "get_config", return_value=config):
            response = asyncio.run(admin_api.get_web_search_settings())

        self.assertTrue(response["enabled"])
        self.assertEqual(response["active_provider"], "bocha")
        self.assertTrue(response["providers"]["bocha"]["api_key_set"])
        self.assertNotIn("api_key", response["providers"]["bocha"])

    def test_web_search_update_preserves_key_when_form_leaves_it_blank(self):
        config = self._config()
        config.save = Mock()
        config._data["web_search"] = {
            "enabled": True,
            "active_provider": "bocha",
            "providers": {"bocha": {"api_key": "secret", "enabled": True}},
        }
        with patch.object(admin_api, "get_config", return_value=config):
            response = asyncio.run(admin_api.update_web_search_settings({
                "enabled": True,
                "active_provider": "bocha",
                "provider": {"api_key": "", "summary": False},
            }))

        self.assertTrue(response["web_search"]["enabled"])
        self.assertEqual(config._data["web_search"]["providers"]["bocha"]["api_key"], "secret")
        config.save.assert_called_once()

    def test_web_search_connection_test_preserves_saved_key_when_form_leaves_it_blank(self):
        config = self._config()
        config._data["web_search"] = {
            "enabled": True,
            "active_provider": "bocha",
            "providers": {
                "bocha": {
                    "api_key": "secret",
                    "base_url": "https://example.test/search",
                    "timeout": 30,
                }
            },
        }

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"data": {"webPages": {"value": []}}}

        class FakeClient:
            last_headers = {}

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url, json, headers):
                FakeClient.last_headers = headers
                return FakeResponse()

        with patch.object(admin_api, "get_config", return_value=config), patch.object(
            admin_api.httpx, "AsyncClient", FakeClient
        ):
            response = asyncio.run(admin_api.test_web_search({
                "active_provider": "bocha",
                "provider": {"api_key": ""},
                "query": "OpenAI",
            }))

        self.assertEqual(response["status"], "ok")
        self.assertEqual(FakeClient.last_headers["Authorization"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
