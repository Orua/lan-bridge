"""OpenAI-compatible generic adapter.

Use this when the user supplies the concrete API address and no vendor-specific
request or endpoint rewriting should be applied.
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class OpenAICompatibleAdapter(BaseAdapter):
    name = "openai"
    base_url = ""
    api_key_env = "OPENAI_API_KEY"

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        tools = chat_req.get("tools")
        if tools:
            for tool in tools:
                if "function" not in tool and "name" in tool:
                    tool["function"] = {
                        "name": tool.pop("name"),
                        "description": tool.pop("description", ""),
                        "parameters": tool.pop("parameters", {}),
                    }
                tool.setdefault("type", "function")
                params = tool.get("function", {}).get("parameters")
                if not params or not isinstance(params, dict):
                    tool.setdefault("function", {})["parameters"] = {"type": "object", "properties": {}}
                elif params.get("type") != "object":
                    params["type"] = "object"
                    params.setdefault("properties", {})
        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        for choice in chat_resp.get("choices", []):
            msg = choice.get("message", {})
            for tool_call in msg.get("tool_calls") or []:
                tool_call.setdefault("type", "function")
                func = tool_call.get("function", {})
                if isinstance(func.get("arguments"), dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
        return chat_resp

    def preprocess_image_gen_request(self, req: dict) -> dict:
        base = self.base_url.lower()
        if "volces.com" in base or "ark.cn-" in base:
            req.pop("size", None)
        return req

    def stream_event_transform(self, raw_event: dict) -> dict:
        for choice in raw_event.get("choices", []):
            delta = choice.get("delta", {})
            for tool_call in delta.get("tool_calls") or []:
                tool_call.setdefault("type", "function")
                func = tool_call.get("function", {})
                if isinstance(func.get("arguments"), dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
        return raw_event
