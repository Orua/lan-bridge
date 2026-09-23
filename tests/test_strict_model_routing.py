import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from code_cn_bridge.admin_api import _find_provider_for_target
from code_cn_bridge.config import Config
from code_cn_bridge.routing import resolve_route
from code_cn_bridge import server


class StrictModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump({
                "providers": {
                    "deepseek": {
                        "adapter": "deepseek",
                        "api_key": "test-key",
                        "enabled": True,
                    },
                    "ai.licco.top": {
                        "adapter": "openai",
                        "api_key": "unused-key",
                        "enabled": True,
                    },
                },
                "model_mapping": {
                    "deepseek-v4-pro": {
                        "provider": "deepseek",
                        "target": "deepseek-reasoner",
                        "enabled": True,
                    },
                },
                "model_slots": {
                    "reasoning_text": {
                        "alias": "deepseek-v4-pro",
                        "provider": "deepseek",
                        "target": "deepseek-reasoner",
                        "enabled": True,
                    },
                },
                "native_models": {
                    "gpt-5.6-sol": {"enabled": True},
                    "gpt-disabled": {"enabled": False},
                },
            }),
            encoding="utf-8",
        )
        self.config = Config(self.config_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_reasoning_alias_resolves_to_current_slot(self):
        self.assertEqual(
            self.config.resolve_model("gpt-5.5-reasoning"),
            ("deepseek", "deepseek-reasoner"),
        )

        route = resolve_route(self.config, "gpt-5.5-reasoning")

        self.assertEqual(route.kind, "custom")
        self.assertEqual(route.provider, "deepseek")
        self.assertEqual(route.target_model, "deepseek-reasoner")

    def test_existing_native_model_inherits_default_media_capabilities(self):
        native = self.config.native_models["gpt-5.6-sol"]

        self.assertTrue(native["capabilities"]["vision"])
        self.assertTrue(native["capabilities"]["image_generation"])
        self.assertFalse(self.config.native_models["gpt-disabled"]["enabled"])
        self.assertIn("gpt-6-astra", self.config.native_models)
        self.assertIn("gpt-6-sol", self.config.native_models)
        self.assertIn("gpt-6-luna", self.config.native_models)

    def test_responses_slot_route_preserves_model_wire_metadata(self):
        self.config.data["model_mapping"]["deepseek-v4-pro-responses"] = {
            "provider": "deepseek",
            "target": "deepseek-reasoner",
            "wire_api": "responses",
            "enabled": True,
        }
        self.config.data["model_slots"]["responses"] = {
            "alias": "deepseek-v4-pro-responses",
            "provider": "deepseek",
            "target": "deepseek-reasoner",
            "enabled": True,
        }

        route = resolve_route(self.config, "deepseek-v4-pro-responses")

        self.assertTrue(route.metadata["slot_alias"])
        self.assertEqual(route.metadata["wire_api"], "responses")

    def test_unknown_model_never_uses_first_provider(self):
        self.assertEqual(
            self.config.resolve_model("gpt-never-configured"),
            ("unknown", "gpt-never-configured"),
        )

        route = resolve_route(self.config, "gpt-never-configured")

        self.assertEqual(route.kind, "native_codex")
        self.assertEqual(route.target_model, "gpt-never-configured")
        self.assertNotEqual(route.provider, "ai.licco.top")

    def test_new_native_model_needs_no_registry_entry(self):
        route = resolve_route(self.config, "gpt-6-astra-preview")
        self.assertEqual(route.kind, "native_codex")
        self.assertEqual(route.target_model, "gpt-6-astra-preview")

    def test_only_enabled_native_models_are_routed_natively(self):
        self.assertEqual(resolve_route(self.config, "gpt-5.6-sol").kind, "native_codex")
        disabled = resolve_route(self.config, "gpt-disabled")
        self.assertEqual(disabled.kind, "custom")
        self.assertEqual(disabled.provider, "unknown")

    def test_provider_inference_has_no_first_provider_fallback(self):
        self.assertIsNone(self.config._find_provider_for_target("unrelated-model"))
        self.assertIsNone(
            _find_provider_for_target("unrelated-model", self.config.providers)
        )

    def test_responses_unknown_model_falls_back_to_only_configured_text_model(self):
        expected = (object(), "deepseek", "deepseek-reasoner", "test-key")
        with patch.object(server, "_get_adapter_for_model", return_value=expected) as resolver:
            result = server._text_route_for_responses_request(
                "gpt-never-configured",
                {"input": [], "stream": False},
                self.config,
            )

        self.assertEqual(result, expected)
        resolver.assert_called_once_with("deepseek-v4-pro")

    def test_responses_unknown_model_errors_when_no_text_model_exists(self):
        self.config.data["model_mapping"] = {}
        self.config.data["model_slots"] = {}

        with self.assertRaisesRegex(ValueError, "未找到可用的文本模型配置"):
            server._text_route_for_responses_request(
                "gpt-never-configured",
                {"input": [], "stream": False},
                self.config,
            )

    def test_model_proxy_is_scoped_to_the_requested_alias(self):
        self.config.data["model_mapping"].update({
            "direct-alias": {
                "provider": "deepseek",
                "target": "shared-target",
                "use_proxy": False,
            },
            "proxied-alias": {
                "provider": "deepseek",
                "target": "shared-target",
                "use_proxy": True,
                "proxy_url": "http://127.0.0.1:19828",
            },
        })

        self.assertEqual(
            server._model_proxy_url(self.config, alias="direct-alias"),
            "",
        )
        self.assertEqual(
            server._model_proxy_url(self.config, alias="proxied-alias"),
            "http://127.0.0.1:19828",
        )

    def test_ambiguous_internal_route_never_borrows_another_model_proxy(self):
        self.config.data["model_mapping"].update({
            "direct-alias": {
                "provider": "deepseek",
                "target": "shared-target",
                "use_proxy": False,
            },
            "proxied-alias": {
                "provider": "deepseek",
                "target": "shared-target",
                "use_proxy": True,
                "proxy_url": "http://127.0.0.1:19828",
            },
        })

        self.assertEqual(
            server._model_proxy_url(
                self.config,
                provider_name="deepseek",
                target_model="shared-target",
            ),
            "",
        )

    def test_routed_model_uses_its_own_proxy_instead_of_original_alias(self):
        self.config.data["model_mapping"].update({
            "text-alias": {
                "provider": "deepseek",
                "target": "text-target",
                "use_proxy": False,
            },
            "vision-alias": {
                "provider": "xai",
                "target": "vision-target",
                "use_proxy": True,
                "proxy_url": "http://127.0.0.1:19828",
            },
        })

        self.assertEqual(
            server._model_proxy_url(
                self.config,
                alias="text-alias",
                provider_name="xai",
                target_model="vision-target",
            ),
            "http://127.0.0.1:19828",
        )


if __name__ == "__main__":
    unittest.main()
