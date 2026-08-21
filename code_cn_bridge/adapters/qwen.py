"""通义千问 (Qwen) 适配器"""

from __future__ import annotations

import json
import re

from .base import BaseAdapter


class QwenAdapter(BaseAdapter):
    name = "qwen"
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env = "QWEN_API_KEY"

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # 移除千问不支持的字段
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # Qwen is used as a short-lived visual submodel for image tool output.
        # Keep that hop direct: reasoning is performed by the routed text model,
        # while visual thinking can yield reasoning-only retries and extra tokens.
        if self._contains_image(chat_req.get("messages", [])):
            chat_req["enable_thinking"] = False

        # stop 只支持字符串数组或单个字符串
        stop = chat_req.get("stop")
        if isinstance(stop, list):
            chat_req["stop"] = stop  # 千问支持列表
        return chat_req

    @staticmethod
    def _contains_image(messages: list[dict]) -> bool:
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if any(part.get("type") == "image_url" for part in content if isinstance(part, dict)):
                return True
        return False

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        """处理千问的非流式响应"""
        choices = chat_resp.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            content = msg.get("content", "")

            # 千问可能将 tool_calls 嵌套在 content 的 JSON 中
            if content and isinstance(content, str):
                extracted = self.extract_tool_calls_from_content(content)
                if extracted:
                    msg["tool_calls"] = extracted
                    msg["content"] = None  # 有 tool_calls 时 content 应为空

            # 确保 tool_calls 结构正确
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        return chat_resp

    def stream_event_transform(self, raw_event: dict) -> dict:
        """千问 SSE 事件可能为 {"output": {"choices": [...]}} 格式"""
        # 提取 output.choices
        if "output" in raw_event and "choices" not in raw_event:
            output = raw_event["output"]
            if isinstance(output, dict) and "choices" in output:
                raw_event["choices"] = output["choices"]

        # 确保 choices 存在
        if "choices" not in raw_event:
            return raw_event

        for choice in raw_event.get("choices", []):
            delta = choice.get("delta", {})
            tool_calls = delta.get("tool_calls", [])

            # 确保 tool_call 有 type
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                # arguments 可能是 dict
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        return raw_event

    def extract_tool_calls_from_content(self, content: str) -> list[dict] | None:
        """从 content 中提取 tool_calls (千问旧版可能把 tool_call 放在 content 里)"""
        if not content:
            return None

        # 尝试匹配 <tool_call>{"name": "...", "arguments": {...}}</tool_call>
        pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
        matches = re.findall(pattern, content, re.DOTALL)
        if not matches:
            return None

        tool_calls = []
        for i, m in enumerate(matches):
            try:
                data = json.loads(m)
                tool_calls.append({
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": data.get("name", ""),
                        "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                    },
                })
            except json.JSONDecodeError:
                # 尝试函数调用格式
                fn_match = re.match(
                    r'(\w+)\s*\((.*)\)', m.strip(), re.DOTALL
                )
                if fn_match:
                    tool_calls.append({
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": fn_match.group(1),
                            "arguments": fn_match.group(2).strip(),
                        },
                    })

        return tool_calls if tool_calls else None

    # ── 生图 API ───────────────────────────────────────────────

    def build_image_gen_url(self) -> str:
        """千问生图使用 DashScope 多模态生成端点

        Token Plan 用户路径不同，需要保留原始 base_url 的 host 部分。
        """
        from urllib.parse import urlparse
        base = self.base_url.rstrip("/")
        parsed = urlparse(base)
        host = parsed.netloc
        return f"https://{host}/api/v1/services/aigc/multimodal-generation/generation"

    def preprocess_image_gen_request(self, req: dict) -> dict:
        """千问生图需要 DashScope 格式: input.messages 而非 DALL-E prompt"""
        prompt = req.pop("prompt", "")
        size = str(req.pop("size", "")).strip()
        if size.lower() == "auto":
            size = "1024*1024"
        else:
            size = size.replace("x", "*").replace("X", "*")
        req["input"] = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}]
        }
        if size:
            req["parameters"] = {"size": size}
        else:
            req["parameters"] = {}
        return req
