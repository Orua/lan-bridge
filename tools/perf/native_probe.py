"""Bounded, content-free native latency probe. Credentials never leave this host.

Run from the repository: python tools/perf/native_probe.py --output agent/output/...
Use --mode paired before changes; --mode bridge for the candidate (30 total calls).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from code_cn_bridge.access_control import get_access_key_store
from code_cn_bridge.config import Config
from code_cn_bridge.native_proxy import (
    _native_base_url, _native_client, _prepare_native_responses_payload,
    native_request_headers,
)
from code_cn_bridge.sse_observer import SSEJsonObserver

BODY = {
    "model": "gpt-6.1-sol", "stream": True, "store": False,
    "instructions": "Answer the user's question briefly and exactly.",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly: BRIDGE_PERF_OK"}]}],
    "reasoning": {"effort": "medium"},
}


async def measure(client, url, headers, payload, route, index):
    started = time.perf_counter()
    row = {"index": index, "route": route, "model": BODY["model"], "effort": "medium",
           "request_bytes": len(payload), "first_data_ms": None, "first_text_ms": None,
           "first_tool_ms": None, "connect_ms": None, "connections": 0,
           "status": None, "completed": False, "failed": False, "output_chars": 0,
           "usage": None, "error_type": None}
    connect_started = None

    async def trace(event, info):
        nonlocal connect_started
        if event == "connection.connect_tcp.started":
            row["connections"] += 1
            connect_started = time.perf_counter()
        if event == "connection.connect_tcp.complete" and connect_started is not None:
            row["connect_ms"] = (time.perf_counter() - connect_started) * 1000

    def observe(event):
        elapsed = (time.perf_counter() - started) * 1000
        kind = event.get("type")
        if kind == "response.output_text.delta" and event.get("delta"):
            if row["first_text_ms"] is None:
                row["first_text_ms"] = elapsed
            row["output_chars"] += len(event["delta"])
        if kind == "response.output_item.done" and event.get("item", {}).get("type") == "function_call":
            row["first_tool_ms"] = row["first_tool_ms"] or elapsed
        if kind == "response.completed":
            row["completed"] = True
            row["usage"] = event.get("response", {}).get("usage")
        if kind in {"error", "response.failed", "response.incomplete"}:
            row["failed"] = True

    try:
        req = client.build_request("POST", url, headers=headers, content=payload)
        req.extensions["trace"] = trace
        response = await client.send(req, stream=True)
        try:
            row["status"] = response.status_code
            observer = SSEJsonObserver()
            async for chunk in response.aiter_bytes():
                if chunk and row["first_data_ms"] is None:
                    row["first_data_ms"] = (time.perf_counter() - started) * 1000
                if response.status_code == 200:
                    for event in observer.feed(chunk):
                        observe(event)
            if response.status_code == 200:
                for event in observer.finish():
                    observe(event)
        finally:
            await response.aclose()
    except Exception as exc:
        row["error_type"] = type(exc).__name__
    row["total_ms"] = (time.perf_counter() - started) * 1000
    return row


async def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    config = Config()
    config.load(Path.home() / ".lan-bridge.yaml")
    server = config.data.get("server", {})
    metadata = {"python": platform.python_version(), "httpx": httpx.__version__,
                "upstream_host": httpx.URL(_native_base_url(config)).host,
                "explicit_outbound_proxy": bool(server.get("codex_official_proxy_url")),
                "config_sha256": hashlib.sha256(config._config_path.read_bytes()).hexdigest(),
                "mode": args.mode, "pairs": args.samples,
                "unavailable": ["LAN client", "inbound tunnel", "upstream internal queue/compute"]}
    (output / "environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    store = get_access_key_store(config)
    key, record = store.create("Bounded performance probe", [BODY["model"]])
    deadline = time.monotonic() + args.deadline
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=60) as bridge:
            for pair in range(args.samples):
                routes = (["A", "B"] if pair % 2 == 0 else ["B", "A"]) if args.mode == "paired" else ["B"]
                for route in routes:
                    if time.monotonic() >= deadline:
                        return
                    payload = json.dumps(_prepare_native_responses_payload(BODY, BODY["model"]) if route == "A" else BODY,
                                         ensure_ascii=False, separators=(",", ":")).encode()
                    async with asyncio.timeout(min(65, deadline - time.monotonic())):
                        if route == "A":
                            client = _native_client(config, httpx.Timeout(60, connect=15))
                            try:
                                row = await measure(client, _native_base_url(config) + "/responses", native_request_headers({}, config), payload, route, pair)
                            finally:
                                await client.aclose()
                        else:
                            row = await measure(bridge, "http://127.0.0.1:8765/v1/responses", {"authorization": "Bearer " + key}, payload, route, pair)
                    with (output / "requests.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row) + "\n")
                    print(json.dumps(row), flush=True)
                    if row["status"] == 429:
                        return
    finally:
        store.revoke(record["id"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["paired", "bridge"], default="paired")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--deadline", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10 or not 1 <= args.deadline <= 600:
        parser.error("maximum 10 samples per route, 600 seconds")
    asyncio.run(main(args))
