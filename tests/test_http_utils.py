import ssl
import types
import unittest
from unittest.mock import patch

from code_cn_bridge.http_utils import (
    _ssl_verify,
    make_async_client,
    make_native_async_client,
    make_provider_async_client,
)


class HttpUtilsTests(unittest.TestCase):
    def test_direct_client_forces_environment_proxy_bypass(self):
        with patch("code_cn_bridge.http_utils.httpx.AsyncClient") as client_class:
            make_async_client(timeout=30, trust_env=True)

        self.assertFalse(client_class.call_args.kwargs["trust_env"])
        self.assertNotIn("proxy", client_class.call_args.kwargs)

    def test_direct_client_rejects_explicit_proxy(self):
        with self.assertRaisesRegex(ValueError, "cannot use an HTTP proxy"):
            make_async_client(proxy="http://127.0.0.1:19828")

    def test_native_client_uses_only_explicit_proxy_url(self):
        with patch("code_cn_bridge.http_utils.httpx.AsyncClient") as client_class:
            make_native_async_client(timeout=30, proxy_url="http://127.0.0.1:19828")

        kwargs = client_class.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertEqual(kwargs["proxy"], "http://127.0.0.1:19828")

    def test_provider_client_uses_only_explicit_provider_proxy(self):
        with patch("code_cn_bridge.http_utils.httpx.AsyncClient") as client_class:
            make_provider_async_client(
                timeout=30,
                proxy_url="http://127.0.0.1:19828",
            )

        kwargs = client_class.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertEqual(kwargs["proxy"], "http://127.0.0.1:19828")

    def test_provider_client_stays_direct_without_provider_proxy(self):
        with patch("code_cn_bridge.http_utils.httpx.AsyncClient") as client_class:
            make_provider_async_client(timeout=30)

        kwargs = client_class.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertNotIn("proxy", kwargs)

    def test_ssl_verify_falls_back_when_certifi_path_is_missing(self):
        fake_certifi = types.SimpleNamespace(where=lambda: r"C:\missing\cacert.pem")

        with patch.dict("sys.modules", {"certifi": fake_certifi}):
            verify = _ssl_verify()

        self.assertIsInstance(verify, ssl.SSLContext)
