"""Native ChatGPT Codex passthrough and merged model catalog support."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .http_utils import make_native_async_client
from .codex_auth import (
    NativeAuthError,
    load_native_credentials,
    native_auth_injection_enabled,
)


DEFAULT_NATIVE_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CUSTOM_CONTEXT_WINDOW = 131_072
DEFAULT_CUSTOM_AUTO_COMPACT_TOKEN_LIMIT = 110_000
_CUSTOM_CONTEXT_PROFILES = (
    (("deepseek",), 1_048_576, 900_000),
    (("qwen",), 262_144, 235_000),
    (("kimi", "moonshot"), 262_144, 235_000),
)
_MAX_UPSTREAM_ERROR_DETAIL = 2048
_NATIVE_CONTEXT_CACHE_MAX_ENTRIES = 64
_NATIVE_CONTEXT_CACHE_MAX_BYTES = 96 * 1024 * 1024
_NATIVE_CONTEXT_CACHE_TTL_SECONDS = 2 * 60 * 60
_NATIVE_CONTEXT_CACHE: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
_NATIVE_CONTEXT_CACHE_BYTES = 0
_RESPONSE_CONTEXT_OWNERS: dict[str, tuple[str, str]] = {}
FORWARD_REQUEST_HEADERS = frozenset({
    "authorization",
    "chatgpt-account-id",
    "openai-beta",
    "originator",
    "session_id",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-installation-id",
    "x-codex-parent-thread-id",
    "x-codex-turn-metadata",
    "x-codex-turn-state",
    "x-codex-window-id",
    "x-oai-attestation",
    "x-openai-subagent",
    "x-responsesapi-include-timing-metrics",
})

HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})

SAFE_RESPONSE_HEADERS = frozenset({
    "cache-control",
    "content-type",
    "openai-processing-ms",
    "openai-version",
    "request-id",
    "retry-after",
    "x-accel-buffering",
    "x-envoy-upstream-service-time",
    "x-request-id",
})


class NativeUpstreamHTTPError(RuntimeError):
    """A non-success response from the official Codex upstream."""

    def __init__(self, status_code: int, body: bytes, content_type: str = ""):
        text = body.decode("utf-8", errors="replace").strip()
        text = " ".join(text.split())
        if len(text) > _MAX_UPSTREAM_ERROR_DETAIL:
            text = text[:_MAX_UPSTREAM_ERROR_DETAIL] + "...[truncated]"
        detail = text or "No response body returned"
        if content_type:
            detail = f"{content_type}: {detail}"
        super().__init__(f"Official upstream HTTP {status_code}: {detail}")
        self.status_code = status_code


class ResponseContextAccessError(RuntimeError):
    """A continuation cannot access a cache entry owned by this bridge key."""

    code = "response_context_unavailable"

    def __init__(self):
        super().__init__(
            "previous_response_id is unavailable for this bridge access key"
        )


def _usage_trace_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Extract upstream-reported usage without modifying the proxied response."""
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


def native_request_headers(headers, config=None) -> dict[str, str]:
    forwarded = {
        "content-type": "application/json",
        "accept": "text/event-stream, application/json",
        "accept-encoding": "identity",
    }
    for name in FORWARD_REQUEST_HEADERS:
        value = headers.get(name)
        if value is not None:
            forwarded[name] = value
    if config is not None:
        if not native_auth_injection_enabled(config):
            raise NativeAuthError(
                "Host Codex login injection is disabled; enable it on the bridge computer"
            )
        if not _is_official_openai_url(_native_base_url(config)):
            raise NativeAuthError(
                "Host login injection is restricted to official OpenAI and ChatGPT upstreams"
            )
        credentials = load_native_credentials(config)
        # The client Authorization value is a LAN BRIDGE key.  It and all
        # client-supplied account/attestation credentials must never leave the
        # bridge process.
        forwarded.pop("authorization", None)
        forwarded.pop("chatgpt-account-id", None)
        forwarded.pop("x-oai-attestation", None)
        forwarded["authorization"] = f"Bearer {credentials.access_token}"
        forwarded["chatgpt-account-id"] = credentials.account_id
    return forwarded


_NATIVE_ENCRYPTED_TOKEN = re.compile(r"^gAAAAA[A-Za-z0-9_-]+={0,2}$")
_ROUTED_REASONING_PREFIX = (
    "A third-party model produced this reasoning summary in an earlier turn. "
    "Use it only as conversation context:\n\n"
)
_ORPHAN_TOOL_OUTPUT_PREFIX = (
    "A tool produced this output in an earlier routed turn, but its matching "
    "tool call is unavailable. Use it only as conversation context:\n\n"
)
_TOOL_CALL_TYPES = frozenset({
    "function_call",
    "custom_tool_call",
    "local_shell_call",
    "tool_search_call",
    "tool_call",
})
_TOOL_OUTPUT_TYPES = frozenset({
    "function_call_output",
    "custom_tool_call_output",
    "local_shell_call_output",
    "tool_search_output",
    "tool_result",
})
_NATIVE_TOOL_ITEM_ID_PREFIXES = {
    "function_call": "fc_",
    "custom_tool_call": "ctc_",
    "tool_search_call": "tsc_",
}


def _looks_like_native_encrypted_content(value: Any) -> bool:
    return isinstance(value, str) and _NATIVE_ENCRYPTED_TOKEN.fullmatch(value) is not None


def _routed_reasoning_message(item: dict[str, Any]) -> dict[str, Any] | None:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return None
    text = "\n".join(
        str(part.get("text"))
        for part in summary
        if isinstance(part, dict) and part.get("text")
    ).strip()
    if not text:
        return None
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": _ROUTED_REASONING_PREFIX + text}],
    }


def _sanitize_native_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    if item.get("type") == "reasoning" and not _looks_like_native_encrypted_content(
        item.get("encrypted_content")
    ):
        return _routed_reasoning_message(item)
    normalized = copy.deepcopy(item)
    item_type = normalized.get("type")
    expected_prefix = _NATIVE_TOOL_ITEM_ID_PREFIXES.get(item_type)
    if expected_prefix:
        item_id = normalized.get("id")
        if isinstance(item_id, str) and item_id and not item_id.startswith(expected_prefix):
            digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:32]
            normalized["id"] = f"{expected_prefix}{digest}"
    content = normalized.get("content")
    if isinstance(content, list):
        sanitized_content = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "encrypted_content":
                sanitized_content.append(_sanitize_native_item(part))
                continue
            value = part.get("encrypted_content")
            if _looks_like_native_encrypted_content(value):
                sanitized_content.append(copy.deepcopy(part))
            elif normalized.get("type") == "agent_message" and isinstance(value, str) and value:
                sanitized_content.append({"type": "input_text", "text": value})
        normalized["content"] = sanitized_content
    return normalized


def _tool_item_ids(item: dict[str, Any]) -> set[str]:
    return {
        value
        for key in ("call_id", "tool_use_id", "id")
        if isinstance((value := item.get(key)), str) and value
    }


def _tool_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_tool_output_text(part) for part in value))).strip()
    if isinstance(value, dict):
        for key in ("text", "output_text", "content"):
            if key in value:
                text = _tool_output_text(value[key])
                if text:
                    return text
    return ""


def _orphan_tool_output_message(item: dict[str, Any]) -> dict[str, Any]:
    text = _tool_output_text(item.get("output"))
    if not text:
        text = _tool_output_text(item.get("content"))
    if not text:
        text = "[The earlier tool returned non-text output that cannot be resumed.]"
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": _ORPHAN_TOOL_OUTPUT_PREFIX + text}],
    }


def _repair_orphan_tool_outputs(input_items: list[Any]) -> list[Any]:
    call_ids: set[str] = set()
    for item in input_items:
        if isinstance(item, dict) and item.get("type") in _TOOL_CALL_TYPES:
            call_ids.update(_tool_item_ids(item))

    repaired = []
    for item in input_items:
        if isinstance(item, dict) and item.get("type") in _TOOL_OUTPUT_TYPES:
            output_ids = _tool_item_ids(item)
            if not output_ids or call_ids.isdisjoint(output_ids):
                repaired.append(_orphan_tool_output_message(item))
                continue
        repaired.append(item)
    return repaired


def _discard_native_context(response_id: str) -> None:
    global _NATIVE_CONTEXT_CACHE_BYTES
    cached = _NATIVE_CONTEXT_CACHE.pop(response_id, None)
    _RESPONSE_CONTEXT_OWNERS.pop(response_id, None)
    if cached is not None:
        _NATIVE_CONTEXT_CACHE_BYTES -= len(cached[1])


def _native_context_owner(
    response_id: str | None,
    access_key_id: str = "",
) -> tuple[str, str] | None:
    if response_id is None:
        return None
    if not isinstance(response_id, str) or not response_id:
        if access_key_id:
            raise ResponseContextAccessError()
        return None
    cached = _NATIVE_CONTEXT_CACHE.get(response_id)
    if cached is None:
        if access_key_id:
            raise ResponseContextAccessError()
        return None
    created_at, _ = cached
    if time.monotonic() - created_at > _NATIVE_CONTEXT_CACHE_TTL_SECONDS:
        _discard_native_context(response_id)
        if access_key_id:
            raise ResponseContextAccessError()
        return None
    owner = _RESPONSE_CONTEXT_OWNERS.get(response_id)
    # Tolerate cache state created by an older in-process implementation only
    # for legacy callers that do not provide an access-key identity.
    if isinstance(owner, str):
        owner = (owner, "")
    if not isinstance(owner, tuple) or len(owner) != 2:
        if access_key_id:
            raise ResponseContextAccessError()
        return None
    if access_key_id and owner[1] != access_key_id:
        raise ResponseContextAccessError()
    return owner


def _native_context_get(
    response_id: Any,
    access_key_id: str = "",
) -> list[Any] | None:
    _native_context_owner(response_id, access_key_id)
    if not isinstance(response_id, str) or not response_id:
        return None
    cached = _NATIVE_CONTEXT_CACHE.pop(response_id, None)
    if cached is None:
        return None
    _, encoded = cached
    _NATIVE_CONTEXT_CACHE[response_id] = cached
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        _discard_native_context(response_id)
        if access_key_id:
            raise ResponseContextAccessError()
        return None
    return value if isinstance(value, list) else None


def _native_context_put(
    response_id: str,
    input_items: list[Any],
    output_items: list[Any],
    *,
    owner: str = "native_codex",
    access_key_id: str = "",
) -> None:
    global _NATIVE_CONTEXT_CACHE_BYTES
    if not response_id:
        return
    encoded = json.dumps(
        [*input_items, *output_items],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _NATIVE_CONTEXT_CACHE_MAX_BYTES:
        return
    previous = _NATIVE_CONTEXT_CACHE.pop(response_id, None)
    if previous is not None:
        _NATIVE_CONTEXT_CACHE_BYTES -= len(previous[1])
    _NATIVE_CONTEXT_CACHE[response_id] = (time.monotonic(), encoded)
    _RESPONSE_CONTEXT_OWNERS[response_id] = (owner, access_key_id)
    _NATIVE_CONTEXT_CACHE_BYTES += len(encoded)
    while (
        len(_NATIVE_CONTEXT_CACHE) > _NATIVE_CONTEXT_CACHE_MAX_ENTRIES
        or _NATIVE_CONTEXT_CACHE_BYTES > _NATIVE_CONTEXT_CACHE_MAX_BYTES
    ):
        removed_id, (_, removed) = _NATIVE_CONTEXT_CACHE.popitem(last=False)
        _RESPONSE_CONTEXT_OWNERS.pop(removed_id, None)
        _NATIVE_CONTEXT_CACHE_BYTES -= len(removed)


def cached_response_owner(
    response_id: str | None,
    access_key_id: str = "",
) -> str | None:
    """Return the upstream that created a cached response ID."""
    owner = _native_context_owner(response_id, access_key_id)
    return owner[0] if owner is not None else None


def prepare_provider_responses_payload(
    body: dict[str, Any],
    target_model: str,
    provider: str,
    *,
    supports_previous_response_id: bool = False,
    access_key_id: str = "",
) -> tuple[dict[str, Any], bool]:
    """Keep native Responses state unless a turn crosses provider boundaries."""
    normalized = copy.deepcopy(body)
    normalized["model"] = target_model
    previous_id = normalized.get("previous_response_id")
    owner = cached_response_owner(previous_id, access_key_id)
    if owner is None or (owner == provider and supports_previous_response_id):
        return normalized, False

    prior_input = _native_context_get(previous_id, access_key_id)
    current_input = normalized.get("input")
    normalized.pop("previous_response_id", None)
    if prior_input is not None and isinstance(current_input, list):
        normalized["input"] = [*prior_input, *current_input]
    return normalized, prior_input is not None


def prepare_chat_responses_payload(
    body: dict[str, Any],
    target_model: str,
    *,
    access_key_id: str = "",
) -> tuple[dict[str, Any], bool]:
    """Expand a Codex Responses continuation for a stateless Chat upstream."""
    normalized = copy.deepcopy(body)
    normalized["model"] = target_model
    previous_id = normalized.pop("previous_response_id", None)
    prior_input = (
        _native_context_get(previous_id, access_key_id)
        if previous_id is not None
        else None
    )
    current_input = normalized.get("input")
    if not isinstance(current_input, list):
        return normalized, False
    normalized["input"] = _repair_orphan_tool_outputs([
        *(prior_input or []),
        *current_input,
    ])
    return normalized, prior_input is not None


def _prepare_native_responses_payload(
    body: dict[str, Any],
    target_model: str,
    access_key_id: str = "",
) -> dict[str, Any]:
    """Replay cached native output when Codex sends only a continuation delta."""
    normalized = normalize_native_payload(
        body,
        target_model,
        preserve_previous_response_id=True,
    )
    previous_id = normalized.pop("previous_response_id", None)
    prior_input = (
        _native_context_get(previous_id, access_key_id)
        if previous_id is not None
        else None
    )
    current_input = normalized.get("input")
    if not isinstance(current_input, list):
        return normalized
    normalized["input"] = _repair_orphan_tool_outputs([
        *(prior_input or []),
        *current_input,
    ])
    return normalized


def normalize_native_payload(
    body: dict[str, Any],
    target_model: str,
    *,
    preserve_previous_response_id: bool = False,
) -> dict[str, Any]:
    """Remove routed-provider state that the native backend cannot safely consume."""
    normalized = copy.deepcopy(body)
    normalized["model"] = target_model
    if not preserve_previous_response_id:
        normalized.pop("previous_response_id", None)
    if isinstance(normalized.get("input"), list):
        sanitized_input = [
            item
            for item in (_sanitize_native_item(item) for item in normalized["input"])
            if item is not None
        ]
        normalized["input"] = (
            sanitized_input
            if preserve_previous_response_id
            else _repair_orphan_tool_outputs(sanitized_input)
        )
    return normalized


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() in SAFE_RESPONSE_HEADERS
    }


def _native_base_url(config) -> str:
    server = getattr(config, "data", {}).get("server", {})
    return str(server.get("native_codex_base_url") or DEFAULT_NATIVE_CODEX_BASE_URL).rstrip("/")


def _is_official_openai_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return hostname in {"openai.com", "chatgpt.com"} or hostname.endswith((".openai.com", ".chatgpt.com"))


def _native_client(config, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Use the official Codex proxy only for the native ChatGPT route."""
    server = getattr(config, "data", {}).get("server", {})
    proxy_url = ""
    if _is_official_openai_url(_native_base_url(config)):
        proxy_url = str(server.get("codex_official_proxy_url") or "").strip()
    return make_native_async_client(timeout=timeout, proxy_url=proxy_url)


async def proxy_native_responses(
    request: Request,
    body: dict[str, Any],
    target_model: str,
    config,
    *,
    upstream_path: str = "responses",
    on_trace: Callable[[str, dict[str, Any]], None] | None = None,
    access_key_id: str = "",
) -> Response:
    """Forward a Responses request and stream upstream bytes without SSE reconstruction."""
    if upstream_path not in {"responses", "responses/compact"}:
        raise ValueError(f"Unsupported native Responses path: {upstream_path}")
    server = getattr(config, "data", {}).get("server", {})
    timeout_seconds = float(server.get("native_stream_timeout", 600))

    if upstream_path == "responses/compact":
        if body.get("previous_response_id") is not None and access_key_id:
            cached_response_owner(body.get("previous_response_id"), access_key_id)
        upstream_payload = normalize_native_payload(
            body,
            target_model,
            preserve_previous_response_id=bool(body.get("previous_response_id")),
        )
    else:
        continuation_id = body.get("previous_response_id")
        upstream_payload = _prepare_native_responses_payload(
            body,
            target_model,
            access_key_id,
        )
        context_cache_hit = (
            isinstance(continuation_id, str)
            and cached_response_owner(continuation_id, access_key_id) is not None
        )

    client = _native_client(config, httpx.Timeout(timeout_seconds, connect=15.0))

    if on_trace is not None:
        prepared_input = upstream_payload.get("input", [])
        on_trace("prepared", {
            "context_cache_hit": context_cache_hit if upstream_path == "responses" else False,
            "input_items": len(prepared_input) if isinstance(prepared_input, list) else 0,
            "input_types": [
                item.get("type", "")
                for item in prepared_input
                if isinstance(item, dict)
            ],
        })

    def build_upstream_request():
        return client.build_request(
            "POST",
            f"{_native_base_url(config)}/{upstream_path}",
            headers=native_request_headers(request.headers, config),
            content=json.dumps(
                upstream_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    upstream_request = build_upstream_request()
    try:
        upstream = await client.send(upstream_request, stream=True)
    except Exception:
        await client.aclose()
        raise

    if upstream.status_code >= 400:
        error_body = await upstream.aread()

    if upstream.status_code >= 400:
        try:
            raise NativeUpstreamHTTPError(
                upstream.status_code,
                error_body,
                upstream.headers.get("content-type", ""),
            )
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = _response_headers(upstream.headers)

    async def stream_bytes() -> AsyncIterator[bytes]:
        pending = b""
        traced_response_ids: set[str] = set()
        completed_output_items: list[Any] = []
        completed = False
        usage_fields: dict[str, Any] = {}
        try:
            async for chunk in upstream.aiter_raw():
                if on_trace is not None or upstream_path == "responses":
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        if not line.startswith(b"data:"):
                            continue
                        raw_event = line[5:].strip()
                        if not raw_event or raw_event == b"[DONE]":
                            continue
                        try:
                            event = json.loads(raw_event)
                        except (TypeError, ValueError):
                            continue
                        response = event.get("response") if isinstance(event, dict) else None
                        response_id = response.get("id") if isinstance(response, dict) else None
                        if (
                            isinstance(event, dict)
                            and event.get("type") == "response.output_item.done"
                            and isinstance(event.get("item"), dict)
                        ):
                            completed_output_items.append(event["item"])
                        if (
                            on_trace is not None
                            and isinstance(response_id, str)
                            and response_id not in traced_response_ids
                        ):
                            traced_response_ids.add(response_id)
                            on_trace(
                                "response_id",
                                {"response_id": response_id, "event_type": event.get("type", "")},
                            )
                        if (
                            upstream_path == "responses"
                            and isinstance(response, dict)
                            and event.get("type") == "response.completed"
                            and isinstance(response_id, str)
                        ):
                            completed = True
                            usage_fields = _usage_trace_fields(response)
                            response_output = response.get("output")
                            output_items = (
                                response_output
                                if isinstance(response_output, list) and response_output
                                else completed_output_items
                            )
                            if on_trace is not None:
                                on_trace("completed_output", {
                                    **usage_fields,
                                    "output_items": len(output_items),
                                    "output_types": [
                                        item.get("type", "")
                                        for item in output_items
                                        if isinstance(item, dict)
                                    ],
                                })
                            _native_context_put(
                                response_id,
                                upstream_payload.get("input", []),
                                output_items,
                                access_key_id=access_key_id,
                            )
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
        if upstream_path == "responses":
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                payload = None
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("id"), str)
                and isinstance(payload.get("output"), list)
            ):
                _native_context_put(
                    payload["id"],
                    upstream_payload.get("input", []),
                    payload["output"],
                    access_key_id=access_key_id,
                )
            if isinstance(payload, dict) and on_trace is not None:
                on_trace("completed", _usage_trace_fields(payload))
        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=headers,
            media_type=None,
        )
    finally:
        await upstream.aclose()
        await client.aclose()


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def custom_model_context_settings(alias: str, entry: dict[str, Any]) -> dict[str, int]:
    """Resolve configured custom-model limits against deterministic defaults."""
    identity = " ".join(
        str(entry.get(key) or "")
        for key in ("provider", "target", "display_name")
    )
    identity = f"{alias} {identity}".lower()
    default_context = DEFAULT_CUSTOM_CONTEXT_WINDOW
    default_compact = DEFAULT_CUSTOM_AUTO_COMPACT_TOKEN_LIMIT
    for needles, context_window, compact_limit in _CUSTOM_CONTEXT_PROFILES:
        if any(needle in identity for needle in needles):
            default_context = context_window
            default_compact = compact_limit
            break

    capabilities = dict(entry.get("capabilities") or {})
    configured_context = _optional_positive_int(
        capabilities.get("context_window", entry.get("context_window"))
    )
    context_window = configured_context or default_context
    configured_compact = _optional_positive_int(
        capabilities.get(
            "auto_compact_token_limit",
            entry.get("auto_compact_token_limit"),
        )
    )
    if configured_compact is not None:
        compact_limit = configured_compact
    elif configured_context is None:
        compact_limit = default_compact
    else:
        compact_limit = round(context_window * default_compact / default_context)
    compact_limit = max(1, min(compact_limit, context_window - 1))
    return {
        "context_window": context_window,
        "auto_compact_token_limit": compact_limit,
        "default_context_window": default_context,
        "default_auto_compact_token_limit": default_compact,
    }


def custom_model_info(
    alias: str,
    entry: dict[str, Any],
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    capabilities = dict(entry.get("capabilities") or {})
    context_settings = custom_model_context_settings(alias, entry)
    context_window = context_settings["context_window"]
    reasoning = bool(capabilities.get("reasoning", entry.get("is_reasoning_text", True)))
    image_input = bool(capabilities.get("image_input", entry.get("is_multimodal", False)))
    web_search = bool(capabilities.get("web_search", True))
    shell = bool(capabilities.get("shell", True))
    apply_patch = bool(capabilities.get("apply_patch", True))
    configured_tool_mode = str(
        capabilities.get("tool_mode") or entry.get("tool_mode") or ""
    ).strip()
    if configured_tool_mode in {"direct", "code_mode_only"}:
        tool_mode = configured_tool_mode
    else:
        # Preserve Codex's native direct-tool behavior for both Chat-backed and
        # Responses-backed custom models.  Code mode remains available only as
        # an explicit per-model opt-in; making it the Chat default hides search,
        # MCP, and plugin tools from sessions that previously received them.
        tool_mode = "direct"
    default_instructions = (
        "You are Codex, a pragmatic coding agent. For tool work, call the provided local executor "
        "immediately. Use its ALL_TOOLS index to locate exact tools, batch independent operations, "
        "and avoid narrating or debating tool plumbing. Continue until the result is verified."
        if tool_mode == "code_mode_only"
        else "You are Codex, a pragmatic coding agent. Use the available tools to complete the user's task accurately."
    )
    base_instructions = str(
        capabilities.get("base_instructions")
        or entry.get("base_instructions")
        or default_instructions
    )
    levels = capabilities.get("reasoning_levels") or ([
        {"effort": effort, "description": description}
        for effort, description in (
            ("low", "Fast responses with lighter reasoning"),
            ("medium", "Balanced reasoning for everyday tasks"),
            ("high", "Greater reasoning depth for complex tasks"),
            ("xhigh", "Extra reasoning depth for difficult tasks"),
        )
    ] if reasoning else [])
    return {
        "slug": alias,
        "base_instructions": base_instructions,
        "display_name": str(entry.get("display_name") or alias),
        "description": str(entry.get("description") or f"Routed through CN Bridge to {entry.get('provider', 'custom provider')}"),
        "context_window": context_window,
        "max_context_window": context_window,
        "auto_compact_token_limit": context_settings["auto_compact_token_limit"],
        "input_modalities": ["text", "image"] if image_input else ["text"],
        "prefer_websockets": False,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform" if apply_patch else None,
        "web_search_tool_type": "text_and_image" if web_search else None,
        "supports_parallel_tool_calls": bool(capabilities.get("parallel_tools", True)),
        "tool_mode": tool_mode,
        "shell_type": "shell_command" if shell else "disabled",
        "use_responses_lite": False,
        "supports_reasoning_summary_parameter": bool(capabilities.get("reasoning_summary", reasoning)),
        "supports_reasoning_summaries": bool(capabilities.get("reasoning_summary", reasoning)),
        "default_reasoning_summary": "auto" if reasoning else "none",
        "default_reasoning_level": str(capabilities.get("default_reasoning_level") or ("medium" if reasoning else "none")),
        "supported_reasoning_levels": levels,
        "truncation_policy": {"mode": "tokens", "limit": int(capabilities.get("tool_output_limit") or 10000)},
        "visibility": "list",
        "minimal_client_version": "0.0.0",
        "supported_in_api": True,
        "priority": int(entry.get("priority") or 100),
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "availability_nux": None,
        "upgrade": None,
        "model_messages": None,
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": True,
        "supports_image_detail_original": image_input,
        "effective_context_window_percent": int(capabilities.get("effective_context_window_percent") or 95),
        "experimental_supported_tools": [],
        "supports_search_tool": web_search,
        "auto_review_model_override": None,
        "model_specialty": None,
        "multi_agent_version": None,
    }


def add_openai_model_list(catalog: dict[str, Any]) -> dict[str, Any]:
    """Expose the merged Codex catalog through OpenAI's ``/v1/models`` shape.

    Codex Desktop consumes the richer ``models`` array, while ordinary
    OpenAI-compatible clients expect ``object: list`` and ``data`` entries.
    Keep both views in one response so configuring Codex against the bridge
    does not make the endpoint less useful to standard API callers.
    """
    result = dict(catalog)
    data = []
    for model in result.get("models") or []:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("slug") or model.get("id") or "").strip()
        if not model_id:
            continue
        created = model.get("created", 0)
        if not isinstance(created, int) or created < 0:
            created = 0
        data.append({
            "id": model_id,
            "object": "model",
            "created": created,
            "owned_by": str(model.get("owned_by") or "lan-bridge"),
        })
    result["object"] = "list"
    result["data"] = data
    return result


def merge_model_catalog(native_payload: dict[str, Any], config) -> dict[str, Any]:
    supported_reasoning_efforts = {"none", "minimal", "low", "medium", "high", "xhigh"}
    native_models = []
    for model in native_payload.get("models") or []:
        if not isinstance(model, dict):
            continue
        bridged_model = copy.deepcopy(model)
        # The bridge exposes an HTTP Responses endpoint, not the native Codex
        # websocket/lite continuation protocol. Force the client to replay the
        # complete conversation so model switches cannot lose session context.
        bridged_model["prefer_websockets"] = False
        bridged_model["use_responses_lite"] = False
        bridged_model.setdefault(
            "supports_reasoning_summaries",
            bridged_model.get("default_reasoning_summary") not in (None, "none"),
        )
        levels = bridged_model.get("supported_reasoning_levels")
        if isinstance(levels, list):
            normalized_levels = []
            seen_efforts = set()
            for level in levels:
                if not isinstance(level, dict):
                    continue
                normalized = copy.deepcopy(level)
                effort = str(normalized.get("effort") or "")
                if effort == "max":
                    effort = "xhigh"
                    normalized["effort"] = effort
                if effort not in supported_reasoning_efforts or effort in seen_efforts:
                    continue
                seen_efforts.add(effort)
                normalized_levels.append(normalized)
            bridged_model["supported_reasoning_levels"] = normalized_levels
        if bridged_model.get("default_reasoning_level") == "max":
            bridged_model["default_reasoning_level"] = "xhigh"
        native_models.append(bridged_model)
    by_slug = {
        str(model.get("slug")): model
        for model in native_models
        if isinstance(model, dict) and model.get("slug")
    }
    native_template = next(iter(by_slug.values()), None)
    for alias, entry in getattr(config, "native_models", {}).items():
        if isinstance(entry, dict) and not entry.get("enabled", True):
            continue
        alias = str(alias)
        if not alias or alias in by_slug:
            continue
        target = str(entry.get("target") or alias) if isinstance(entry, dict) else alias
        template = by_slug.get(target) or native_template
        if template is not None:
            model = copy.deepcopy(template)
            model["slug"] = alias
            model["display_name"] = str(entry.get("display_name") or alias) if isinstance(entry, dict) else alias
            model["description"] = str(entry.get("description") or "Routed through LAN BRIDGE host login") if isinstance(entry, dict) else "Routed through LAN BRIDGE host login"
            model["priority"] = int(entry.get("priority") or 90) if isinstance(entry, dict) else 90
        else:
            model = custom_model_info(alias, {
                "display_name": str(entry.get("display_name") or alias) if isinstance(entry, dict) else alias,
                "description": str(entry.get("description") or "Routed through LAN BRIDGE host login") if isinstance(entry, dict) else "Routed through LAN BRIDGE host login",
                "context_window": int(entry.get("context_window") or 200_000) if isinstance(entry, dict) else 200_000,
                "is_reasoning_text": True,
                "priority": int(entry.get("priority") or 90) if isinstance(entry, dict) else 90,
            }, None)
        by_slug[alias] = model
    for alias, entry in getattr(config, "model_mapping", {}).items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        route_kind = entry.get("route_kind") or entry.get("kind")
        if route_kind == "native_codex" or entry.get("provider") == "native_codex":
            continue
        if any(entry.get(flag) for flag in ("is_image_gen", "is_video_gen")):
            continue
        provider_name = str(entry.get("provider") or "")
        provider = config.get_provider(provider_name) if provider_name else None
        by_slug[str(alias)] = custom_model_info(str(alias), entry, provider)
    merged = dict(native_payload)
    merged["models"] = sorted(by_slug.values(), key=lambda item: (int(item.get("priority", 100)), str(item.get("slug", ""))))
    return add_openai_model_list(merged)


async def fetch_merged_models(request: Request, config) -> Response:
    client = _native_client(config, httpx.Timeout(10.0, connect=5.0))
    url = f"{_native_base_url(config)}/models"
    query = list(request.query_params.multi_items())
    if not any(name == "client_version" for name, _ in query):
        # The native catalog requires this Codex-specific query field, while
        # ordinary OpenAI-compatible clients correctly call /v1/models without
        # it.  Supplying a neutral value keeps both client styles compatible.
        query.append(("client_version", "0.0.0"))
    try:
        upstream = await client.get(
            url,
            params=query,
            headers=native_request_headers(request.headers, config),
        )
        if upstream.is_success:
            native_payload = upstream.json()
            merged = merge_model_catalog(native_payload, config)
            headers = _response_headers(upstream.headers)
            headers.pop("content-type", None)
            return Response(
                content=json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                status_code=200,
                headers=headers,
                media_type="application/json",
            )
        custom_aliases = {
            str(alias)
            for alias, entry in getattr(config, "model_mapping", {}).items()
            if isinstance(entry, dict) and entry.get("enabled", True)
        }
        if custom_aliases:
            # WorkBuddy and ordinary OpenAI clients still need the configured
            # custom aliases when the optional native catalog is unavailable.
            # Do not invent native entries in this fallback; those require the
            # host's native login and remain absent until the catalog is live.
            fallback = merge_model_catalog({"models": []}, config)
            fallback["models"] = [
                model for model in fallback.get("models", [])
                if str(model.get("slug") or "") in custom_aliases
            ]
            fallback = add_openai_model_list(fallback)
            headers = _response_headers(upstream.headers)
            headers.pop("content-type", None)
            return Response(
                content=json.dumps(fallback, ensure_ascii=False, separators=(",", ":")),
                status_code=200,
                headers=headers,
                media_type="application/json",
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=_response_headers(upstream.headers),
            media_type=None,
        )
    finally:
        await client.aclose()
