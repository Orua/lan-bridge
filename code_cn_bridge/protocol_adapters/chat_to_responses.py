"""Convert an OpenAI Chat Completions request into a Responses request.

This adapter deliberately accepts only the portable function-tool subset.  A
WorkBuddy client can continue speaking Chat Completions while the selected
model route explicitly opts into the Responses upstream protocol.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit


SUPPORTED_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
_SUPPORTED_ROLES = {"system", "developer", "user", "assistant", "tool"}
_TEXT_PART_TYPES = {"text", "input_text", "output_text"}


class ChatToResponsesConversionError(ValueError):
    """A client request cannot be represented by the Responses API."""

    code = "invalid_request_error"
    status_code = 400


def _capabilities(entry: dict[str, Any] | None) -> dict[str, Any]:
    raw = (entry or {}).get("capabilities")
    return dict(raw) if isinstance(raw, dict) else {}


def _supported_reasoning_efforts(capabilities: dict[str, Any]) -> set[str]:
    configured = capabilities.get("reasoning_efforts")
    if configured is None:
        configured = capabilities.get("supported_reasoning_levels")
    if isinstance(configured, Iterable) and not isinstance(configured, (str, bytes, dict)):
        result: set[str] = set()
        for item in configured:
            value = item.get("effort") if isinstance(item, dict) else item
            if value is not None:
                result.add(str(value).strip().lower())
        if result:
            return result
    return set(SUPPORTED_REASONING_EFFORTS)


def _validate_image_url(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.strip():
        raise ChatToResponsesConversionError("图片输入缺少合法的 URL 或 data URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "data"}:
        raise ChatToResponsesConversionError("图片 URL 只支持 http(s) 或 data URL")
    return value


def _content_part(part: Any, *, role: str) -> dict[str, Any]:
    if not isinstance(part, dict):
        raise ChatToResponsesConversionError("消息 content 数组中的每一项必须是对象")
    part_type = str(part.get("type") or "")
    if part_type in _TEXT_PART_TYPES:
        text = part.get("text", "")
        if not isinstance(text, str):
            text = str(text)
        response_type = "output_text" if role == "assistant" else "input_text"
        return {"type": response_type, "text": text}
    if part_type in {"image_url", "input_image"}:
        raw_url = part.get("image_url") if part_type == "image_url" else part.get("image_url", part.get("url"))
        image_url = _validate_image_url(raw_url)
        converted: dict[str, Any] = {"type": "input_image", "image_url": image_url}
        detail = part.get("detail")
        if isinstance(raw_url, dict):
            detail = raw_url.get("detail", detail)
        if detail is not None:
            converted["detail"] = detail
        return converted
    raise ChatToResponsesConversionError(f"不支持的 Chat 内容类型: {part_type or 'missing'}")


def _message_content(content: Any, *, role: str) -> str | list[dict[str, Any]] | None:
    if content is None:
        return None
    if isinstance(content, str):
        if role == "tool":
            return content
        response_type = "output_text" if role == "assistant" else "input_text"
        return [{"type": response_type, "text": content}]
    if isinstance(content, list):
        return [_content_part(part, role=role) for part in content]
    raise ChatToResponsesConversionError("消息 content 必须是字符串、数组或 null")


def _instruction_text(message: Any) -> str:
    """Return text suitable for Responses ``instructions``."""
    if not isinstance(message, dict):
        raise ChatToResponsesConversionError("messages 中的每一项必须是对象")
    role = str(message.get("role") or "").strip().lower()
    if role not in {"system", "developer"}:
        raise ChatToResponsesConversionError("仅 system/developer 消息可转换为 instructions")
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ChatToResponsesConversionError("system/developer 消息 content 必须是字符串、文本数组或 null")
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise ChatToResponsesConversionError("system/developer 消息 content 数组中的每一项必须是对象")
        part_type = str(part.get("type") or "")
        if part_type not in _TEXT_PART_TYPES:
            raise ChatToResponsesConversionError("system/developer 消息只支持文本内容")
        text = part.get("text", "")
        texts.append(text if isinstance(text, str) else str(text))
    return "\n".join(texts)


def _function_call_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ChatToResponsesConversionError("assistant.tool_calls 必须是数组")
    items = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise ChatToResponsesConversionError("tool_calls 中的每一项必须是对象")
        if raw_call.get("type", "function") != "function":
            raise ChatToResponsesConversionError("仅支持 type=function 的 Chat 工具调用")
        function = raw_call.get("function")
        if not isinstance(function, dict) or not str(function.get("name") or "").strip():
            raise ChatToResponsesConversionError("工具调用缺少 function.name")
        call_id = str(raw_call.get("id") or raw_call.get("call_id") or "").strip()
        if not call_id:
            raise ChatToResponsesConversionError("工具调用缺少 id")
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        items.append({
            "type": "function_call",
            "call_id": call_id,
            "name": str(function["name"]),
            "arguments": arguments,
            "status": "completed",
        })
    return items


def _message_items(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        raise ChatToResponsesConversionError("messages 中的每一项必须是对象")
    role = str(message.get("role") or "").strip().lower()
    if role not in _SUPPORTED_ROLES:
        raise ChatToResponsesConversionError(f"不支持的消息角色: {role or 'missing'}")

    if role == "tool":
        call_id = str(message.get("tool_call_id") or "").strip()
        if not call_id:
            raise ChatToResponsesConversionError("tool 消息缺少 tool_call_id")
        output = _message_content(message.get("content", ""), role="tool")
        return [{"type": "function_call_output", "call_id": call_id, "output": output or ""}]

    items: list[dict[str, Any]] = []
    content = _message_content(message.get("content"), role=role)
    if content is not None:
        items.append({"type": "message", "role": role, "content": content})
    items.extend(_function_call_items(message) if role == "assistant" else [])
    if not items:
        items.append({"type": "message", "role": role, "content": []})
    return items


def _convert_tool(raw_tool: Any) -> dict[str, Any]:
    if not isinstance(raw_tool, dict):
        raise ChatToResponsesConversionError("tools 中的每一项必须是对象")
    if raw_tool.get("type") != "function":
        raise ChatToResponsesConversionError(
            f"Responses 适配器不支持工具类型: {raw_tool.get('type', 'missing')}"
        )
    function = raw_tool.get("function")
    if not isinstance(function, dict) or not str(function.get("name") or "").strip():
        raise ChatToResponsesConversionError("function tool 缺少 function.name")
    parameters = function.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        raise ChatToResponsesConversionError("function.parameters 必须是对象")
    converted: dict[str, Any] = {
        "type": "function",
        "name": str(function["name"]),
        "description": str(function.get("description") or ""),
        "parameters": copy.deepcopy(parameters),
    }
    if "strict" in function:
        converted["strict"] = bool(function["strict"])
    return converted


def _convert_tool_choice(value: Any, *, supports_required: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in {"auto", "none", "required"}:
            raise ChatToResponsesConversionError(f"不支持的 tool_choice: {value}")
        if value == "required" and not supports_required:
            raise ChatToResponsesConversionError("当前 Responses 模型不支持 tool_choice=required")
        return value
    if isinstance(value, dict) and value.get("type") == "function":
        function = value.get("function")
        name = function.get("name") if isinstance(function, dict) else value.get("name")
        if not str(name or "").strip():
            raise ChatToResponsesConversionError("指定工具选择缺少函数名")
        return {"type": "function", "name": str(name)}
    raise ChatToResponsesConversionError("tool_choice 必须是 auto、none、required 或指定函数")


def convert_chat_request_to_responses(
    body: dict[str, Any],
    upstream_model: str,
    *,
    model_entry: dict[str, Any] | None = None,
    on_drop: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Return a new, upstream-safe Responses request without mutating ``body``."""
    if not isinstance(body, dict):
        raise ChatToResponsesConversionError("请求体必须是 JSON 对象")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ChatToResponsesConversionError("Chat 请求缺少 messages 数组")
    input_items: list[dict[str, Any]] = []
    instructions: list[str] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower() if isinstance(message, dict) else ""
        if role in {"system", "developer"}:
            text = _instruction_text(message)
            if text:
                instructions.append(text)
            continue
        input_items.extend(_message_items(message))

    capabilities = _capabilities(model_entry)
    payload: dict[str, Any] = {
        "model": upstream_model,
        "input": input_items,
        "stream": bool(body.get("stream", False)),
        "store": False,
    }
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)

    if body.get("max_completion_tokens") is not None:
        payload["max_output_tokens"] = body["max_completion_tokens"]
    elif body.get("max_tokens") is not None:
        payload["max_output_tokens"] = body["max_tokens"]

    if body.get("reasoning_effort") is not None:
        effort = str(body["reasoning_effort"]).strip().lower()
        if capabilities.get("reasoning") is False:
            raise ChatToResponsesConversionError("当前 Responses 模型未启用 reasoning 能力")
        supported = _supported_reasoning_efforts(capabilities)
        if effort not in supported:
            raise ChatToResponsesConversionError(
                f"Responses 模型不支持 reasoning_effort={effort!r}，支持值: {', '.join(sorted(supported))}"
            )
        payload["reasoning"] = {"effort": effort}

    if "parallel_tool_calls" in body:
        supports_parallel = bool(capabilities.get("parallel_tool_calls", capabilities.get("parallel_tools", True)))
        if supports_parallel:
            payload["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
        elif on_drop:
            on_drop("parallel_tool_calls")

    if body.get("tools") is not None:
        raw_tools = body.get("tools")
        if not isinstance(raw_tools, list):
            raise ChatToResponsesConversionError("tools 必须是数组")
        payload["tools"] = [_convert_tool(tool) for tool in raw_tools]
        if "tool_choice" in body:
            payload["tool_choice"] = _convert_tool_choice(
                body.get("tool_choice"),
                supports_required=bool(capabilities.get("required_tool_choice", True)),
            )
    elif body.get("tool_choice") not in (None, "none"):
        raise ChatToResponsesConversionError("tool_choice 不能在没有 tools 时使用")

    # Responses model capability declarations may opt into these optional
    # parameters.  Unknown Chat-only fields are intentionally not forwarded.
    optional_capability_fields = {
        "temperature": "supports_temperature",
        "top_p": "supports_top_p",
        "stop": "supports_stop",
        "seed": "supports_seed",
    }
    for field, capability in optional_capability_fields.items():
        if body.get(field) is not None:
            if capabilities.get(capability, False):
                payload[field] = copy.deepcopy(body[field])
            elif on_drop:
                on_drop(field)

    return payload
