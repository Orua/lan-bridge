#!/usr/bin/env python3
"""Call LAN BRIDGE with Codex sub-agent shaped parameters.

This is a thin client for the bridge's OpenAI-compatible /v1/responses
endpoint. It does not implement native Codex sub-agent scheduling; it packages
the delegated task, role hints, and reasoning effort so the upstream model can
act as a bridge-backed sub-agent.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8765/v1"
CLIENT_NAME = "tools/codex_subagent_bridge.py"

BASE_GUIDE = """\
You are acting as a Codex-style sub-agent reached through LAN BRIDGE.
Solve only the delegated task and return the result directly to the main agent.
Do not reveal hidden reasoning. Do not claim to run commands, inspect files,
open browsers, or use tools unless those tool results are explicitly included
in the input. If the task needs unavailable local action, state the limitation
and provide the best actionable answer. Respond in the user's language unless
the task asks otherwise."""

AGENT_TYPE_GUIDES = {
    "default": "Use a direct, practical Codex assistant style.",
    "explorer": (
        "Explorer role: answer the specific question with concise findings, "
        "evidence, assumptions, and risks. Do not propose broad rewrites."
    ),
    "worker": (
        "Worker role: produce implementation-ready output. Be explicit about "
        "changed files, commands, or patches when the input provides enough "
        "context; otherwise provide a concrete plan."
    ),
}

REASONING_GUIDES = {
    "low": "Reasoning depth: low. Keep the answer short and avoid broad analysis.",
    "medium": "Reasoning depth: medium. Balance speed with useful checks.",
    "high": (
        "Reasoning depth: high. Analyze carefully, check edge cases, and make "
        "tradeoffs explicit while keeping the final answer focused."
    ),
}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _json_arg(value: str, label: str) -> Any:
    text = value
    if value.startswith("@"):
        text = Path(value[1:]).read_text(encoding="utf-8")
    else:
        path = Path(value)
        if path.is_file():
            text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be JSON or @path-to-json: {exc}") from exc


def _data_uri_from_file(path_value: str) -> str:
    path = Path(path_value)
    data = path.read_bytes()
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _item_to_content_part(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "text")
    if item_type == "text":
        text = item.get("text", "")
        return {"type": "input_text", "text": str(text)}
    if item_type == "image":
        image_url = item.get("image_url") or item.get("url")
        if not image_url:
            raise ValueError("image item requires image_url or url")
        return {"type": "input_image", "image_url": str(image_url)}
    if item_type == "local_image":
        path = item.get("path")
        if not path:
            raise ValueError("local_image item requires path")
        return {"type": "input_image", "image_url": _data_uri_from_file(str(path))}
    if item_type in {"skill", "mention"}:
        name = item.get("name") or item.get("path") or item_type
        path = item.get("path", "")
        text = f"[{item_type}: {name}]"
        if path:
            text = f"{text} {path}"
        return {"type": "input_text", "text": text}
    raise ValueError(f"unsupported item type: {item_type}")


def _input_from_items(message: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if message:
        content.append({"type": "input_text", "text": message})
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("items must be a JSON array of objects")
        part = _item_to_content_part(item)
        if part is not None:
            content.append(part)
    if not content:
        raise ValueError("provide a message, stdin text, --items, or --input")
    return [{"type": "message", "role": "user", "content": content}]


def build_instructions(agent_type: str, reasoning_effort: str, extra: str) -> str:
    parts = [
        BASE_GUIDE,
        AGENT_TYPE_GUIDES[agent_type],
        REASONING_GUIDES[reasoning_effort],
    ]
    if extra.strip():
        parts.append("Additional instructions from the main agent:\n" + extra.strip())
    return "\n\n".join(parts)


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    message = args.message_flag or args.prompt or args.message or ""
    if not message and not sys.stdin.isatty():
        message = sys.stdin.read().strip()

    if args.input:
        input_items = _json_arg(args.input, "--input")
        if not isinstance(input_items, list):
            raise ValueError("--input must be a Responses API input array")
    else:
        native_items = _json_arg(args.items, "--items") if args.items else []
        if not isinstance(native_items, list):
            raise ValueError("--items must be a native sub-agent items array")
        input_items = _input_from_items(message.strip(), native_items)

    payload: dict[str, Any] = {
        "model": args.model,
        "instructions": build_instructions(
            args.agent_type,
            args.reasoning_effort,
            args.instructions or "",
        ),
        "input": input_items,
        "reasoning": {"effort": args.reasoning_effort},
        "stream": False,
        "metadata": {
            "client": CLIENT_NAME,
            "agent_type": args.agent_type,
            "fork_context": bool(args.fork_context),
        },
    }

    if args.service_tier:
        payload["service_tier"] = args.service_tier
        payload["metadata"]["service_tier"] = args.service_tier
    if args.previous_response_id:
        payload["previous_response_id"] = args.previous_response_id
    if args.max_output_tokens is not None:
        payload["max_output_tokens"] = args.max_output_tokens
    if args.temperature is not None:
        payload["temperature"] = args.temperature
    if args.top_p is not None:
        payload["top_p"] = args.top_p
    if args.tools_json:
        tools = _json_arg(args.tools_json, "--tools-json")
        if not isinstance(tools, list):
            raise ValueError("--tools-json must be a JSON array")
        payload["tools"] = tools
    if args.tool_choice:
        payload["tool_choice"] = args.tool_choice
    if args.metadata_json:
        metadata = _json_arg(args.metadata_json, "--metadata-json")
        if not isinstance(metadata, dict):
            raise ValueError("--metadata-json must be a JSON object")
        payload["metadata"].update(metadata)

    return payload


def responses_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/responses"):
        return url
    if url.endswith("/v1"):
        return f"{url}/responses"
    return f"{url}/v1/responses"


def post_json(url: str, payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"bridge returned HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach bridge at {url}: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bridge returned non-JSON response: {body[:500]}") from exc


def extract_text(response: dict[str, Any], include_reasoning: bool = False) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    parts: list[str] = []
    for item in response.get("output", []) or []:
        item_type = item.get("type")
        if item_type == "reasoning" and not include_reasoning:
            continue
        content = item.get("content") or item.get("summary")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(part for part in parts if part)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Invoke LAN BRIDGE as a Codex-style sub-agent client.",
    )
    parser.add_argument("message", nargs="?", help="Delegated task message.")
    parser.add_argument("--message", dest="message_flag", help="Delegated task message.")
    parser.add_argument("--prompt", help="Alias for --message.")
    parser.add_argument(
        "--items",
        help=(
            "Native sub-agent style items JSON or @file. Supported item types: "
            "text, image, local_image, skill, mention."
        ),
    )
    parser.add_argument(
        "--input",
        help="Raw Responses API input array JSON or @file. Overrides --message and --items.",
    )
    parser.add_argument(
        "--agent-type",
        default="default",
        choices=sorted(AGENT_TYPE_GUIDES),
        help="Codex sub-agent role hint.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODEX_SUBAGENT_MODEL")
        or os.environ.get("LAN_BRIDGE_MODEL")
        or "gpt-5.5",
        help="Bridge model alias to request.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("CODEX_SUBAGENT_REASONING_EFFORT", "medium"),
        choices=sorted(REASONING_GUIDES),
        help="Main-agent selected thinking intensity.",
    )
    parser.add_argument("--service-tier", help="Native sub-agent style service tier hint.")
    parser.add_argument("--fork-context", action="store_true", help="Record native fork_context intent in metadata.")
    parser.add_argument("--instructions", help="Extra system instructions from the main agent.")
    parser.add_argument("--previous-response-id", help="Forward previous_response_id when needed.")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--tools-json", help="Responses API tools array JSON or @file.")
    parser.add_argument("--tool-choice", help="Responses API tool_choice value.")
    parser.add_argument("--metadata-json", help="Extra metadata JSON object or @file.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LAN_BRIDGE_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL,
        help="Bridge base URL, e.g. http://127.0.0.1:8765/v1.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LAN_BRIDGE_API_KEY") or os.environ.get("OPENAI_API_KEY") or "",
        help="Bearer token for bridge API-key filtering, if enabled.",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--json", action="store_true", help="Print the full Responses JSON.")
    parser.add_argument("--include-reasoning", action="store_true", help="Include reasoning summary text in text output.")
    parser.add_argument("--dry-run", action="store_true", help="Print the request payload without calling the bridge.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    try:
        payload = build_payload(args)
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        response = post_json(responses_url(args.base_url), payload, args.api_key, args.timeout)
        if args.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0
        text = extract_text(response, include_reasoning=args.include_reasoning).strip()
        if text:
            print(text)
        else:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"codex-subagent-bridge error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
