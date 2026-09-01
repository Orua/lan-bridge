"""Convert a completed Responses payload into Chat Completions JSON."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


class ResponsesToChatConversionError(ValueError):
    code = "responses_upstream_error"
    status_code = 502


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
    )


def _usage(usage: Any) -> dict[str, int]:
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    completion = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    total = usage.get("total_tokens", int(prompt) + int(completion)) or 0
    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
    }


def _incomplete_finish_reason(response: dict[str, Any]) -> str:
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else response.get("reason")
    if reason in {"max_output_tokens", "max_tokens", "length"}:
        return "length"
    if reason in {"content_filter", "safety"}:
        return "content_filter"
    return "stop"


def convert_responses_response_to_chat(
    response: dict[str, Any],
    chat_model: str,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ResponsesToChatConversionError("Responses 上游返回的 JSON 顶层不是对象")
    status = str(response.get("status") or "completed")
    if status == "failed":
        error = response.get("error")
        message = error.get("message") if isinstance(error, dict) else "Responses 上游返回 failed 状态"
        raise ResponsesToChatConversionError(str(message))

    output = response.get("output")
    if not isinstance(output, list):
        output = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text_parts.append(_text_from_content(item.get("content")))
        elif item_type == "output_text":
            text_parts.append(str(item.get("text") or ""))
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if not call_id:
                raise ResponsesToChatConversionError("Responses function_call 缺少 call_id")
            arguments = item.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": arguments,
                },
            })

    content = "".join(text_parts)
    if not content and not tool_calls:
        top_level_text = response.get("output_text")
        if isinstance(top_level_text, str):
            content = top_level_text

    if tool_calls:
        finish_reason = "tool_calls"
    elif status == "incomplete":
        finish_reason = _incomplete_finish_reason(response)
    else:
        finish_reason = "stop"

    message: dict[str, Any] = {
        "role": "assistant",
        "content": content or None,
        "tool_calls": tool_calls,
    }
    return {
        "id": f"chatcmpl-bridge-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": chat_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _usage(response.get("usage")),
    }
