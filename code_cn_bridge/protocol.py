"""协议转换引擎 —— OpenAI Responses API ↔ Chat Completions API 双向转换

包括：
- 请求转换 (Responses → Chat)
- 非流式响应转换 (Chat → Responses)
- 流式 SSE 转换 (Chat SSE → Responses SSE)
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from datetime import date
from typing import AsyncIterator

from .models import (
    _uid,
    build_responses_response,
    build_error_response,
    make_function_call_output_item,
    make_custom_tool_call_output_item,
    make_output_text,
    make_message_output_item,
    make_responses_usage,
    make_reasoning_output_item,
)
from .adapters.base import BaseAdapter


logger = logging.getLogger("lan-bridge.protocol")

_EMPTY_UPSTREAM_RESPONSE_MESSAGE = (
    "Upstream model completed without returning text or tool calls. "
    "For image requests, verify that a working multimodal model is configured."
)
_REASONING_ONLY_RESPONSE_MESSAGE = (
    "Upstream thinking mode ended after reasoning without returning a final answer "
    "or a tool call."
)
_TOOL_USE_GUARDRAIL_MESSAGE = (
    "When a tool is available for an action, call the tool using tool_calls. "
    "Do not print shell commands, Python scripts, JSON tool payloads, or patch text "
    "in the assistant message as a substitute for calling the tool. If you need to "
    "inspect files, edit files, run commands, search tools, or use a browser, emit "
    "the matching tool call and wait for the tool result."
)
_IMAGE_GEN_GUARDRAIL_MESSAGE = (
    "The image_gen tool is available in this request. For user requests to create, "
    "generate, draw, render, design, or visualize a new raster image, you MUST call "
    "the image_gen tool with a complete prompt. Do not use shell commands, Python, "
    "local imagegen skills, local CLI scripts, OPENAI_API_KEY, or filesystem searches "
    "as substitutes for image generation. The bridge will execute image_gen through "
    "the configured image model."
)
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


# ═══════════════════════════════════════════════════════════════════
# 请求转换: Responses API → Chat Completions API
# ═══════════════════════════════════════════════════════════════════

def translate_request(
    responses_body: dict,
    adapter: BaseAdapter,
    target_model: str,
) -> dict:
    """将 Responses API 请求转换为 Chat Completions API 请求"""
    messages = _map_input_to_messages(responses_body.get("input", []))

    # instructions → 前缀 system 消息（code 的系统提示词）
    instructions = responses_body.get("instructions", "").strip()
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})

    chat_req: dict = {
        "model": target_model,
        "messages": messages,
        "stream": responses_body.get("stream", False),
    }
    if chat_req["stream"]:
        raw_stream_options = responses_body.get("stream_options")
        stream_options = dict(raw_stream_options) if isinstance(raw_stream_options, dict) else {}
        stream_options["include_usage"] = True
        chat_req["stream_options"] = stream_options

    # 可选参数映射
    _map_optional(responses_body, chat_req, "temperature")
    _map_optional(responses_body, chat_req, "top_p")
    _map_optional(responses_body, chat_req, "stop")

    # max_output_tokens → max_tokens
    if "max_output_tokens" in responses_body:
        chat_req["max_tokens"] = responses_body["max_output_tokens"]

    if adapter.name == "deepseek":
        reasoning = responses_body.get("reasoning") or {}
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        effort = effort or responses_body.get("reasoning_effort")
        if effort:
            chat_req["_codex_reasoning_effort"] = str(effort).lower()

    # tools: 确保每个 tool 都有 type: "function"，过滤空名工具
    tools = list(responses_body.get("tools") or [])
    tools.extend(_collect_loaded_tools_from_input(responses_body.get("input", [])))
    has_image_gen = False
    has_web_search = False
    namespace_tools: dict[str, dict[str, str]] = {}
    custom_tool_names: set[str] = set()
    response_tool_types: dict[str, str] = {}
    if tools:
        normalized = []
        seen_tool_names: set[str] = set()
        for raw_tool in tools:
            t = _coerce_tool(raw_tool)
            if not t:
                continue
            tool_type = t.get("type", "function")
            if tool_type in ("image_gen", "image_generation"):
                has_image_gen = True
                normalized.append(_make_image_gen_tool(t))
                response_tool_types["image_gen"] = "image_generation_call"
            elif tool_type == "web_search":
                has_web_search = True
                normalized.append(_make_web_search_tool())
            elif tool_type == "local_shell":
                normalized_tool = _make_local_shell_tool(t)
                normalized.append(normalized_tool)
                response_tool_types[normalized_tool["function"]["name"]] = "local_shell_call"
            elif tool_type == "tool_search":
                normalized_tool = _make_tool_search_tool(t)
                normalized.append(normalized_tool)
                response_tool_types[normalized_tool["function"]["name"]] = "tool_search_call"
            elif tool_type == "computer_use":
                normalized_tool = _make_builtin_custom_tool(t, "computer_use")
                if normalized_tool is not None:
                    normalized.append(normalized_tool)
                    custom_tool_names.add(normalized_tool["function"]["name"])
            elif tool_type == "namespace":
                for exposed_name, normalized_tool, identity in _expand_namespace_tools(t):
                    if exposed_name in seen_tool_names:
                        continue
                    seen_tool_names.add(exposed_name)
                    normalized.append(normalized_tool)
                    namespace_tools[exposed_name] = identity
            elif tool_type == "custom":
                normalized_tool = _normalize_custom_tool(t)
                if normalized_tool is not None:
                    name = normalized_tool["function"]["name"]
                    if name not in seen_tool_names:
                        seen_tool_names.add(name)
                        normalized.append(normalized_tool)
                    custom_tool_names.add(name)
            else:
                normalized_tool = _normalize_tool(t)
                if normalized_tool is not None:
                    name = normalized_tool["function"]["name"]
                    if name not in seen_tool_names:
                        seen_tool_names.add(name)
                        normalized.append(normalized_tool)
        normalized = [t for t in normalized if t.get("function", {}).get("name", "").strip()]
        if normalized:
            chat_req["tools"] = normalized
            messages.insert(0, {"role": "system", "content": _TOOL_USE_GUARDRAIL_MESSAGE})
        if has_image_gen:
            messages.insert(0, {"role": "system", "content": _IMAGE_GEN_GUARDRAIL_MESSAGE})
            chat_req["_has_image_gen"] = True
        if has_web_search:
            chat_req["_has_web_search"] = True
        if namespace_tools:
            chat_req["_namespace_tools"] = namespace_tools
        if custom_tool_names:
            chat_req["_custom_tool_names"] = sorted(custom_tool_names)
        if response_tool_types:
            chat_req["_response_tool_types"] = response_tool_types

    # tool_choice
    tool_choice = responses_body.get("tool_choice")
    if tool_choice and chat_req.get("tools"):
        chat_req["tool_choice"] = _normalize_tool_choice(tool_choice)

    # 适配器预处理
    chat_req = adapter.preprocess_chat_request(chat_req)
    return chat_req


# 国产模型不支持的 role，映射到 system
_ROLE_MAP = {"developer": "system"}


def _collect_loaded_tools_from_input(input_items) -> list[dict]:
    """Collect tools returned by client-side tool_search for Chat providers."""
    loaded_tools: list[dict] = []
    if not isinstance(input_items, list):
        return loaded_tools
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "tool_search_output":
            continue
        tools = item.get("tools")
        if isinstance(tools, list):
            loaded_tools.extend(t for t in tools if isinstance(t, dict))
    return loaded_tools


def _extract_reasoning_text(item: dict) -> str:
    """从 reasoning 类型的 item 中提取文本"""
    parts = []
    for field in ("summary", "content"):
        for part in item.get(field, []) or []:
            text = part.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _map_input_to_messages(input_items: list[dict]) -> list[dict]:
    """将 Responses API 的 input 数组映射为 Chat 的 messages 数组"""
    messages = []
    pending_tool_calls: list[dict] = []  # 收集连续的 function_call
    pending_reasoning: str = ""  # 收集 reasoning 文本，附加到紧随的 assistant 消息
    pending_tool_images: list[dict] = []

    def _flush_tool_calls():
        """提交收集中的 tool_calls，附带 reasoning_content（Kimi 等 thinking 模型需要）"""
        nonlocal pending_reasoning
        if pending_tool_calls:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": pending_tool_calls.copy(),
            }
            # Kimi thinking 模型要求所有带 tool_calls 的 assistant 消息必须有 reasoning_content
            msg["reasoning_content"] = pending_reasoning or "Tool calls."
            pending_reasoning = ""
            messages.append(msg)
            pending_tool_calls.clear()

    def _flush_tool_images():
        if pending_tool_images:
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "The preceding tool returned the following image for visual inspection.",
                    },
                    *pending_tool_images,
                ],
            })
            pending_tool_images.clear()

    for item in input_items:
        item_type = item.get("type", "")

        # reasoning → 收集文本，附加到下一个 assistant 消息
        if item_type == "reasoning":
            _flush_tool_images()
            pending_reasoning = _extract_reasoning_text(item) or pending_reasoning
            continue

        # hosted web_search 已由 Bridge 执行并体现在后续 assistant 答案中；
        # 不能将该 output item 作为 content=None 的用户消息转发给 Chat API。
        if item_type == "web_search_call":
            continue

        if item_type == "image_generation_call":
            _flush_tool_calls()
            _flush_tool_images()
            status = item.get("status", "completed")
            messages.append({
                "role": "assistant",
                "content": f"Image generation {status}.",
            })
            continue

        # function_call_output → tool role (工具调用结果)
        if item_type in ("function_call_output", "custom_tool_call_output", "tool_search_output", "tool_result"):
            _flush_tool_calls()

            # Keep the tool result paired with its call, then pass visual data
            # in a user message because chat providers expect images there.
            output = item.get("output", "")
            if isinstance(output, list):
                normalized = _normalize_content(output)
                if isinstance(normalized, str):
                    output = normalized
                else:
                    images = [
                        part for part in normalized or []
                        if isinstance(part, dict) and part.get("type") == "image_url"
                    ]
                    text = "".join(
                        part.get("text", "") for part in normalized or []
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    if images:
                        pending_tool_images.extend(images)
                        output = text or "Image output attached in the next message."
                    else:
                        output = text
            elif not isinstance(output, str):
                output = str(output)

            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("tool_use_id") or item.get("id", ""),
                "content": output,
            })
            continue

        _flush_tool_images()

        # function_call → 收集到 pending（合并连续多个为一条 assistant 消息）
        if item_type in ("function_call", "custom_tool_call", "local_shell_call", "tool_search_call", "tool_call"):
            arguments = item.get("arguments", "")
            if item_type == "custom_tool_call":
                arguments = _custom_tool_history_arguments(item.get("name", ""), item.get("input", ""))
            elif item_type == "local_shell_call":
                arguments = json.dumps(_local_shell_action_to_arguments(item.get("action", {})), ensure_ascii=False)
            elif item_type == "tool_search_call":
                arguments = json.dumps(item.get("arguments", {}), ensure_ascii=False)
            elif item_type == "tool_call":
                arguments = _tool_call_history_arguments(item)
            name = item.get("name", "")
            if item_type == "local_shell_call":
                name = "local_shell"
            elif item_type == "tool_search_call":
                name = "tool_search"
            if item_type == "function_call" and item.get("namespace"):
                name = _flatten_namespaced_name(str(item["namespace"]), name)
            tc = {
                "type": "function",
                "id": item.get("call_id") or item.get("id", ""),
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
            pending_tool_calls.append(tc)
            continue

        # 遇到非 function_call 的消息，先提交之前收集的 tool_calls
        _flush_tool_calls()

        role = item.get("role", "user")
        role = _ROLE_MAP.get(role, role)
        content = _normalize_content(item.get("content", ""))
        msg = {"role": role}
        if content is not None:
            msg["content"] = content or None
        if "name" in item:
            msg["name"] = item["name"]
        if "tool_call_id" in item:
            msg["tool_call_id"] = item["tool_call_id"]
        if "tool_calls" in item:
            msg["tool_calls"] = item["tool_calls"]
            if not msg.get("content"):
                msg["content"] = None

        # assistant 消息：附加之前收集的 reasoning_content
        if role == "assistant" and pending_reasoning:
            msg["reasoning_content"] = pending_reasoning
            pending_reasoning = ""

        messages.append(msg)

    # 末尾如果还有未提交的 tool_calls
    _flush_tool_calls()
    _flush_tool_images()

    # 末尾如果还有未消费的 reasoning（极少情况，附加到最后一个 assistant 消息）
    if pending_reasoning:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                m["reasoning_content"] = pending_reasoning
                break
        pending_reasoning = ""

    return _sanitize_tool_history(messages)


def _sanitize_tool_history(messages: list[dict]) -> list[dict]:
    """Remove incomplete tool-call turns before sending Chat Completions history.

    Responses clients can resume after an interrupted tool turn.  In that case
    the flat input history may contain a function_call without every matching
    function_call_output.  Chat Completions providers reject that history
    instead of allowing the conversation to recover.
    """
    sanitized: list[dict] = []
    index = 0

    while index < len(messages):
        message = messages[index]
        tool_calls = message.get("tool_calls") if message.get("role") == "assistant" else None

        if tool_calls:
            tool_messages: list[dict] = []
            next_index = index + 1
            while next_index < len(messages) and messages[next_index].get("role") == "tool":
                tool_messages.append(messages[next_index])
                next_index += 1

            outputs_by_id = {
                tool_message.get("tool_call_id"): tool_message
                for tool_message in tool_messages
                if tool_message.get("tool_call_id")
            }
            matched_calls = [
                tool_call for tool_call in tool_calls
                if tool_call.get("id") in outputs_by_id
            ]

            if matched_calls:
                assistant_message = message.copy()
                assistant_message["tool_calls"] = matched_calls
                sanitized.append(assistant_message)
                sanitized.extend(outputs_by_id[tool_call["id"]] for tool_call in matched_calls)
            elif message.get("content"):
                assistant_message = message.copy()
                assistant_message.pop("tool_calls", None)
                assistant_message.pop("reasoning_content", None)
                sanitized.append(assistant_message)

            index = next_index
            continue

        if message.get("role") != "tool":
            sanitized.append(message)
        index += 1

    return sanitized


def _normalize_content(content) -> str | list[dict] | None:
    """将 Responses API 的 content 格式转换为 Chat 格式

    Responses: [{"type": "input_text", "text": "Hello"}]
    Chat:      [{"type": "text", "text": "Hello"}]  或 纯字符串 "Hello"
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        has_text = False
        for part in content:
            ptype = part.get("type", "")
            # 映射 type 名
            if ptype == "input_text":
                parts.append({"type": "text", "text": part.get("text", "")})
                has_text = True
            elif ptype == "input_image":
                image_url = part.get("image_url", {})
                if isinstance(image_url, str):
                    image_url = {"url": image_url}
                parts.append({"type": "image_url", "image_url": image_url})
            elif ptype == "output_text":
                parts.append({"type": "text", "text": part.get("text", "")})
                has_text = True
            else:
                # 透传未知类型
                parts.append(part)
        # 如果只有一个纯文本，直接返回字符串
        if len(parts) == 1 and has_text:
            return parts[0]["text"]
        return parts if parts else None
    if content is None:
        return None
    return str(content)


def _coerce_tool(tool) -> dict | None:
    if isinstance(tool, str):
        return {"type": tool}
    if isinstance(tool, dict):
        return copy.deepcopy(tool)
    return None


def _make_image_gen_tool(tool: dict) -> dict:
    """将 code 内置 image_gen 工具转换为国产 LLM 可理解的 function tool"""
    return {
        "type": "function",
        "function": {
            "name": "image_gen",
            "description": (
                "Generate a new raster image through the bridge's configured image model. "
                "Use this tool directly whenever the user asks to create, draw, generate, "
                "render, design, or visualize an image, photo, illustration, character, poster, "
                "logo, scene, UI mockup, or other bitmap. Do not search for local imagegen "
                "skills, CLI scripts, Python packages, or API keys when this tool is available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "A detailed, structured image generation prompt describing exactly what to create, including subject, scene, style, composition, lighting, colors, and constraints. Write this as a complete production spec, not a casual description."
                    },
                },
                "required": ["prompt"]
            }
        }
    }


def _normalize_tool(tool: dict) -> dict | None:
    """确保 tool 格式为 {"type": "function", "function": {...}}"""
    tool_type = tool.get("type", "function")
    if tool_type == "custom" and tool.get("name") == "apply_patch":
        return _make_apply_patch_function_tool(tool)
    if tool_type != "function":
        logger.info(
            "Skipping unsupported Responses tool for Chat provider: type=%s name=%s",
            tool_type,
            tool.get("name", ""),
        )
        return None
    if "type" not in tool:
        tool = {"type": "function", **tool}
    if "function" not in tool:
        tool["function"] = {
            "name": tool.pop("name", ""),
            "description": tool.pop("description", ""),
            "parameters": tool.pop("parameters", {}),
        }
        if "strict" in tool:
            tool["function"]["strict"] = bool(tool.pop("strict"))
        tool["type"] = "function"
    elif "strict" in tool and "strict" not in tool["function"]:
        tool["function"]["strict"] = bool(tool["strict"])
    # 修复 parameters：必须是一个 type: "object" 的 JSON Schema
    params = tool["function"].get("parameters")
    if not params or not isinstance(params, dict):
        tool["function"]["parameters"] = {"type": "object", "properties": {}}
    elif params.get("type") != "object":
        params["type"] = "object"
        if "properties" not in params:
            params["properties"] = {}
    tool["function"]["parameters"] = _sanitize_json_schema(tool["function"]["parameters"])
    return tool


def _make_local_shell_tool(tool: dict) -> dict:
    name = str(tool.get("name") or "local_shell")
    description = str(tool.get("description") or "Run a local shell command through Codex.")
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command argv array to execute, for example [\"cmd\", \"/c\", \"dir\"].",
                },
                "working_directory": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 0},
            },
            "required": ["command"],
            "additionalProperties": False,
        }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _sanitize_json_schema(parameters),
        },
    }


def _make_tool_search_tool(tool: dict) -> dict:
    name = str(tool.get("name") or "tool_search")
    description = str(tool.get("description") or "Search available client-side tools.")
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for tool discovery."},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _sanitize_json_schema(parameters),
        },
    }


def _make_builtin_custom_tool(tool: dict, default_name: str) -> dict | None:
    custom_tool = copy.deepcopy(tool)
    custom_tool["type"] = "custom"
    custom_tool["name"] = str(custom_tool.get("name") or default_name)
    if not custom_tool.get("description"):
        custom_tool["description"] = f"Codex built-in tool: {custom_tool['name']}."
    return _normalize_custom_tool(custom_tool)


def _normalize_custom_tool(tool: dict) -> dict | None:
    name = str(tool.get("name", "")).strip()
    if not name:
        return None
    if name == "apply_patch":
        return _make_apply_patch_function_tool(tool)
    description = str(tool.get("description", "")).strip()
    if description:
        description = (
            f"{description}\n\nThis is a FREEFORM tool. Put the raw tool input text "
            "in the `input` parameter. Do not wrap the input in markdown."
        )
    else:
        description = f"FREEFORM custom tool: {name}. Put only the raw tool input text in `input`."
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Raw freeform input for this custom tool.",
                    }
                },
                "required": ["input"],
                "additionalProperties": False,
            },
        },
    }


def _make_apply_patch_function_tool(tool: dict) -> dict:
    grammar = tool.get("format", {}).get("definition", "")
    patch_instructions = (
        "Use Codex apply_patch format exactly, not git diff or unified diff headers. "
        "The patch must start with `*** Begin Patch` and end with `*** End Patch`. "
        "Use `*** Add File: path`, `*** Update File: path`, or `*** Delete File: path` hunks. "
        "For an added file, prefix every content line with `+`."
    )
    if grammar:
        patch_instructions = f"{patch_instructions}\n\nGrammar:\n{grammar}"
    return {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": patch_instructions,
            "parameters": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": patch_instructions,
                    }
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        },
    }


def _expand_namespace_tools(namespace_tool: dict) -> list[tuple[str, dict, dict[str, str]]]:
    """Expose Responses namespace tools as flat Chat functions for local execution."""
    namespace = str(namespace_tool.get("name", ""))
    if not namespace:
        return []
    expanded: list[tuple[str, dict, dict[str, str]]] = []
    for child in namespace_tool.get("tools", []) or []:
        if child.get("type", "function") != "function":
            continue
        child_name = str(child.get("name", ""))
        if not child_name:
            continue
        exposed_name = _flatten_namespaced_name(namespace, child_name)
        flattened = copy.deepcopy(child)
        flattened["name"] = exposed_name
        normalized = _normalize_tool(flattened)
        if normalized is not None:
            expanded.append((
                exposed_name,
                normalized,
                {"namespace": namespace, "name": child_name},
            ))
    return expanded


def _flatten_namespaced_name(namespace: str, name: str) -> str:
    if namespace.endswith("_"):
        return f"{namespace}{name}"
    return f"{namespace}__{name}"


def _make_web_search_tool() -> dict:
    """将 Responses hosted web_search 映射为 Bridge 内部执行的函数。"""
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for current or externally verifiable information. "
                "Use this for recent facts, documentation, news, or cited sources. "
                f"The bridge's current date is {date.today().isoformat()}; use it when "
                "interpreting requests such as today, latest, yesterday, or recent. "
                "Return results as a markdown paragraph. Inline citations should use clickable [source title](url) links where you reference the source, not bare [n] markers. List all sources at the end if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A focused web search query.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _restore_tool_identity(
    name: str,
    namespace_tools: dict[str, dict[str, str]] | None,
) -> tuple[str, str | None]:
    identity = (namespace_tools or {}).get(name)
    if not identity:
        return name, None
    return identity.get("name", name), identity.get("namespace")


def _custom_tool_input(name: str, arguments: str) -> str | None:
    return _custom_tool_input_with_names(name, arguments, None)


def _custom_tool_input_with_names(
    name: str,
    arguments: str,
    custom_tool_names: set[str] | None,
) -> str | None:
    try:
        parsed = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return arguments if name == "apply_patch" else None
    if name == "apply_patch" and isinstance(parsed, dict) and isinstance(parsed.get("patch"), str):
        return parsed["patch"]
    if (
        custom_tool_names is not None
        and name in custom_tool_names
        and isinstance(parsed, dict)
        and isinstance(parsed.get("input"), str)
    ):
        return parsed["input"]
    if name == "apply_patch":
        return arguments
    return None


def _custom_tool_history_arguments(name: str, input_value) -> str:
    input_text = _response_output_text(input_value)
    if name == "apply_patch":
        return json.dumps({"patch": input_text}, ensure_ascii=False)
    return json.dumps({"input": input_text}, ensure_ascii=False)


def _tool_call_history_arguments(item: dict) -> str:
    input_value = item.get("input", item.get("arguments", {}))
    if isinstance(input_value, str):
        return input_value
    return json.dumps(input_value, ensure_ascii=False)


def _local_shell_action_to_arguments(action) -> dict:
    if isinstance(action, dict) and isinstance(action.get("root"), dict):
        action = action["root"]
    if not isinstance(action, dict):
        return {"command": []}
    result = {
        "command": action.get("command") if isinstance(action.get("command"), list) else [],
    }
    for key in ("env", "timeout_ms", "user", "working_directory"):
        if action.get(key) is not None:
            result[key] = action[key]
    return result


def _arguments_to_local_shell_action(arguments: str) -> dict:
    parsed = _json_object(arguments)
    command = parsed.get("command")
    if not isinstance(command, list):
        cmd = parsed.get("cmd") or parsed.get("input") or arguments
        command = ["cmd", "/c", str(cmd)]
    action = {
        "type": "exec",
        "command": [str(part) for part in command],
    }
    for key in ("env", "timeout_ms", "user", "working_directory"):
        if parsed.get(key) is not None:
            action[key] = parsed[key]
    return action


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _response_output_text(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sanitize_json_schema(schema):
    if not isinstance(schema, dict):
        return schema
    STRIP = {"strict", "x-oai", "x-openai", "$schema"}
    clean = {}
    for k, v in schema.items():
        if k.startswith("x-") or k in STRIP:
            continue
        if isinstance(v, dict):
            clean[k] = _sanitize_json_schema(v)
        elif isinstance(v, list):
            clean[k] = [_sanitize_json_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            clean[k] = v
    return clean


def _normalize_tool_choice(tool_choice):
    if isinstance(tool_choice, str) and tool_choice in ("auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "")
        if tc_type == "function":
            fn = tool_choice.get("function", {})
            name = fn.get("name") or tool_choice.get("name", "")
            if name:
                return {"type": "function", "function": {"name": name}}
    return "auto"


def _map_optional(src: dict, dst: dict, key: str) -> None:
    if key in src and src[key] is not None:
        dst[key] = src[key]


# ═══════════════════════════════════════════════════════════════════
# 非流式响应转换: Chat Completions API → Responses API
# ═══════════════════════════════════════════════════════════════════

def translate_response(
    chat_resp: dict,
    adapter: BaseAdapter,
    model: str,
    namespace_tools: dict[str, dict[str, str]] | None = None,
    custom_tool_names: set[str] | list[str] | None = None,
    response_tool_types: dict[str, str] | None = None,
) -> dict:
    """将 Chat Completions 响应转换为 Responses API 格式"""
    chat_resp = adapter.postprocess_chat_response(chat_resp)

    choices = chat_resp.get("choices", [])
    usage = chat_resp.get("usage", {})
    output_items: list[dict] = []

    for choice in choices:
        msg = choice.get("message", {})
        reasoning_content = _extract_message_reasoning(msg)
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []
        if msg.get("function_call") and not tool_calls:
            fn = msg["function_call"]
            tool_calls = [{
                "id": msg.get("tool_call_id") or _uid("call"),
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                },
            }]

        # Reasoning must be preserved as a native Responses item and must not
        # leak into assistant answer text.
        if reasoning_content:
            output_items.append(_make_reasoning_output_item(reasoning_content))

        # 文本内容
        if content:
            output_items.append(make_message_output_item(content))

        # 工具调用
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            arguments = fn.get("arguments", "")
            call_id = tc.get("id", "")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            custom_input = _custom_tool_input_with_names(
                name,
                arguments,
                set(custom_tool_names or []),
            )
            if custom_input is not None:
                output_items.append(make_custom_tool_call_output_item(name, custom_input, call_id))
            elif (response_tool_types or {}).get(name) == "local_shell_call":
                output_items.append(_make_local_shell_call_item(arguments, call_id))
            elif (response_tool_types or {}).get(name) == "tool_search_call":
                output_items.append(_make_tool_search_call_item(arguments, call_id))
            else:
                restored_name, namespace = _restore_tool_identity(name, namespace_tools)
                output_items.append(
                    make_function_call_output_item(restored_name, arguments, call_id, namespace)
                )

    if not output_items:
        raise ValueError(_EMPTY_UPSTREAM_RESPONSE_MESSAGE)
    if not any(item.get("type") != "reasoning" for item in output_items):
        raise ValueError(_REASONING_ONLY_RESPONSE_MESSAGE)

    response = build_responses_response(output_items, model, usage)
    if any(choice.get("finish_reason") == "length" for choice in choices):
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


def _extract_message_reasoning(msg: dict) -> str:
    reasoning = msg.get("reasoning_content")
    if reasoning:
        return str(reasoning)
    reasoning = msg.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning
    if isinstance(reasoning, dict):
        for key in ("content", "text", "summary"):
            value = reasoning.get(key)
            if isinstance(value, str):
                return value
    return ""


def _make_local_shell_call_item(arguments: str, call_id: str) -> dict:
    return {
        "id": _uid("lsh"),
        "type": "local_shell_call",
        "call_id": call_id or _uid("call"),
        "status": "completed",
        "action": _arguments_to_local_shell_action(arguments),
    }


def _make_tool_search_call_item(arguments: str, call_id: str) -> dict:
    parsed = _json_object(arguments)
    return {
        "id": _uid("tsc"),
        "type": "tool_search_call",
        "call_id": call_id or _uid("call"),
        "status": "completed",
        "execution": "client",
        "arguments": parsed if parsed else arguments,
    }


def _make_reasoning_output_item(text: str, status: str = "completed") -> dict:
    return {
        "id": _uid("rs"),
        "type": "reasoning",
        "status": status,
        "summary": [{"type": "summary_text", "text": text}],
        "content": None,
        "encrypted_content": None,
    }


# ═══════════════════════════════════════════════════════════════════
# 流式转换: Chat Completions SSE → Responses API SSE
# ═══════════════════════════════════════════════════════════════════

class StreamTranslator:
    """有状态的流式转换器

    将 Chat Completions 的 SSE 流逐块转换为 Responses API 的 SSE 事件流。
    """

    def __init__(
        self,
        response_id: str | None = None,
        model: str = "",
        initial_output_items: list[dict] | None = None,
        created_sent: bool = False,
        completion_usage: dict | None = None,
        defer_completion_until_stream_end: bool = False,
        namespace_tools: dict[str, dict[str, str]] | None = None,
        custom_tool_names: set[str] | list[str] | None = None,
        response_tool_types: dict[str, str] | None = None,
    ):
        self.response_id = response_id or _uid("resp")
        self.model = model
        self._namespace_tools = namespace_tools or {}
        self._custom_tool_names = set(custom_tool_names or [])
        self._response_tool_types = response_tool_types or {}

        # 状态
        self._created_sent = created_sent
        self._done = False
        self._output_index = len(initial_output_items or []) - 1

        # 文本输出追踪
        self._text_item_index = -1
        self._text_item_id = ""
        self._text_content_index = -1
        self._text_buf: list[str] = []
        self._text_started = False

        # Reasoning is retained as a completed item so tool-call history can
        # restore the exact provider field on the next request.
        self._reasoning_item_index = -1
        self._reasoning_buf: list[str] = []
        self._reasoning_started = False

        # 工具调用缓冲 (按 index 分组)
        # {index: {"id": str, "name": str, "arguments": str, "item_index": int}}
        self._tc_buf: dict[int, dict] = {}

        # 最终输出列表
        self._output_items: list[dict] = list(initial_output_items or [])

        # 辅助
        self._accumulated_text = ""
        self._finish_reason = ""
        self._completion_usage = completion_usage
        self._defer_completion_until_stream_end = defer_completion_until_stream_end
        self._inline_think_mode = "detecting"
        self._inline_think_buffer = ""

    # ── 入口 ─────────────────────────────────────────────────────

    async def translate_stream(
        self,
        chat_stream: AsyncIterator[dict],
    ) -> AsyncIterator[str]:
        """将 Chat SSE stream 转换为 Responses SSE stream 的字符串行"""
        try:
            async for chunk in chat_stream:
                for event_line in self._process_chunk(chunk):
                    yield event_line
            for event_line in self._finish():
                yield event_line
        except Exception as exc:
            yield _sse_line({"type": "response.failed", "response": {
                "id": self.response_id, "object": "response", "model": self.model,
                "status": "failed", "output": [],
                "error": {"message": str(exc), "type": "stream_error"}}})

    def translate_chunk(self, chunk: dict) -> list[str]:
        """同步版本：处理单个 chunk"""
        return list(self._process_chunk(chunk))

    # ── 核心处理逻辑 ─────────────────────────────────────────────

    def _process_chunk(self, chunk: dict):
        """处理单个 Chat SSE chunk，生成 Responses SSE 事件行"""
        if self._done:
            return

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._completion_usage = usage

        if not self._created_sent:
            yield from self._emit_created()

        choices = chunk.get("choices", [])
        if not choices:
            return

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason") or ""
        if finish_reason:
            self._finish_reason = finish_reason

        reasoning_content = _extract_delta_reasoning(delta)
        if reasoning_content:
            yield from self._handle_reasoning_delta(reasoning_content)

        # 文本增量
        content = delta.get("content")
        if content:
            yield from self._handle_content_delta(str(content))

        # 工具调用增量
        tool_calls = delta.get("tool_calls", [])
        if tool_calls:
            yield from self._flush_inline_think_at_boundary()
        for tc in tool_calls:
            yield from self._handle_tool_call_delta(tc)

        # 完成
        if finish_reason:
            if not self._defer_completion_until_stream_end:
                yield from self._finish()

    @property
    def completion_usage(self) -> dict | None:
        return self._completion_usage

    def _finish(self) -> list[str]:
        """流结束时的收尾事件"""
        if self._done:
            return []
        events: list[str] = []

        events.extend(self._flush_inline_think_at_boundary())

        if self._reasoning_started:
            events.extend(self._emit_reasoning_done())

        # 结束文本项 (如果还在进行中)
        if self._text_started:
            events.extend(self._emit_text_done())

        # 结束工具调用项
        for idx in sorted(self._tc_buf.keys()):
            events.extend(self._emit_tool_call_done(idx))

        if not self._output_items:
            self._done = True
            events.append(
                _sse_line({
                    "type": "response.failed",
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "model": self.model,
                        "status": "failed",
                        "output": [],
                        "error": {
                            "message": _EMPTY_UPSTREAM_RESPONSE_MESSAGE,
                            "type": "empty_upstream_response",
                        },
                    },
                })
            )
            return events

        if not any(item.get("type") != "reasoning" for item in self._output_items):
            self._done = True
            events.append(
                _sse_line({
                    "type": "response.failed",
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "model": self.model,
                        "status": "failed",
                        "output": self._output_items,
                        "error": {
                            "message": _REASONING_ONLY_RESPONSE_MESSAGE,
                            "type": "reasoning_without_action",
                        },
                    },
                })
            )
            return events

        # response.completed
        completed_response = {
            "id": self.response_id,
            "object": "response",
            "model": self.model,
            "status": "completed",
            "output": self._output_items,
        }
        completed_response["usage"] = make_responses_usage(self._completion_usage)
        events.append(
            _sse_line({
                "type": "response.completed",
                "response": completed_response,
            })
        )
        self._done = True
        return events

    # ── 事件生成 ─────────────────────────────────────────────────

    def _emit_created(self):
        events = []
        events.append(
            _sse_line({
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "model": self.model,
                    "status": "in_progress",
                    "output": [],
                },
            })
        )
        self._created_sent = True
        return events

    @property
    def reasoning_content(self) -> str:
        return "".join(self._reasoning_buf)

    def _handle_content_delta(self, content: str) -> list[str]:
        events: list[str] = []
        remaining = content

        while remaining:
            if self._inline_think_mode == "reasoning":
                self._inline_think_buffer += remaining
                close_pos = self._inline_think_buffer.find(_THINK_CLOSE_TAG)
                if close_pos != -1:
                    reasoning = self._inline_think_buffer[:close_pos]
                    tail = self._inline_think_buffer[close_pos + len(_THINK_CLOSE_TAG):]
                    self._inline_think_buffer = ""
                    self._inline_think_mode = "text"
                    if reasoning:
                        events.extend(self._handle_reasoning_delta(reasoning))
                    if self._reasoning_started:
                        events.extend(self._emit_reasoning_done())
                    remaining = tail
                    continue

                keep = _suffix_prefix_len(self._inline_think_buffer, _THINK_CLOSE_TAG)
                emit_len = len(self._inline_think_buffer) - keep
                if emit_len > 0:
                    events.extend(self._handle_reasoning_delta(self._inline_think_buffer[:emit_len]))
                    self._inline_think_buffer = self._inline_think_buffer[emit_len:]
                break

            if self._inline_think_mode == "detecting":
                self._inline_think_buffer += remaining
                if _THINK_OPEN_TAG.startswith(self._inline_think_buffer):
                    break
                open_pos = self._inline_think_buffer.find(_THINK_OPEN_TAG)
                if open_pos != -1:
                    before = self._inline_think_buffer[:open_pos]
                    tail = self._inline_think_buffer[open_pos + len(_THINK_OPEN_TAG):]
                    self._inline_think_buffer = ""
                    if before:
                        self._inline_think_mode = "text"
                        events.extend(self._handle_text_delta(before))
                    self._inline_think_mode = "reasoning"
                    remaining = tail
                    continue
                visible = self._inline_think_buffer
                self._inline_think_buffer = ""
                self._inline_think_mode = "text"
                if visible:
                    events.extend(self._handle_text_delta(visible))
                break

            open_pos = remaining.find(_THINK_OPEN_TAG)
            if open_pos == -1:
                events.extend(self._handle_text_delta(remaining))
                break
            before = remaining[:open_pos]
            if before:
                events.extend(self._handle_text_delta(before))
            self._inline_think_mode = "reasoning"
            remaining = remaining[open_pos + len(_THINK_OPEN_TAG):]

        return events

    def _flush_inline_think_at_boundary(self) -> list[str]:
        events: list[str] = []
        buffered = self._inline_think_buffer
        self._inline_think_buffer = ""
        if not buffered:
            if self._inline_think_mode == "detecting":
                self._inline_think_mode = "text"
            return events

        if self._inline_think_mode == "reasoning":
            if buffered:
                events.extend(self._handle_reasoning_delta(buffered))
            self._inline_think_mode = "text"
            return events

        self._inline_think_mode = "text"
        events.extend(self._handle_text_delta(buffered))
        return events

    def _handle_reasoning_delta(self, content: str) -> list[str]:
        events = []
        if not self._reasoning_started:
            self._output_index += 1
            self._reasoning_item_index = self._output_index
            self._reasoning_buf = []
            self._reasoning_started = True
            item = _make_reasoning_output_item("", status="in_progress")
            self._output_items.append(item)
            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": self._reasoning_item_index,
                    "item": item,
                })
            )
            events.append(
                _sse_line({
                    "type": "response.reasoning_summary_part.added",
                    "output_index": self._reasoning_item_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                })
            )
        self._reasoning_buf.append(content)
        events.append(
            _sse_line({
                "type": "response.reasoning_summary_text.delta",
                "output_index": self._reasoning_item_index,
                "summary_index": 0,
                "delta": content,
            })
        )
        return events

    def _emit_reasoning_done(self) -> list[str]:
        if not self._reasoning_started:
            return []
        item = self._output_items[self._reasoning_item_index]
        item["status"] = "completed"
        item["summary"][0]["text"] = "".join(self._reasoning_buf)
        self._reasoning_started = False
        return [
            _sse_line({
                "type": "response.output_item.done",
                "output_index": self._reasoning_item_index,
                "item": item,
            })
        ]

    def _handle_text_delta(self, content: str) -> list[str]:
        events = []
        if not self._text_started:
            # 开始新的文本输出项
            self._output_index += 1
            self._text_item_index = self._output_index
            self._text_item_id = _uid("msg")
            self._text_content_index = 0
            self._text_buf = []
            self._text_started = True

            # 生成 output_item 和 content_part 的占位记录
            item = {
                "id": self._text_item_id,
                "object": "realtime.item",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            self._output_items.append(item)

            # event: response.output_item.added
            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": self._text_item_index,
                    "item": item,
                })
            )

            # event: response.content_part.added
            part = {"type": "output_text", "text": "", "annotations": []}
            item["content"].append(part)
            events.append(
                _sse_line({
                    "type": "response.content_part.added",
                    "output_index": self._text_item_index,
                    "content_index": self._text_content_index,
                    "part": part,
                })
            )

        self._text_buf.append(content)
        self._accumulated_text += content

        # event: response.output_text.delta
        events.append(
            _sse_line({
                "type": "response.output_text.delta",
                "output_index": self._text_item_index,
                "content_index": self._text_content_index,
                "delta": content,
            })
        )
        return events

    def _emit_text_done(self) -> list[str]:
        if not self._text_started:
            return []
        events = []

        # 更新 item 状态
        if self._text_item_index < len(self._output_items):
            item = self._output_items[self._text_item_index]
            item["status"] = "completed"
            if item["content"]:
                item["content"][0]["text"] = self._accumulated_text

        # Codex waits for the content part to be explicitly finalized before
        # accepting the containing output item as complete.
        events.append(
            _sse_line({
                "type": "response.content_part.done",
                "output_index": self._text_item_index,
                "content_index": self._text_content_index,
                "part": (
                    self._output_items[self._text_item_index]["content"][0]
                    if self._text_item_index < len(self._output_items)
                    else {}
                ),
            })
        )

        # event: response.output_item.done
        events.append(
            _sse_line({
                "type": "response.output_item.done",
                "output_index": self._text_item_index,
                "item": self._output_items[self._text_item_index] if self._text_item_index < len(self._output_items) else {},
            })
        )
        self._text_started = False
        return events

    def _handle_tool_call_delta(self, tc: dict) -> list[str]:
        events = []
        tc_index = tc.get("index", 0)
        fn = tc.get("function", {})
        fn_name = fn.get("name", "")
        fn_args = fn.get("arguments", "")
        tc_id = tc.get("id", "")

        if tc_index not in self._tc_buf:
            # 新的工具调用
            self._output_index += 1
            item_id = _uid("fc")
            call_id = tc_id or _uid("call")

            self._tc_buf[tc_index] = {
                "id": item_id,
                "call_id": call_id,
                "name": "",
                "arguments": "",
                "item_index": self._output_index,
                "name_done": False,
            }

            # 占位 item
            item = {
                "id": item_id,
                "object": "realtime.item",
                "type": "function_call",
                "call_id": call_id,
                "name": "",
                "arguments": "",
                "status": "in_progress",
            }
            self._output_items.append(item)

        buf = self._tc_buf[tc_index]

        # 名字事件（首次出现时）
        if fn_name and not buf["name_done"]:
            buf["name"] = fn_name
            buf["name_done"] = True
            if buf["item_index"] < len(self._output_items):
                restored_name, namespace = _restore_tool_identity(fn_name, self._namespace_tools)
                self._output_items[buf["item_index"]]["name"] = restored_name
                if namespace:
                    self._output_items[buf["item_index"]]["namespace"] = namespace
                if self._is_custom_tool_name(fn_name):
                    self._output_items[buf["item_index"]]["type"] = "custom_tool_call"
                    self._output_items[buf["item_index"]]["id"] = _uid("ctc")
                    buf["id"] = self._output_items[buf["item_index"]]["id"]
                    self._output_items[buf["item_index"]].pop("arguments", None)
                    self._output_items[buf["item_index"]]["input"] = ""
                elif self._response_tool_types.get(fn_name) == "local_shell_call":
                    self._output_items[buf["item_index"]]["type"] = "local_shell_call"
                    self._output_items[buf["item_index"]]["action"] = {"type": "exec", "command": []}
                    self._output_items[buf["item_index"]].pop("name", None)
                    self._output_items[buf["item_index"]].pop("arguments", None)
                elif self._response_tool_types.get(fn_name) == "tool_search_call":
                    self._output_items[buf["item_index"]]["type"] = "tool_search_call"
                    self._output_items[buf["item_index"]]["execution"] = "client"
                    self._output_items[buf["item_index"]]["arguments"] = {}
                    self._output_items[buf["item_index"]].pop("name", None)

            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": buf["item_index"],
                    "item": self._output_items[buf["item_index"]],
                })
            )

        # 参数增量
        if fn_args:
            buf["arguments"] += fn_args
            if buf["item_index"] < len(self._output_items):
                if not self._is_custom_tool_name(buf["name"]):
                    self._output_items[buf["item_index"]]["arguments"] = buf["arguments"]

            if not self._is_custom_tool_name(buf["name"]):
                events.append(
                    _sse_line({
                        "type": "response.function_call_arguments.delta",
                        "output_index": buf["item_index"],
                        "call_id": buf["call_id"],
                        "delta": fn_args,
                    })
                )

        return events

    def _emit_tool_call_done(self, tc_index: int) -> list[str]:
        buf = self._tc_buf[tc_index]
        item_idx = buf["item_index"]
        events: list[str] = []
        if item_idx < len(self._output_items):
            item = self._output_items[item_idx]
            item["status"] = "completed"
            custom_input = _custom_tool_input_with_names(
                buf["name"],
                buf["arguments"],
                self._custom_tool_names,
            )
            if custom_input is not None:
                item["type"] = "custom_tool_call"
                item["input"] = custom_input
                item.pop("arguments", None)
                events.append(
                    _sse_line({
                        "type": "response.custom_tool_call_input.delta",
                        "output_index": item_idx,
                        "call_id": buf["call_id"],
                        "delta": custom_input,
                    })
                )
            elif self._response_tool_types.get(buf["name"]) == "local_shell_call":
                item.clear()
                item.update(_make_local_shell_call_item(buf["arguments"], buf["call_id"]))
            elif self._response_tool_types.get(buf["name"]) == "tool_search_call":
                item.clear()
                item.update(_make_tool_search_call_item(buf["arguments"], buf["call_id"]))

        # Only standard function_call items use the function-arguments event
        # family.  Custom, local-shell and tool-search calls keep their native
        # completion sequence unchanged.
        item = self._output_items[item_idx] if item_idx < len(self._output_items) else {}
        if item.get("type") == "function_call":
            events.append(
                _sse_line({
                    "type": "response.function_call_arguments.done",
                    "output_index": item_idx,
                    "call_id": buf["call_id"],
                    "arguments": buf["arguments"],
                })
            )

        events.append(
            _sse_line({
                "type": "response.output_item.done",
                "output_index": item_idx,
                "item": item,
            })
        )
        return events

    def _is_custom_tool_name(self, name: str) -> bool:
        return name == "apply_patch" or name in self._custom_tool_names


def _extract_delta_reasoning(delta: dict) -> str:
    reasoning = delta.get("reasoning_content")
    if reasoning:
        return str(reasoning)
    reasoning = delta.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning
    if isinstance(reasoning, dict):
        for key in ("content", "text", "summary"):
            value = reasoning.get(key)
            if isinstance(value, str):
                return value
    return ""


def _suffix_prefix_len(value: str, prefix: str) -> int:
    max_len = min(len(value), len(prefix) - 1)
    for size in range(max_len, 0, -1):
        if prefix.startswith(value[-size:]):
            return size
    return 0


def _split_complete_think_block(content: str) -> tuple[str, str]:
    start = content.find(_THINK_OPEN_TAG)
    end = content.find(_THINK_CLOSE_TAG)
    if start == -1 or end == -1 or end < start:
        return "", content
    reasoning = content[start + len(_THINK_OPEN_TAG):end]
    visible = content[:start] + content[end + len(_THINK_CLOSE_TAG):]
    return reasoning, visible


def _sse_line(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
