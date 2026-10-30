"""Shared HTTP client helpers."""

from __future__ import annotations

import logging
import ssl
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import anyio


logger = logging.getLogger("lan-bridge.http")


async def send_with_connect_retries(
    client: httpx.AsyncClient, request: httpx.Request, *, stream: bool = False,
) -> httpx.Response:
    """Retry connection setup only; never replay a request after writes/reads."""
    previous_trace = request.extensions.get("trace")
    stage = "connection_setup"

    async def trace(name, info):
        nonlocal stage
        if name.endswith(".started"):
            stage = name.removesuffix(".started")
        if previous_trace is not None:
            await previous_trace(name, info)

    request.extensions["trace"] = trace
    try:
        for attempt in range(3):
            stage = "connection_setup"
            try:
                return await client.send(request, stream=stream)
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                exc.bridge_connect_stage = stage
                if attempt == 2:
                    logger.warning("Upstream connection exhausted host=%s stage=%s error=%s", request.url.host, stage, type(exc).__name__)
                    raise
                delay = 0.5 * (2 ** attempt)
                logger.warning(
                    "Upstream connection retry=%d/2 delay=%.1fs host=%s stage=%s error=%s",
                    attempt + 1, delay, request.url.host, stage, type(exc).__name__,
                )
                await anyio.sleep(delay)
    finally:
        if previous_trace is None:
            request.extensions.pop("trace", None)
        else:
            request.extensions["trace"] = previous_trace
    raise AssertionError("unreachable")


@asynccontextmanager
async def stream_with_connect_retries(client: httpx.AsyncClient, request: httpx.Request):
    response = await send_with_connect_retries(client, request, stream=True)
    try:
        yield response
    finally:
        with anyio.CancelScope(shield=True):
            await response.aclose()


async def close_http_client(client: httpx.AsyncClient, response: httpx.Response | None = None) -> None:
    """Release both resources even inside Starlette's cancelled stream scope."""
    with anyio.CancelScope(shield=True):
        try:
            if response is not None:
                await response.aclose()
        finally:
            await client.aclose()


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
