"""Pydantic 数据模型 —— 定义 Responses API 和 Chat Completions API 的结构"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ── Chat Completions API 模型 ──────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatFunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ChatToolDef(BaseModel):
    type: Literal["function"] = "function"
    function: ChatFunctionDef


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict | None = None


# ── Responses API 模型 ─────────────────────────────────────────────

class ResponsesInputItem(BaseModel):
    role: str
    content: str | list[dict] | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ResponsesRequest(BaseModel):
    model: str
    input: list[dict[str, Any]] = Field(default_factory=list)
    instructions: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict | None = "auto"
    previous_response_id: str | None = None
    metadata: dict[str, Any] | None = None


# ── Responses API 输出项 ───────────────────────────────────────────

def make_output_text(text: str, annotations: list[dict] | None = None) -> dict:
    return {"type": "output_text", "text": text, "annotations": annotations or []}


def make_message_output_item(content_text: str, annotations: list[dict] | None = None) -> dict:
    item_id = _uid("msg")
    return {
        "id": item_id,
        "object": "realtime.item",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [make_output_text(content_text, annotations)],
    }


def make_reasoning_output_item(reasoning_content: str) -> dict:
    """Preserve provider reasoning for a subsequent tool-call turn."""
    return {
        "id": _uid("rs"),
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": reasoning_content}],
    }


def make_function_call_output_item(
    name: str,
    arguments: str,
    call_id: str | None = None,
    namespace: str | None = None,
) -> dict:
    fc_id = call_id or _uid("call")
    item_id = _uid("fc")
    item = {
        "id": item_id,
        "object": "realtime.item",
        "type": "function_call",
        "name": name,
        "call_id": fc_id,
        "arguments": arguments,
        "status": "completed",
    }
    if namespace:
        item["namespace"] = namespace
    return item


def make_custom_tool_call_output_item(name: str, input_text: str, call_id: str | None = None) -> dict:
    item_id = _uid("ctc")
    return {
        "id": item_id,
        "object": "realtime.item",
        "type": "custom_tool_call",
        "name": name,
        "call_id": call_id or _uid("call"),
        "input": input_text,
        "status": "completed",
    }


def make_web_search_call_output_item(query: str, results: list[dict]) -> dict:
    return {
        "id": _uid("ws"),
        "type": "web_search_call",
        "status": "completed",
        "action": {
            "type": "search",
            "query": query,
            "queries": [query],
            "sources": [
                {
                    "type": "url",
                    "url": result.get("url", ""),
                    "title": result.get("title", ""),
                }
                for result in results
                if result.get("url")
            ],
        },
    }


def make_responses_usage(usage: dict | None = None) -> dict:
    """Convert Chat Completions token usage into the Responses wire shape."""
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": usage.get("input_tokens_details"),
        "output_tokens": output_tokens,
        "output_tokens_details": usage.get("output_tokens_details"),
        "total_tokens": total_tokens,
    }


# ── Responses API 非流式响应 ───────────────────────────────────────

def build_responses_response(
    output_items: list[dict],
    model: str,
    usage: dict | None = None,
) -> dict:
    normalized_usage = make_responses_usage(usage)
    return {
        "id": _uid("resp"),
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output_items,
        "usage": normalized_usage,
    }


# ── 错误响应 ───────────────────────────────────────────────────────

def build_error_response(message: str, code: str = "internal_error", status_code: int = 500) -> dict:
    return {
        "error": {
            "message": message,
            "type": code,
            "code": status_code,
        },
    }
