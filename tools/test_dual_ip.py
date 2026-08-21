"""对比测试：同时往 127.0.0.1 和 TEST_LAN_BRIDGE_HOST 发相同的 chat 请求，
显式绕过系统代理（proxies={}），对比两个 IP 的回复是否一致。

用户认为问题跟 IP 无关，是请求类型的问题。这个测试验证：
1. 简单请求 → 两个 IP 都应该成功
2. 大请求（模拟 context_summary/制令单）→ 两个 IP 结果应该相同
"""
import json
import os
import time
import sys
import httpx

REMOTE_IP = os.environ.get("TEST_LAN_BRIDGE_HOST", "192.0.2.10")
IPS = ["127.0.0.1", REMOTE_IP]
PORT = 8765
# trust_env=False 让 httpx 忽略系统 HTTP_PROXY/HTTPS_PROXY 环境变量
def send_chat(ip, body, timeout=30):
    url = f"http://{ip}:{PORT}/v1/chat/completions"
    t0 = time.time()
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.post(url, json=body, headers={"Content-Type": "application/json"})
        elapsed = round((time.time() - t0) * 1000, 1)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}
        return {
            "status": resp.status_code,
            "elapsed_ms": elapsed,
            "body": data,
        }
    except Exception as e:
        elapsed = round((time.time() - t0) * 1000, 1)
        return {
            "status": "EXCEPTION",
            "elapsed_ms": elapsed,
            "error": f"{type(e).__name__}: {e}",
        }

def test_simple():
    """测试1：简单文本请求"""
    print("\n" + "=" * 70)
    print("测试1：简单文本请求 '你好'")
    print("=" * 70)
    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "你好，简短回复"}],
        "stream": False,
        "max_tokens": 100,
    }
    results = {}
    for ip in IPS:
        print(f"\n  → 发送到 {ip}:{PORT} ...")
        r = send_chat(ip, body)
        results[ip] = r
        content = ""
        if isinstance(r.get("body"), dict):
            choices = r["body"].get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")[:200]
        print(f"    status={r['status']}  elapsed={r['elapsed_ms']}ms")
        if content:
            print(f"    回复: {content}")
        elif r.get("error"):
            print(f"    错误: {r['error']}")
        elif isinstance(r.get("body"), dict):
            print(f"    body: {json.dumps(r['body'], ensure_ascii=False)[:300]}")
    return results

def test_large():
    """测试2：大请求（模拟 context_summary，~150KB）"""
    print("\n" + "=" * 70)
    print("测试2：大请求（模拟 context_summary/制令单，~150KB）")
    print("=" * 70)
    # 构造一个大的系统 prompt，模拟 WB 的 context_summary
    big_system = "This is a simulated large context summary.\n" * 3000  # ~120KB
    body = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": big_system},
            {"role": "user", "content": "请简短总结上面的内容"},
        ],
        "stream": False,
        "max_tokens": 100,
    }
    body_size = len(json.dumps(body, ensure_ascii=False))
    print(f"  请求体大小: {body_size} bytes ({body_size/1024:.1f} KB)")

    results = {}
    for ip in IPS:
        print(f"\n  → 发送到 {ip}:{PORT} ...")
        r = send_chat(ip, body, timeout=60)
        results[ip] = r
        print(f"    status={r['status']}  elapsed={r['elapsed_ms']}ms")
        if isinstance(r.get("body"), dict):
            err = r["body"].get("error")
            if err:
                print(f"    error: {json.dumps(err, ensure_ascii=False)[:400]}")
            else:
                choices = r["body"].get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")[:200]
                    print(f"    回复: {content}")
                else:
                    print(f"    body: {json.dumps(r['body'], ensure_ascii=False)[:300]}")
        elif r.get("error"):
            print(f"    错误: {r['error']}")
    return results

def test_medium():
    """测试3：中等大小请求（~20KB）"""
    print("\n" + "=" * 70)
    print("测试3：中等请求（~20KB）")
    print("=" * 70)
    medium_system = "这是中等大小的系统提示。包含一些上下文信息。\n" * 400  # ~20KB
    body = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": medium_system},
            {"role": "user", "content": "用一句话回复"},
        ],
        "stream": False,
        "max_tokens": 100,
    }
    body_size = len(json.dumps(body, ensure_ascii=False))
    print(f"  请求体大小: {body_size} bytes ({body_size/1024:.1f} KB)")

    results = {}
    for ip in IPS:
        print(f"\n  → 发送到 {ip}:{PORT} ...")
        r = send_chat(ip, body, timeout=60)
        results[ip] = r
        print(f"    status={r['status']}  elapsed={r['elapsed_ms']}ms")
        if isinstance(r.get("body"), dict):
            err = r["body"].get("error")
            if err:
                print(f"    error: {json.dumps(err, ensure_ascii=False)[:400]}")
            else:
                choices = r["body"].get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")[:200]
                    print(f"    回复: {content}")
                else:
                    print(f"    body: {json.dumps(r['body'], ensure_ascii=False)[:300]}")
        elif r.get("error"):
            print(f"    错误: {r['error']}")
    return results

def test_nested_http_simulation():
    """测试4：模拟嵌套 HTTP（直接发 forward-proxy 格式的 body）"""
    print("\n" + "=" * 70)
    print("测试4：模拟嵌套 HTTP 请求（forward-proxy 格式）")
    print("=" * 70)
    # 构造嵌套 HTTP 格式的 body
    inner_body = json.dumps({
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "测试嵌套"}],
        "stream": False,
        "max_tokens": 50,
    }, ensure_ascii=False)
    nested_body = (
        f"POST http://{REMOTE_IP}:{PORT}/v1/chat/completions HTTP/1.1\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(inner_body)}\r\n"
        f"\r\n"
        f"{inner_body}"
    ).encode("utf-8")

    results = {}
    for ip in IPS:
        url = f"http://{ip}:{PORT}/v1/chat/completions"
        print(f"\n  → 发送嵌套HTTP到 {ip}:{PORT} ...")
        t0 = time.time()
        try:
            with httpx.Client(trust_env=False, timeout=30) as client:
                resp = client.post(url, content=nested_body, headers={"Content-Type": "application/json"})
            elapsed = round((time.time() - t0) * 1000, 1)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:500]}
            results[ip] = {"status": resp.status_code, "elapsed_ms": elapsed, "body": data}
            print(f"    status={resp.status_code}  elapsed={elapsed}ms")
            if isinstance(data, dict):
                err = data.get("error")
                if err:
                    print(f"    error: {json.dumps(err, ensure_ascii=False)[:400]}")
                else:
                    print(f"    body: {json.dumps(data, ensure_ascii=False)[:300]}")
            else:
                print(f"    raw: {str(data)[:300]}")
        except Exception as e:
            elapsed = round((time.time() - t0) * 1000, 1)
            results[ip] = {"status": "EXCEPTION", "elapsed_ms": elapsed, "error": str(e)}
            print(f"    错误: {type(e).__name__}: {e}")
    return results

def compare_results(test_name, results):
    """对比两个 IP 的结果"""
    r1 = results.get("127.0.0.1", {})
    r2 = results.get(REMOTE_IP, {})
    s1 = r1.get("status")
    s2 = r2.get("status")
    match = "✅ 一致" if s1 == s2 else "❌ 不一致"
    print(f"\n  📊 对比: 127.0.0.1={s1} vs {REMOTE_IP}={s2} → {match}")

if __name__ == "__main__":
    print("╔" + "═" * 68 + "╗")
    print(f"║  LAN BRIDGE 对比测试：127.0.0.1 vs {REMOTE_IP}（显式绕过代理）")
    print("╚" + "═" * 68 + "╝")

    r1 = test_simple()
    compare_results("简单请求", r1)

    r2 = test_medium()
    compare_results("中等请求", r2)

    r3 = test_large()
    compare_results("大请求", r3)

    r4 = test_nested_http_simulation()
    compare_results("嵌套HTTP", r4)

    print("\n" + "=" * 70)
    print("测试完成。如果两个 IP 的结果一致，说明问题跟 IP 无关。")
    print("=" * 70)
