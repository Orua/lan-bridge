"""Credential-isolated Responses API passthrough for custom providers."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .adapters.base import BaseAdapter
from .http_utils import make_provider_async_client as make_async_client
from .native_proxy import (
    _native_context_put,
    _response_headers,
    prepare_provider_responses_payload,
    proxy_native_responses,
)
from .protocol_adapters import (
    ChatToResponsesConversionError,
    ResponsesStreamToChat,
    ResponsesToChatConversionError,
    convert_chat_request_to_responses,
    convert_responses_response_to_chat,
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


async def _collect_chat_response_from_responses_stream(
    translator: ResponsesStreamToChat,
    chunks: AsyncIterator[bytes | str],
) -> dict[str, Any]:
    """Aggregate translated Chat SSE when a Chat client requested JSON."""
    completion_id = translator.chunk_id
    created = int(time.time())
    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    async for frame in translator.convert(chunks):
        data = frame[5:].strip() if frame.startswith("data:") else ""
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("error"), dict):
            raise ResponsesToChatConversionError(
                str(payload["error"].get("message") or "Responses 上游流失败")
            )
        completion_id = str(payload.get("id") or completion_id)
        created = int(payload.get("created") or created)
        if isinstance(payload.get("usage"), dict):
            usage = {
                "prompt_tokens": int(payload["usage"].get("prompt_tokens") or 0),
                "completion_tokens": int(payload["usage"].get("completion_tokens") or 0),
                "total_tokens": int(payload["usage"].get("total_tokens") or 0),
            }
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        if choice.get("finish_reason") is not None:
            finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if isinstance(delta.get("content"), str):
            content_parts.append(delta["content"])
        for raw_call in delta.get("tool_calls") or []:
            if not isinstance(raw_call, dict):
                continue
            index = int(raw_call.get("index") or 0)
            state = tool_calls.setdefault(index, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if raw_call.get("id"):
                state["id"] = str(raw_call["id"])
            function = raw_call.get("function")
            if isinstance(function, dict):
                if function.get("name"):
                    state["function"]["name"] = str(function["name"])
                if isinstance(function.get("arguments"), str):
                    state["function"]["arguments"] += function["arguments"]

    ordered_calls = [tool_calls[index] for index in sorted(tool_calls)]
    if finish_reason is None:
        finish_reason = "tool_calls" if ordered_calls else "stop"
    message = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
        "tool_calls": ordered_calls,
    }
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": translator.model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


class ProviderResponsesHTTPError(RuntimeError):
    """A non-success response from a custom Responses endpoint."""

    def __init__(self, provider: str, status_code: int, body: bytes):
        text = " ".join(body.decode("utf-8", errors="replace").strip().split())
        if len(text) > _MAX_UPSTREAM_ERROR_DETAIL:
            text = text[:_MAX_UPSTREAM_ERROR_DETAIL] + "...[truncated]"
        super().__init__(f"{provider} Responses upstream HTTP {status_code}: {text or 'No response body returned'}")
        self.status_code = status_code


class ChatToResponsesHTTPError(ProviderResponsesHTTPError):
    """A non-success response from a Chat-to-Responses upstream call."""


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
        or (model_config or {}).get("upstream_protocol")
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


async def proxy_chat_to_responses(
    body: dict[str, Any],
    chat_model: str,
    target_model: str,
    provider_name: str,
    provider: dict[str, Any],
    adapter: BaseAdapter,
    api_key: str,
    *,
    model_entry: dict[str, Any] | None = None,
    proxy_url: str = "",
    request_id: str = "",
    on_trace: Callable[[str, dict[str, Any]], None] | None = None,
) -> Response:
    """Call a Responses upstream and expose its result as Chat Completions."""
    dropped: list[str] = []
    payload = convert_chat_request_to_responses(
        body,
        target_model,
        model_entry=model_entry,
        on_drop=dropped.append,
    )
    if dropped:
        import logging

        logging.getLogger("lan-bridge").debug(
            "Responses route dropped unsupported Chat fields: %s",
            ", ".join(dropped),
        )
    if on_trace is not None:
        on_trace(
            "prepared",
            {
                "input_items": len(payload.get("input", [])),
                "input_types": [
                    item.get("type", "")
                    for item in payload.get("input", [])
                    if isinstance(item, dict)
                ],
                "has_images": any(
                    isinstance(part, dict) and part.get("type") == "input_image"
                    for item in payload.get("input", [])
                    if isinstance(item, dict)
                    for part in (item.get("content") or [])
                    if isinstance(item.get("content"), list)
                ),
                "tool_count": len(payload.get("tools") or []),
                "stream": bool(payload.get("stream")),
            },
        )

    timeout_seconds = float(provider.get("timeout", 120))
    stream_timeout = max(float(provider.get("stream_timeout", timeout_seconds)), 600.0)
    client = make_async_client(
        proxy_url=proxy_url,
        timeout=httpx.Timeout(stream_timeout if payload.get("stream") else timeout_seconds, connect=30.0),
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
            raise ChatToResponsesHTTPError(provider_name, upstream.status_code, error_body)
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = _response_headers(upstream.headers)
    if payload.get("stream") or "text/event-stream" in upstream.headers.get("content-type", ""):
        async def stream_bytes() -> AsyncIterator[str]:
            translator = ResponsesStreamToChat(chat_model, request_id)
            try:
                async for chunk in translator.convert(upstream.aiter_raw()):
                    yield chunk
                if on_trace is not None:
                    on_trace(
                        "stream_finished",
                        {
                            "completed": translator.completed or translator.incomplete,
                            "incomplete": translator.incomplete,
                            "failed": translator.failed,
                            "tokens": (translator.usage or {}).get("total_tokens", 0),
                        },
                    )
            finally:
                await upstream.aclose()
                await client.aclose()

        headers.pop("content-type", None)
        headers["Cache-Control"] = "no-cache, no-transform"
        headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(stream_bytes(), status_code=200, headers=headers, media_type="text/event-stream")

    try:
        raw = await upstream.aread()
        try:
            response_payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ResponsesToChatConversionError("Responses 上游返回了无效 JSON") from exc
        chat_response = convert_responses_response_to_chat(response_payload, chat_model)
        if on_trace is not None:
            usage = chat_response.get("usage") or {}
            on_trace("completed", {"tokens": usage.get("total_tokens", 0)})
        headers.pop("content-type", None)
        return Response(
            content=json.dumps(chat_response, ensure_ascii=False, separators=(",", ":")),
            status_code=200,
            headers=headers,
            media_type="application/json",
        )
    finally:
        await upstream.aclose()
        await client.aclose()


async def proxy_chat_to_native_responses(
    request: Request,
    body: dict[str, Any],
    chat_model: str,
    target_model: str,
    config,
    *,
    model_entry: dict[str, Any] | None = None,
    request_id: str = "",
    access_key_id: str = "",
    on_trace: Callable[[str, dict[str, Any]], None] | None = None,
) -> Response:
    """Expose an official native Responses model through Chat Completions."""
    client_wants_stream = bool(body.get("stream", False))
    dropped: list[str] = []
    payload = convert_chat_request_to_responses(
        body,
        target_model,
        model_entry=model_entry,
        on_drop=dropped.append,
    )
    # The official Codex backend only serves Responses as SSE and rejects the
    # public max_output_tokens field. Non-streaming Chat clients are aggregated
    # locally after the upstream stream has completed.
    payload["stream"] = True
    if payload.pop("max_output_tokens", None) is not None:
        dropped.append("max_output_tokens")
    if dropped:
        import logging

        logging.getLogger("lan-bridge").debug(
            "Native Responses route dropped unsupported Chat fields: %s",
            ", ".join(dropped),
        )

    if on_trace is not None:
        on_trace("prepared", {
            "input_items": len(payload.get("input", [])),
            "input_types": [
                item.get("type", "")
                for item in payload.get("input", [])
                if isinstance(item, dict)
            ],
            "tool_count": len(payload.get("tools") or []),
            "stream": client_wants_stream,
            "upstream_stream": True,
        })

    def trace_native(event: str, fields: dict[str, Any]) -> None:
        if on_trace is not None:
            on_trace(f"native_{event}", dict(fields))

    upstream_response = await proxy_native_responses(
        request,
        payload,
        target_model,
        config,
        access_key_id=access_key_id,
        on_trace=trace_native,
    )
    headers = dict(upstream_response.headers)
    headers.pop("content-length", None)
    headers.pop("content-type", None)

    if isinstance(upstream_response, StreamingResponse):
        original_iterator = upstream_response.body_iterator

        if not client_wants_stream:
            translator = ResponsesStreamToChat(chat_model, request_id)
            try:
                chat_response = await _collect_chat_response_from_responses_stream(
                    translator,
                    original_iterator,
                )
            finally:
                closer = getattr(original_iterator, "aclose", None)
                if callable(closer):
                    await closer()
            if on_trace is not None:
                on_trace("completed", {
                    "incomplete": translator.incomplete,
                    "tokens": (chat_response.get("usage") or {}).get("total_tokens", 0),
                })
            return Response(
                content=json.dumps(chat_response, ensure_ascii=False, separators=(",", ":")),
                status_code=upstream_response.status_code,
                headers=headers,
                media_type="application/json",
            )

        async def stream_chat() -> AsyncIterator[str]:
            translator = ResponsesStreamToChat(chat_model, request_id)
            try:
                async for chunk in translator.convert(original_iterator):
                    yield chunk
            finally:
                closer = getattr(original_iterator, "aclose", None)
                if callable(closer):
                    await closer()
                if on_trace is not None:
                    on_trace("stream_finished", {
                        "completed": translator.completed or translator.incomplete,
                        "incomplete": translator.incomplete,
                        "failed": translator.failed,
                        "tokens": (translator.usage or {}).get("total_tokens", 0),
                    })

        headers["Cache-Control"] = "no-cache, no-transform"
        headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(
            stream_chat(),
            status_code=upstream_response.status_code,
            headers=headers,
            media_type="text/event-stream",
        )

    try:
        response_payload = json.loads(upstream_response.body or b"{}")
    except (TypeError, ValueError) as exc:
        raise ResponsesToChatConversionError("Responses 上游返回了无效 JSON") from exc
    chat_response = convert_responses_response_to_chat(response_payload, chat_model)
    if on_trace is not None:
        usage = chat_response.get("usage") or {}
        on_trace("completed", {"tokens": usage.get("total_tokens", 0)})
    return Response(
        content=json.dumps(chat_response, ensure_ascii=False, separators=(",", ":")),
        status_code=upstream_response.status_code,
        headers=headers,
        media_type="application/json",
    )
