"""Credential-isolated Responses API passthrough for custom providers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi.responses import Response, StreamingResponse

from .adapters.base import BaseAdapter
from .http_utils import make_provider_async_client as make_async_client
from .native_proxy import (
    _native_context_put,
    _response_headers,
    prepare_provider_responses_payload,
)


_MAX_UPSTREAM_ERROR_DETAIL = 2048


def _usage_trace_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Extract provider-reported usage without changing the Responses payload."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return {}
    total = usage.get("total_tokens")
    if not isinstance(total, int):
        total = sum(
            value for value in (usage.get("input_tokens"), usage.get("output_tokens"))
            if isinstance(value, int)
        )
    return {"usage": usage, "tokens": total}


class ProviderResponsesHTTPError(RuntimeError):
    """A non-success response from a custom Responses endpoint."""

    def __init__(self, provider: str, status_code: int, body: bytes):
        text = " ".join(body.decode("utf-8", errors="replace").strip().split())
        if len(text) > _MAX_UPSTREAM_ERROR_DETAIL:
            text = text[:_MAX_UPSTREAM_ERROR_DETAIL] + "...[truncated]"
        super().__init__(f"{provider} Responses upstream HTTP {status_code}: {text or 'No response body returned'}")
        self.status_code = status_code


def provider_uses_responses(provider_name: str, provider: dict[str, Any]) -> bool:
    """DeepSeek defaults to Responses; other providers must opt in explicitly."""
    configured = str(provider.get("wire_api") or provider.get("protocol") or "").strip().lower()
    if configured:
        return configured in {"responses", "openai-responses"}
    return provider_name.strip().lower() == "deepseek"


def model_uses_responses(
    provider_name: str,
    provider: dict[str, Any],
    model_config: dict[str, Any] | None = None,
) -> bool:
    """Resolve a model-level wire override before the shared provider default."""
    configured = str(
        (model_config or {}).get("wire_api")
        or (model_config or {}).get("protocol")
        or ""
    ).strip().lower()
    if configured:
        return configured in {"responses", "openai-responses"}
    return provider_uses_responses(provider_name, provider)


def _responses_url(adapter: BaseAdapter) -> str:
    base = adapter.base_url.rstrip("/")
    if base.endswith("/responses"):
        return base
    return f"{base}/responses"


def _provider_headers(adapter: BaseAdapter, api_key: str) -> dict[str, str]:
    headers = dict(adapter.get_headers(api_key))
    headers["Accept"] = "text/event-stream, application/json"
    headers["Accept-Encoding"] = "identity"
    return headers


def _apply_provider_responses_compatibility(
    payload: dict[str, Any],
    provider_name: str,
) -> dict[str, Any]:
    """Drop only fields known to be rejected by an otherwise compatible endpoint."""
    if provider_name.strip().lower() == "deepseek":
        tool_choice = payload.get("tool_choice")
        if tool_choice not in (None, "auto", "none"):
            payload.pop("tool_choice", None)
    return payload


async def proxy_provider_responses(
    body: dict[str, Any],
    target_model: str,
    provider_name: str,
    provider: dict[str, Any],
    adapter: BaseAdapter,
    api_key: str,
    *,
    proxy_url: str = "",
    on_trace: Callable[[str, dict[str, Any]], None] | None = None,
    access_key_id: str = "",
) -> Response:
    """Forward Responses bytes unchanged, apart from routing/auth and cross-provider replay."""
    timeout_seconds = float(provider.get("timeout", 120))
    stream_timeout = max(float(provider.get("stream_timeout", timeout_seconds)), 600.0)
    payload, replayed = prepare_provider_responses_payload(
        body,
        target_model,
        provider_name,
        supports_previous_response_id=bool(provider.get("supports_previous_response_id", False)),
        access_key_id=access_key_id,
    )
    payload = _apply_provider_responses_compatibility(payload, provider_name)
    if on_trace is not None:
        on_trace("prepared", {
            "cross_provider_replay": replayed,
            "has_previous_response_id": bool(payload.get("previous_response_id")),
        })

    client = make_async_client(
        proxy_url=proxy_url,
        timeout=httpx.Timeout(stream_timeout, connect=30.0),
    )
    request = client.build_request(
        "POST",
        _responses_url(adapter),
        headers=_provider_headers(adapter, api_key),
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    try:
        upstream = await client.send(request, stream=True)
    except Exception:
        await client.aclose()
        raise

    if upstream.status_code >= 400:
        error_body = await upstream.aread()
        try:
            raise ProviderResponsesHTTPError(provider_name, upstream.status_code, error_body)
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = _response_headers(upstream.headers)

    async def stream_bytes() -> AsyncIterator[bytes]:
        pending = b""
        output_items: list[Any] = []
        completed = False
        usage_fields: dict[str, Any] = {}
        try:
            async for chunk in upstream.aiter_raw():
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if not line.startswith(b"data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == b"[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict):
                        output_items.append(event["item"])
                    response = event.get("response")
                    if event.get("type") == "response.completed" and isinstance(response, dict):
                        completed = True
                        usage_fields = _usage_trace_fields(response)
                        response_id = response.get("id")
                        completed_output = response.get("output")
                        if isinstance(response_id, str):
                            _native_context_put(
                                response_id,
                                payload.get("input", []) if isinstance(payload.get("input"), list) else [],
                                completed_output if isinstance(completed_output, list) and completed_output else output_items,
                                owner=provider_name,
                                access_key_id=access_key_id,
                            )
                            if on_trace is not None:
                                on_trace("completed", {"response_id": response_id, **usage_fields})
                yield chunk
        finally:
            if on_trace is not None:
                on_trace("stream_finished", {"completed": completed, **usage_fields})
            await upstream.aclose()
            await client.aclose()

    content_type = upstream.headers.get("content-type", "")
    if "text/event-stream" in content_type or body.get("stream"):
        return StreamingResponse(
            stream_bytes(),
            status_code=upstream.status_code,
            headers=headers,
            media_type=None,
        )

    try:
        content = await upstream.aread()
        try:
            response_payload = json.loads(content)
        except (TypeError, ValueError):
            response_payload = None
        if isinstance(response_payload, dict):
            response_id = response_payload.get("id")
            output = response_payload.get("output")
            if isinstance(response_id, str) and isinstance(output, list):
                _native_context_put(
                    response_id,
                    payload.get("input", []) if isinstance(payload.get("input"), list) else [],
                    output,
                    owner=provider_name,
                    access_key_id=access_key_id,
                )
            if on_trace is not None:
                on_trace("completed", _usage_trace_fields(response_payload))
        return Response(content=content, status_code=upstream.status_code, headers=headers, media_type=None)
    finally:
        await upstream.aclose()
        await client.aclose()
