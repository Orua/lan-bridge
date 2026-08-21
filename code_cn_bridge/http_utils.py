"""Shared HTTP client helpers."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx


def _ssl_verify() -> str | ssl.SSLContext:
    """Use certifi when bundled, otherwise fall back to the OS default store."""
    try:
        import certifi

        cafile = certifi.where()
        if cafile and Path(cafile).is_file():
            return cafile
    except Exception:
        pass
    return ssl.create_default_context()


def make_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create a direct client for every non-native provider call.

    This is intentionally strict: custom model, media, search, test, and
    download traffic must never inherit or explicitly select a VPN proxy.
    """
    if kwargs.get("proxy") is not None or kwargs.get("proxies") is not None:
        raise ValueError("Direct provider clients cannot use an HTTP proxy")
    kwargs.pop("proxy", None)
    kwargs.pop("proxies", None)
    kwargs["trust_env"] = False
    kwargs.setdefault("verify", _ssl_verify())
    return httpx.AsyncClient(**kwargs)


def make_provider_async_client(*, proxy_url: str = "", **kwargs: Any) -> httpx.AsyncClient:
    """Create a provider client with an explicit, provider-scoped proxy.

    Custom providers remain direct by default. A proxy is used only when the
    selected provider explicitly configures one, so domestic providers never
    inherit the machine-wide VPN settings.
    """
    if "proxy" in kwargs or "proxies" in kwargs:
        raise ValueError("Pass the provider proxy through proxy_url only")
    kwargs["trust_env"] = False
    kwargs.setdefault("verify", _ssl_verify())
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**kwargs)


def make_native_async_client(*, proxy_url: str = "", **kwargs: Any) -> httpx.AsyncClient:
    """Create the isolated official OpenAI client with an optional VPN proxy."""
    if "proxy" in kwargs or "proxies" in kwargs:
        raise ValueError("Pass the official OpenAI proxy through proxy_url only")
    kwargs["trust_env"] = False
    kwargs.setdefault("verify", _ssl_verify())
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**kwargs)
