"""Translate Responses SSE events into Chat Completions SSE chunks."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any


logger = logging.getLogger("lan-bridge.protocol-adapters")


def _chunk(chunk_id: str, model: str, delta: dict[str, Any], *, finish_reason: str | None = None, usage: dict[str, int] | None = None) -> str:
    payload: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _error(message: str, request_id: str) -> str:
    payload = {
        "error": {
            "message": f"{message} (request_id={request_id})",
            "type": "responses_upstream_error",
            "code": "responses_upstream_error",
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _usage(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    completion = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total = int(usage.get("total_tokens", prompt + completion) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


async def _events(chunks: AsyncIterator[bytes | str]) -> AsyncIterator[dict[str, Any]]:
    buffer = ""
    async for chunk in chunks:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        buffer += chunk.replace("\r\n", "\n")
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            data = "\n".join(
                line[5:].lstrip()
                for line in block.split("\n")
                if line.startswith("data:")
            ).strip()
            if not data or data == "[DONE]":
                continue
            try:
                value = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("忽略无法解析的 Responses SSE 事件（request body 未记录）")
                continue
            if isinstance(value, dict):
                yield value
    if buffer.strip():
        data = "\n".join(line[5:].lstrip() for line in buffer.split("\n") if line.startswith("data:")).strip()
        if data and data != "[DONE]":
            try:
                value = json.loads(data)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                yield value


class ResponsesStreamToChat:
    """Stateful converter with stable tool indexes and no duplicate deltas."""

    def __init__(self, model: str, request_id: str):
        self.model = model
        self.request_id = request_id
        self.chunk_id = f"chatcmpl-bridge-{uuid.uuid4().hex}"
        self.started = False
        self.completed = False
        self.incomplete = False
        self.failed = False
        self.terminal = False
        self.text = ""
        self.tool_indexes: dict[str, int] = {}
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self._next_tool_index = 0
        self.usage: dict[str, int] | None = None

    def _start(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        return [_chunk(self.chunk_id, self.model, {"role": "assistant"})]

    def _tool_state(self, event: dict[str, Any], item: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        item = item or {}
        output_index = event.get("output_index", item.get("output_index"))
        call_id = str(event.get("call_id") or item.get("call_id") or item.get("id") or "")
        key = str(output_index if output_index is not None else call_id)
        if key not in self.tool_indexes and call_id:
            key = next(
                (existing_key for existing_key, existing in self.tool_calls.items() if existing.get("call_id") == call_id),
                key,
            )
        if key not in self.tool_indexes:
            self.tool_indexes[key] = self._next_tool_index
            self._next_tool_index += 1
        state = self.tool_calls.setdefault(key, {
            "call_id": call_id,
            "name": str(item.get("name") or ""),
            "arguments": "",
        })
        if call_id and not state.get("call_id"):
            state["call_id"] = call_id
        if item.get("name") and not state.get("name"):
            state["name"] = str(item["name"])
        return key, state

    def _append_argument(self, state: dict[str, Any], value: Any) -> str:
        value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        current = str(state.get("arguments") or "")
        if not value:
            return ""
        if current and value == current:
            return ""
        if current and value.startswith(current):
            delta = value[len(current):]
            state["arguments"] = value
            return delta
        state["arguments"] = current + value
        return value

    @staticmethod
    def _item_text(item: dict[str, Any]) -> str:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
            )
        return ""

    def _finish_reason(self, response: dict[str, Any], *, default: str = "stop") -> str:
        if self.tool_calls:
            return "tool_calls"
        if response.get("status") == "incomplete":
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else response.get("reason")
            if reason in {"max_output_tokens", "max_tokens", "length"}:
                return "length"
            if reason in {"content_filter", "safety"}:
                return "content_filter"
        return default

    async def convert(self, chunks: AsyncIterator[bytes | str]) -> AsyncIterator[str]:
        async for event in _events(chunks):
            event_type = event.get("type")
            emitted = self._start()
            if event_type == "response.output_text.delta":
                value = event.get("delta")
                if isinstance(value, str) and value:
                    self.text += value
                    emitted.append(_chunk(self.chunk_id, self.model, {"content": value}))
            elif event_type == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    key, state = self._tool_state(event, item)
                    emitted.append(_chunk(self.chunk_id, self.model, {
                        "tool_calls": [{
                            "index": self.tool_indexes[key],
                            "id": state.get("call_id") or "",
                            "type": "function",
                            "function": {"name": state.get("name") or "", "arguments": ""},
                        }]
                    }))
                    initial_arguments = self._append_argument(state, item.get("arguments", ""))
                    if initial_arguments:
                        emitted.append(_chunk(self.chunk_id, self.model, {
                            "tool_calls": [{
                                "index": self.tool_indexes[key],
                                "function": {"arguments": initial_arguments},
                            }]
                        }))
            elif event_type == "response.function_call_arguments.delta":
                key, state = self._tool_state(event)
                delta = self._append_argument(state, event.get("delta", ""))
                if delta:
                    emitted.append(_chunk(self.chunk_id, self.model, {
                        "tool_calls": [{
                            "index": self.tool_indexes[key],
                            "function": {"arguments": delta},
                        }]
                    }))
            elif event_type == "response.function_call_arguments.done":
                key, state = self._tool_state(event)
                delta = self._append_argument(state, event.get("arguments", ""))
                if delta:
                    emitted.append(_chunk(self.chunk_id, self.model, {
                        "tool_calls": [{"index": self.tool_indexes[key], "function": {"arguments": delta}}]
                    }))
            elif event_type == "response.output_item.done":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    key, state = self._tool_state(event, item)
                    delta = self._append_argument(state, item.get("arguments", ""))
                    if delta:
                        emitted.append(_chunk(self.chunk_id, self.model, {
                            "tool_calls": [{"index": self.tool_indexes[key], "function": {"arguments": delta}}]
                        }))
                elif isinstance(item, dict) and item.get("type") == "message":
                    value = self._item_text(item)
                    if value.startswith(self.text):
                        value = value[len(self.text):]
                    else:
                        value = ""
                    if value:
                        self.text += value
                        emitted.append(_chunk(self.chunk_id, self.model, {"content": value}))
            elif event_type in {"response.completed", "response.incomplete"}:
                response = event.get("response") if isinstance(event.get("response"), dict) else {}
                self.usage = _usage(response.get("usage"))
                emitted.append(_chunk(
                    self.chunk_id,
                    self.model,
                    {},
                    finish_reason=self._finish_reason(response),
                    usage=self.usage,
                ))
                emitted.append("data: [DONE]\n\n")
                self.completed = event_type == "response.completed"
                self.incomplete = event_type == "response.incomplete"
                self.terminal = True
            elif event_type == "response.failed":
                self.failed = True
                self.terminal = True
                emitted.append(_error("Responses 上游请求失败", self.request_id))
                emitted.append("data: [DONE]\n\n")

            for value in emitted:
                yield value

        if not self.terminal:
            # A compliant Responses stream ends with response.completed or
            # response.incomplete. Still close a prematurely ended upstream.
            for value in self._start():
                yield value
            yield _error("Responses 上游流提前结束", self.request_id)
            yield "data: [DONE]\n\n"
