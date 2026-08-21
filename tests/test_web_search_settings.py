import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from code_cn_bridge import admin_api
from code_cn_bridge.config import Config
from code_cn_bridge.web_search import search_web, test_web_search_provider


class WebSearchConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tempdir.name) / "config.yaml"
        self.cfg = Config(self.config_path)

    def tearDown(self):
        self.tempdir.cleanup()

    async def test_ui_api_key_is_saved_but_removed_from_export(self):
        with patch.object(admin_api, "get_config", return_value=self.cfg):
            result = await admin_api.update_web_search_settings({
                "enabled": True,
                "active_provider": "bocha",
                "provider": {
                    "adapter": "bocha",
                    "base_url": "https://example.test/search",
                    "api_key": "secret-ui-key",
                },
            })
            exported = await admin_api.export_config()

        self.assertEqual(result["status"], "ok")
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["web_search"]["providers"]["bocha"]["api_key"],
            "secret-ui-key",
        )
        self.assertNotIn("secret-ui-key", exported["yaml"])

    async def test_environment_api_key_is_not_written_to_yaml(self):
        self.config_path.write_text(
            "web_search:\n  providers:\n    bocha:\n      api_key_env: BOCHA_TEST_KEY\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"BOCHA_TEST_KEY": "secret-env-key"}):
            cfg = Config(self.config_path)
            cfg.save()

        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("api_key", persisted["web_search"]["providers"]["bocha"])


class _FakeResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "webPages": {
                "value": [
                    {"name": "Example result", "url": "https://example.test", "snippet": "Found"},
                ],
            },
        }


class _FakeClient:
    last_request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json, headers):
        _FakeClient.last_request = (url, json, headers)
        return _FakeResponse()


class _EmptyResultResponse(_FakeResponse):
    @staticmethod
    def json():
        return {"webPages": {"value": []}}


class _EmptyResultClient(_FakeClient):
    async def post(self, url, json, headers):
        _EmptyResultClient.last_request = (url, json, headers)
        return _EmptyResultResponse()


class _RejectedResultResponse(_FakeResponse):
    @staticmethod
    def json():
        return {"code": 403, "msg": "Query not allowed", "log_id": "log-rejected", "data": None}


class _RejectedResultClient(_FakeClient):
    async def post(self, url, json, headers):
        return _RejectedResultResponse()


class BochaConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_bocha_request_uses_configured_endpoint_and_normalizes_results(self):
        provider = {
            "adapter": "bocha",
            "base_url": "https://example.test/search",
            "api_key": "do-not-return",
            "max_results": 3,
            "summary": True,
            "freshness": "oneWeek",
        }
        with patch("code_cn_bridge.web_search.httpx.AsyncClient", _FakeClient):
            result = await test_web_search_provider(provider, "bridge")

        url, body, headers = _FakeClient.last_request
        self.assertEqual(url, "https://example.test/search")
        self.assertEqual(body["query"], "bridge")
        self.assertEqual(headers["Authorization"], "Bearer do-not-return")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["title"], "Example result")
        self.assertNotIn("do-not-return", str(result))

    async def test_successful_empty_bocha_response_is_not_an_error(self):
        provider = {
            "adapter": "bocha",
            "base_url": "https://example.test/search",
            "api_key": "do-not-return",
        }
        with patch("code_cn_bridge.web_search.httpx.AsyncClient", _EmptyResultClient):
            result = await search_web(provider, "no matching query")

        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["results"], [])

    async def test_provider_business_rejection_is_preserved(self):
        provider = {
            "adapter": "bocha",
            "base_url": "https://example.test/search",
            "api_key": "do-not-return",
        }
        with patch("code_cn_bridge.web_search.httpx.AsyncClient", _RejectedResultClient):
            result = await search_web(provider, "blocked query")

        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["message"], "Query not allowed")


if __name__ == "__main__":
    unittest.main()
