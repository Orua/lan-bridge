"""DeepSeek 适配器"""

from __future__ import annotations

import json

from .base import BaseAdapter


class DeepSeekAdapter(BaseAdapter):
    name = "deepseek"
    base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # DeepSeek 不支持 logprobs
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # stop 只支持单个字符串或字符串列表，DeepSeek 支持列表
        stop = chat_req.get("stop")
        if isinstance(stop, list) and len(stop) > 4:
            chat_req["stop"] = stop[:4]

        # 支持 Codex 的 _codex_reasoning_effort 和 WorkBuddy 的 reasoning_effort
        effort = str(chat_req.pop("_codex_reasoning_effort", "")).lower()
        if not effort:
            # WorkBuddy chat 请求可能带 reasoning_effort 字段
            wb_effort = str(chat_req.get("reasoning_effort", "")).lower()
            if wb_effort and wb_effort != "none":
                effort = wb_effort

        # Low explicitly disables thinking. Medium leaves DeepSeek at its own
        # default. Higher settings explicitly enable thinking.
        if "thinking" not in chat_req:
            if effort in {"none", "off", "disabled", "minimal", "low"}:
                chat_req["thinking"] = {"type": "disabled"}
                chat_req.pop("reasoning_effort", None)
            elif effort in {"high", "xhigh", "max"}:
                chat_req["thinking"] = {"type": "enabled"}
                if effort in {"xhigh", "max"}:
                    chat_req["reasoning_effort"] = "max"
                elif effort == "high":
                    chat_req["reasoning_effort"] = "high"

        # DeepSeek rejects forced tool selection while thinking mode is active.
        # A forced choice is more specific than the reasoning preference, so
        # disable thinking for this request and preserve the requested tool.
        tool_choice = chat_req.get("tool_choice")
        if tool_choice not in (None, "auto", "none"):
            chat_req["thinking"] = {"type": "disabled"}
            chat_req.pop("reasoning_effort", None)

        # DeepSeek 对 tool 格式有要求，确保 function 字段存在
        tools = chat_req.get("tools")
        if tools:
            for tool in tools:
                if "function" not in tool and "name" in tool:
                    tool["function"] = {
                        "name": tool.pop("name"),
                        "description": tool.pop("description", ""),
                        "parameters": tool.pop("parameters", {}),
                    }
                if "type" not in tool:
                    tool["type"] = "function"
                # 修复 parameters: 必须是 type: "object" 的 JSON Schema
                params = tool.get("function", {}).get("parameters")
                if not params or not isinstance(params, dict):
                    tool.setdefault("function", {})["parameters"] = {"type": "object", "properties": {}}
                elif params.get("type") != "object":
                    params["type"] = "object"
                    params.setdefault("properties", {})

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        """处理 DeepSeek 非流式响应"""
        choices = chat_resp.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        return chat_resp

    def stream_event_transform(self, raw_event: dict) -> dict:
        """DeepSeek SSE 格式基本标准，做微调"""
        for choice in raw_event.get("choices", []):
            delta = choice.get("delta", {})
            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        return raw_event

    def extract_tool_calls_from_content(self, content: str) -> list[dict] | None:
        return None  # DeepSeek 原生支持 tool_calls，无需提取
