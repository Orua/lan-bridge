"""HTTP 客户端 —— 异步转发请求到国产模型 API"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import AsyncIterator

import httpx

from .adapters.base import BaseAdapter
from .http_utils import make_provider_async_client as make_async_client


logger = logging.getLogger("lan-bridge.client")


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield complete SSE data payloads, including recoverable multiline JSON."""
    data_lines: list[str] = []

    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if field == "data" and separator:
            data_lines.append(value[1:] if value.startswith(" ") else value)
            continue

        # Some compatible providers emit a literal newline inside a JSON
        # string. It is invalid SSE/JSON, but retaining the continuation lets
        # json.loads(strict=False) recover it before we serialize it cleanly.
        if data_lines and field not in {"event", "id", "retry"}:
            data_lines.append(line)

    if data_lines:
        yield "\n".join(data_lines)


def _decode_sse_json(data: str) -> dict:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as strict_error:
        try:
            payload = json.loads(data, strict=False)
        except json.JSONDecodeError as relaxed_error:
            encoded = data.encode("utf-8", errors="replace")
            digest = hashlib.sha256(encoded).hexdigest()[:16]
            raise ValueError(
                f"Malformed upstream SSE JSON (bytes={len(encoded)}, sha256={digest})"
            ) from relaxed_error
        logger.warning(
            "Recovered upstream SSE JSON containing unescaped control characters bytes=%d",
            len(data.encode("utf-8", errors="replace")),
        )

    if not isinstance(payload, dict):
        raise ValueError(f"Upstream SSE payload must be a JSON object, got {type(payload).__name__}")
    return payload


class UpstreamClient:
    """上游模型 API 异步客户端"""

    def __init__(
        self,
        adapter: BaseAdapter,
        api_key: str,
        timeout: float = 120.0,
        stream_timeout: float = 600.0,
        chat_url: str | None = None,
        proxy_url: str = "",
    ):
        self.adapter = adapter
        self.api_key = api_key
        self._chat_url = chat_url or adapter.build_chat_url()
        self._client: httpx.AsyncClient | None = None
        self._stream_client: httpx.AsyncClient | None = None
        self._timeout = timeout
        self._stream_timeout = stream_timeout
        self._proxy_url = proxy_url

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = make_async_client(
                proxy_url=self._proxy_url,
                timeout=httpx.Timeout(self._timeout),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return self._client

    async def _get_stream_client(self) -> httpx.AsyncClient:
        """流式请求专用客户端 —— 读超时更长，容忍模型长时间推理"""
        if self._stream_client is None:
            self._stream_client = make_async_client(
                proxy_url=self._proxy_url,
                timeout=httpx.Timeout(connect=30.0, read=self._stream_timeout, write=30.0, pool=30.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return self._stream_client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._stream_client:
            await self._stream_client.aclose()
            self._stream_client = None

    async def chat_completion(self, chat_req: dict) -> dict:
        """发送非流式 Chat Completions 请求"""
        client = await self._get_client()
        headers = self.adapter.get_headers(self.api_key)

        response = await client.post(self._chat_url, json=chat_req, headers=headers)
        if response.status_code >= 400:
            body = await response.aread()
            raise httpx.HTTPStatusError(
                f"Upstream {response.status_code}: {body.decode()[:500]}",
                request=response.request,
                response=response,
            )
        return response.json()

    async def chat_completion_stream(self, chat_req: dict) -> AsyncIterator[dict]:
        """发送流式 Chat Completions 请求，返回 SSE 事件迭代器"""
        client = await self._get_stream_client()
        headers = self.adapter.get_headers(self.api_key)
        chat_req["stream"] = True

        async with client.stream("POST", self._chat_url, json=chat_req, headers=headers) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise httpx.HTTPStatusError(
                    f"Upstream {response.status_code}: {body.decode()[:500]}",
                    request=response.request,
                    response=response,
                )
            async for data_str in _iter_sse_data(response):
                if data_str.strip() == "[DONE]":
                    break
                yield _decode_sse_json(data_str)


class UpstreamClientPool:
    """Reuse upstream HTTP connections across Codex tool turns."""

    def __init__(self):
        self._clients: dict[tuple, UpstreamClient] = {}

    def get(
        self,
        provider_name: str,
        adapter: BaseAdapter,
        api_key: str,
        timeout: float,
        stream_timeout: float,
        proxy_url: str = "",
    ) -> UpstreamClient:
        chat_url = adapter.build_chat_url()
        key = (provider_name, adapter.name, chat_url, api_key, timeout, stream_timeout, proxy_url)
        client = self._clients.get(key)
        if client is None:
            client = UpstreamClient(
                adapter,
                api_key,
                timeout=timeout,
                stream_timeout=stream_timeout,
                chat_url=chat_url,
                proxy_url=proxy_url,
            )
            self._clients[key] = client
        return client

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()


_client_pool = UpstreamClientPool()


def get_upstream_client(
    provider_name: str,
    adapter: BaseAdapter,
    api_key: str,
    timeout: float = 120.0,
    stream_timeout: float = 600.0,
    proxy_url: str = "",
) -> UpstreamClient:
    return _client_pool.get(
        provider_name,
        adapter,
        api_key,
        timeout,
        stream_timeout,
        proxy_url,
    )


async def close_upstream_clients() -> None:
    await _client_pool.close()
