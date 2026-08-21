"""测试截断嵌套 HTTP 的 tools 保留 + 流式 + 工具调用"""
import httpx
import json

# 构造完整请求
full = json.dumps({
    "model": "gpt-5.5",
    "messages": [
        {"role": "system", "content": "你是助手，有工具可用时请调用工具"},
        {"role": "user", "content": "查一下今天的制令单"},
    ],
    "agent": "cli",
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "mcp__goldenfu-base__mo_list",
                "description": "查询制令单列表",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "Date1": {"type": "string"},
                        "Date2": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "mcp__goldenfu-base__po_list",
                "description": "查询采购单",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "skill_AskUserQuestion",
                "description": "x" * 3000,
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ],
    "stream": True,
    "max_completion_tokens": 8192,
}, ensure_ascii=False)

# 截断在最后一个 tool 的 description 中间
trunc_pos = full.find("x" * 100)
truncated = full[:trunc_pos]
print(f"full size: {len(full)}, truncated size: {len(truncated)}")
print(f"stream in truncated: {'stream' in truncated}")

# 构造嵌套 HTTP
inner_bytes = truncated.encode("utf-8")
nested = (
    "POST http://192.0.2.10:8765/v1/chat/completions HTTP/1.1\r\n"
    "Content-Type: application/json\r\n"
    f"Content-Length: {len(full)}\r\n"
    "\r\n"
).encode() + inner_bytes

print()
print("=== 测试: 截断嵌套 HTTP (tools 部分截断) ===")
with httpx.Client(trust_env=False, timeout=60) as c:
    with c.stream(
        "POST",
        "http://127.0.0.1:8765/v1/chat/completions",
        content=nested,
        headers={"Content-Type": "application/json"},
    ) as r:
        print(f"  status: {r.status_code}")
        print(f"  content-type: {r.headers.get('content-type', '')}")

        chunk_count = 0
        has_tool_call = False
        tool_names = []
        all_text = ""
        for line in r.iter_lines():
            if line.startswith("data: "):
                chunk_count += 1
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        tc = delta.get("tool_calls")
                        if tc:
                            has_tool_call = True
                            for call in tc:
                                func = call.get("function", {})
                                if func.get("name"):
                                    tool_names.append(func["name"])
                        content = delta.get("content", "")
                        if content:
                            all_text += content
                except Exception:
                    pass

        print(f"  SSE chunks: {chunk_count}")
        print(f"  has tool_calls in response: {has_tool_call}")
        if tool_names:
            print(f"  tool names: {tool_names}")
        if all_text:
            print(f"  text content: {all_text[:200]}")
        if chunk_count > 1:
            print(f"  ✅ 流式正常")
        if has_tool_call:
            print(f"  ✅ 工具调用正常（API 格式，不是文本格式）")
