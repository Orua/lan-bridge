"""管理 API —— 供桌面 UI 调用的配置管理端点"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from starlette.requests import HTTPConnection

from .config import get_config
from .config import get_bridge_root_dir
from .config import get_model_catalog_path
from .config import get_model_catalog_target_path
from .adapters import get_registry
from .stats import get_stats, RequestLog
from .client import UpstreamClient
from .http_utils import make_async_client, make_provider_async_client
from .native_proxy import custom_model_context_settings, merge_model_catalog
from .codex_auth import resolve_auth_file
from .access_control import (
    BridgeAccessError,
    access_control_enabled,
    get_access_key_store,
    public_access_key_record,
)
from .provider_proxy import model_uses_responses

logger = logging.getLogger("lan-bridge")


@contextmanager
def _edit_config(cfg):
    """Use Config's atomic transaction, with a compatible test-double fallback."""
    editor = getattr(cfg, "edit", None)
    if callable(editor):
        with editor() as candidate:
            yield candidate
        return

    original = copy.deepcopy(cfg._data)
    try:
        yield cfg._data
        cfg.save()
    except BaseException:
        cfg._data.clear()
        cfg._data.update(original)
        raise

def _require_local_admin(connection: HTTPConnection) -> None:
    """Keep shutdown and configuration mutation endpoints on this machine."""
    host = connection.client.host if connection.client else ""
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise HTTPException(status_code=403, detail="Bridge management API is local-only")


router = APIRouter(prefix="/admin/api", dependencies=[Depends(_require_local_admin)])

_CODEX_BRIDGE_SETTINGS_FILENAME = "codex-bridge-settings.yaml"
_DEFAULT_CODEX_BRIDGE_SETTINGS = {
    "tool_output_token_limit": 12_000,
}
_LEGACY_CODEX_BRIDGE_MODEL_SETTINGS = {
    "model_context_window",
    "model_auto_compact_token_limit",
}


def _codex_cli_candidates() -> list[Path]:
    candidates: list[Path] = []
    located = shutil.which("codex")
    if located:
        candidates.append(Path(located))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    codex_bin = local_app_data / "OpenAI" / "Codex" / "bin"
    if codex_bin.is_dir():
        candidates.extend(sorted(codex_bin.glob("*/codex.exe"), reverse=True))
    return list(dict.fromkeys(path for path in candidates if path.is_file()))


def _load_codex_models_cache() -> dict[str, Any] | None:
    """Load the account-specific catalog last refreshed by official Codex."""
    cache_path = Path.home() / ".codex" / "models_cache.json"
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, list) or not any(
        isinstance(model, dict) and model.get("slug") for model in models
    ):
        return None
    return payload


def _load_codex_catalog() -> dict[str, Any]:
    cached = _load_codex_models_cache()
    if cached is not None:
        return cached

    errors = []
    for executable in _codex_cli_candidates():
        try:
            completed = subprocess.run(
                [str(executable), "debug", "models", "--bundled"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                errors.append(f"{executable}: exit {completed.returncode}")
                continue
            payload = json.loads(completed.stdout)
            if isinstance(payload, dict) and isinstance(payload.get("models"), list) and payload["models"]:
                return payload
            errors.append(f"{executable}: empty model catalog")
        except Exception as exc:
            errors.append(f"{executable}: {exc}")
    detail = "; ".join(errors) or "Codex CLI was not found"
    raise RuntimeError(f"无法读取 Codex 自带模型目录: {detail}")


def _refresh_codex_model_catalog(cfg=None) -> Path:
    cfg = cfg or get_config()
    merged = merge_model_catalog(_load_codex_catalog(), cfg)
    target = get_model_catalog_target_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def _refresh_codex_model_catalog_if_active(cfg) -> None:
    lines = _read_toml_lines(_CODEX_TOML)
    configured_catalog = _get_toml_key(lines, "model_catalog_json")
    if configured_catalog or get_model_catalog_path() is not None:
        try:
            _refresh_codex_model_catalog(cfg)
        except Exception as exc:
            logger.warning("刷新 Codex Bridge 模型目录失败: %s", exc)


# ═══════════════════════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════════════════════

@router.get("/status")
async def get_status():
    """代理运行状态"""
    cfg = get_config()
    stats = get_stats()
    return {
        "running": True,
        "host": cfg.server_host,
        "port": cfg.server_port,
        "version": "0.2.0",
        "stats": stats.get_summary(),
    }


# ═══════════════════════════════════════════════════════════════════
# 模型 CRUD
# ═══════════════════════════════════════════════════════════════════

@router.get("/models")
async def list_models():
    """Return the native catalog entries and the editable custom model library."""
    cfg = get_config()
    reg = get_registry()
    providers = cfg.providers
    mapping = cfg.model_mapping

    models = []
    for alias, metadata in cfg.native_models.items():
        if not isinstance(metadata, dict):
            metadata = {}
        models.append({
            "alias": alias,
            "display_name": metadata.get("display_name", alias),
            "description": metadata.get("description", "OpenAI Codex native model"),
            "target_model": metadata.get("target", alias),
            "provider": "native_codex",
            "adapter": "native_codex",
            "wire_api": "responses",
            "route_kind": "native_codex",
            "base_url": "",
            "api_key_env": "",
            "api_key_set": False,
            "enabled": metadata.get("enabled", True),
            "capabilities": metadata.get("capabilities", {}),
            "read_only": True,
            "is_multimodal": True,
            "vision_alias": "",
            "is_image_gen": False,
            "image_gen_alias": "",
            "is_video_gen": False,
            "video_gen_alias": "",
            "is_reasoning_text": True,
            "available_adapters": reg.list(),
        })
    for alias, entry in mapping.items():
        target = entry.get("target", alias)
        provider_name = entry.get("provider", "") or _find_provider_for_target(target, providers)
        provider = providers.get(provider_name, {})
        context_settings = custom_model_context_settings(alias, entry)
        models.append({
            "alias": alias,
            "display_name": entry.get("display_name", alias),
            "description": entry.get("description", ""),
            "target_model": target,
            "provider": provider_name or "",
            "adapter": provider.get("adapter", ""),
            "wire_api": str(entry.get("wire_api") or entry.get("protocol") or provider.get("wire_api") or provider.get("protocol") or (
                "responses" if provider_name.strip().lower() == "deepseek" else "chat"
            )).strip().lower(),
            "base_url": provider.get("base_url", ""),
            "api_key_env": provider.get("api_key_env", ""),
            "use_proxy": bool(entry.get("use_proxy", False)),
            "proxy_url": str(entry.get("proxy_url") or ""),
            "api_key_set": bool(provider.get("api_key", "")),
            "enabled": entry.get("enabled", True) if isinstance(entry, dict) else provider.get("enabled", True),
            "route_kind": "custom",
            "capabilities": entry.get("capabilities", {}),
            "effective_context_window": context_settings["context_window"],
            "effective_auto_compact_token_limit": context_settings["auto_compact_token_limit"],
            "default_context_window": context_settings["default_context_window"],
            "default_auto_compact_token_limit": context_settings["default_auto_compact_token_limit"],
            "read_only": False,
            "is_multimodal": entry.get("is_multimodal", False),
            "vision_alias": entry.get("vision_alias") or "",
            "is_image_gen": entry.get("is_image_gen", False),
            "image_gen_alias": entry.get("image_gen_alias") or "",
            "is_video_gen": entry.get("is_video_gen", False),
            "video_gen_alias": entry.get("video_gen_alias") or "",
            "is_reasoning_text": entry.get("is_reasoning_text", False),
            "available_adapters": reg.list(),
        })
    return {"models": models}


@router.post("/models")
async def add_model(data: dict):
    """添加模型映射"""
    cfg = get_config()
    alias = data.get("alias", "").strip()
    target = data.get("target_model", "").strip()

    if not alias or not target:
        return {"error": "alias 和 target_model 为必填项"}, 400
    if data.get("use_proxy") and not str(data.get("proxy_url") or "").strip():
        return {"error": "启用代理时必须填写代理地址"}, 400

    provider_name = data.get("provider", target)
    with _edit_config(cfg) as config_data:
        # 更新 provider 信息
        providers = config_data.setdefault("providers", {})
        if provider_name not in providers:
            providers[provider_name] = {
                "adapter": data.get("adapter", provider_name),
                "wire_api": data.get("wire_api", "chat"),
                "base_url": data.get("base_url", ""),
                "api_key_env": data.get("api_key_env", ""),
                "enabled": True,
            }
        else:
            # 更新已有的 provider 字段
            p = providers[provider_name]
            if "adapter" in data:
                p["adapter"] = data["adapter"]
            if "base_url" in data:
                p["base_url"] = data["base_url"]
            if "api_key_env" in data:
                p["api_key_env"] = data["api_key_env"]

        if data.get("api_key"):
            providers[provider_name]["api_key"] = data["api_key"]

        advanced = data.get("advanced", {})
        if advanced:
            providers[provider_name].update({
                "timeout": advanced.get("timeout", 120),
                "max_retries": advanced.get("max_retries", 0),
                "tool_calls_enabled": advanced.get("tool_calls_enabled", True),
                "extra_headers": advanced.get("extra_headers", {}),
            })

        mapping = config_data.setdefault("model_mapping", {})
        mapping[alias] = {
            "display_name": str(data.get("display_name") or alias).strip(),
            "description": str(data.get("description") or "").strip(),
            "target": target,
            "provider": provider_name,
            "route_kind": "custom",
            "wire_api": str(data.get("wire_api") or providers[provider_name].get("wire_api") or "chat").strip().lower(),
            "enabled": data.get("enabled", True),
            "capabilities": dict(data.get("capabilities") or {}),
            "is_multimodal": data.get("is_multimodal", False),
            "vision_alias": data.get("vision_alias") or None,
            "is_image_gen": data.get("is_image_gen", False),
            "image_gen_alias": data.get("image_gen_alias") or None,
            "is_video_gen": data.get("is_video_gen", False),
            "video_gen_alias": data.get("video_gen_alias") or None,
            "is_reasoning_text": data.get("is_reasoning_text", False),
            "use_proxy": bool(data.get("use_proxy", False)),
            "proxy_url": str(data.get("proxy_url") or "").strip(),
        }
    _refresh_codex_model_catalog_if_active(cfg)
    return {"status": "ok", "alias": alias}


@router.put("/models/{alias}")
async def update_model(alias: str, data: dict):
    """更新模型配置"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    old_entry = mapping[alias]
    old_target = old_entry.get("target", old_entry) if isinstance(old_entry, dict) else old_entry
    old_dict = old_entry if isinstance(old_entry, dict) else {}
    use_proxy = bool(data.get("use_proxy", old_dict.get("use_proxy", False)))
    proxy_url = str(
        data.get("proxy_url") if "proxy_url" in data else old_dict.get("proxy_url", "")
    ).strip()
    if use_proxy and not proxy_url:
        return {"error": "启用代理时必须填写代理地址"}, 400

    target = data.get("target_model", old_target)
    providers = cfg._data.get("providers", {})
    provider_name = data.get("provider", old_entry.get("provider", "") if isinstance(old_entry, dict) else "")

    # 回退：如果 provider_name 为空，从 target 反查 provider
    if not provider_name or provider_name not in providers:
        found = _find_provider_for_target(old_target, providers)
        if found:
            provider_name = found

    with _edit_config(cfg) as config_data:
        mapping = config_data.setdefault("model_mapping", {})
        providers = config_data.setdefault("providers", {})
        mapping[alias] = {
            "display_name": str(data.get("display_name") or old_dict.get("display_name") or alias).strip(),
            "description": str(data.get("description") if "description" in data else old_dict.get("description", "")).strip(),
            "target": target,
            "provider": provider_name,
            "route_kind": "custom",
            "wire_api": str(data.get("wire_api") or old_dict.get("wire_api") or providers.get(provider_name, {}).get("wire_api") or "chat").strip().lower(),
            "enabled": data.get("enabled", old_dict.get("enabled", True)),
            "capabilities": dict(data.get("capabilities") if "capabilities" in data else old_dict.get("capabilities", {})),
            "is_multimodal": data.get("is_multimodal", old_dict.get("is_multimodal", False)),
            "vision_alias": data.get("vision_alias") if "vision_alias" in data else old_dict.get("vision_alias"),
            "is_image_gen": data.get("is_image_gen", old_dict.get("is_image_gen", False)),
            "image_gen_alias": data.get("image_gen_alias") if "image_gen_alias" in data else old_dict.get("image_gen_alias"),
            "is_video_gen": data.get("is_video_gen", old_dict.get("is_video_gen", False)),
            "video_gen_alias": data.get("video_gen_alias") if "video_gen_alias" in data else old_dict.get("video_gen_alias"),
            "is_reasoning_text": data.get("is_reasoning_text", old_dict.get("is_reasoning_text", False)),
            "use_proxy": use_proxy,
            "proxy_url": proxy_url,
        }

        if provider_name and provider_name in providers:
            p = providers[provider_name]
            if "adapter" in data:
                p["adapter"] = data["adapter"]
            if "base_url" in data:
                p["base_url"] = data["base_url"]
            if "api_key" in data and data["api_key"]:
                p["api_key"] = data["api_key"]
            if "api_key_env" in data:
                p["api_key_env"] = data["api_key_env"]
            advanced = data.get("advanced", {})
            if advanced:
                p["timeout"] = advanced.get("timeout", p.get("timeout", 120))
                p["max_retries"] = advanced.get("max_retries", p.get("max_retries", 0))
                p["tool_calls_enabled"] = advanced.get("tool_calls_enabled", p.get("tool_calls_enabled", True))
                p["extra_headers"] = advanced.get("extra_headers", p.get("extra_headers", {}))

    _refresh_codex_model_catalog_if_active(cfg)
    return {"status": "ok", "alias": alias}


@router.delete("/models/{alias}")
async def delete_model(alias: str):
    """删除模型映射"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    with _edit_config(cfg) as config_data:
        del config_data["model_mapping"][alias]
    _refresh_codex_model_catalog_if_active(cfg)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# 连接测试
# ═══════════════════════════════════════════════════════════════════

@router.post("/models/{alias}/test")
async def test_connection(alias: str, data: dict | None = None):
    """测试模型连接"""
    cfg = get_config()
    reg = get_registry()
    mapping = cfg._data.get("model_mapping", {})

    entry = mapping.get(alias, alias)
    if isinstance(entry, dict):
        target = entry.get("target", alias)
        provider_name = entry.get("provider", "") or _find_provider_for_target(target, cfg.providers)
    else:
        target = entry
        provider_name = _find_provider_for_target(target, cfg.providers)

    if data:
        target = str(data.get("target_model") or target).strip()
        provider_name = str(data.get("provider") or provider_name or "").strip()

    if not provider_name:
        return {"status": "error", "message": f"未找到模型 '{alias}' 的 provider 配置"}

    provider = dict(cfg.providers.get(provider_name, {}))
    model_settings = dict(entry) if isinstance(entry, dict) else {}
    if data:
        if "base_url" in data:
            provider["base_url"] = data["base_url"]
        if "api_key" in data and data["api_key"]:
            provider["api_key"] = data["api_key"]
        if "adapter" in data:
            provider["adapter"] = data["adapter"]
        if "wire_api" in data:
            model_settings["wire_api"] = data["wire_api"]
        if "use_proxy" in data:
            model_settings["use_proxy"] = bool(data["use_proxy"])
        if "proxy_url" in data:
            model_settings["proxy_url"] = str(data["proxy_url"] or "").strip()
    adapter_name = provider.get("adapter") or "openai"
    adapter = reg.get(adapter_name)
    if not adapter:
        return {"status": "error", "message": f"未找到适配器 '{adapter_name}'"}
    import copy as _copy
    adapter = _copy.copy(adapter)

    api_key = data.get("api_key") if data else None
    if not api_key:
        api_key = provider.get("api_key", "")
    if not api_key:
        return {"status": "error", "message": "API Key 未设置"}

    # 临时覆盖 base_url
    if provider.get("base_url"):
        adapter.base_url = provider["base_url"]

    # 生图模型直接测生图端点
    is_image_gen = entry.get("is_image_gen", False) if isinstance(entry, dict) else False

    headers = adapter.get_headers(api_key)

    if is_image_gen:
        img_url = adapter.build_image_gen_url()
        img_body = adapter.preprocess_image_gen_request({
            "model": target,
            "prompt": "test",
            "n": 1,
        })
        start = time.time()
        async with make_provider_async_client(
            proxy_url=(
                str(model_settings.get("proxy_url") or "").strip()
                if model_settings.get("use_proxy")
                else ""
            ),
            timeout=httpx.Timeout(30),
        ) as client:
            resp = await client.post(img_url, json=img_body, headers=headers)
            elapsed = (time.time() - start) * 1000
            if resp.status_code == 200:
                return {
                    "status": "ok",
                    "elapsed_ms": round(elapsed, 1),
                    "message": f"生图连接成功 ({resp.status_code})",
                }
            return {
                "status": "error",
                "elapsed_ms": round(elapsed, 1),
                "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }

    # 构建两种测试请求
    headers = adapter.get_headers(api_key)

    if model_uses_responses(provider_name, provider, model_settings):
        base = adapter.base_url.rstrip("/")
        responses_url = base if base.endswith("/responses") else f"{base}/responses"
        responses_body = {
            "model": target,
            "input": "Reply only OK",
            "max_output_tokens": 8,
            "stream": False,
        }
        start = time.time()
        try:
            async with make_provider_async_client(
                proxy_url=(
                    str(model_settings.get("proxy_url") or "").strip()
                    if model_settings.get("use_proxy")
                    else ""
                ),
                timeout=httpx.Timeout(15),
            ) as client:
                resp = await client.post(responses_url, json=responses_body, headers=headers)
                elapsed = (time.time() - start) * 1000
                if resp.status_code == 200:
                    return {
                        "status": "ok",
                        "elapsed_ms": round(elapsed, 1),
                        "message": f"Responses 连接成功 ({resp.status_code})",
                    }
                return {
                    "status": "error",
                    "elapsed_ms": round(elapsed, 1),
                    "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }
        except httpx.TimeoutException:
            return {"status": "error", "message": "Responses 连接超时（15秒）"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # 先尝试 chat 端点
    chat_url = adapter.build_chat_url()
    chat_body = adapter.preprocess_chat_request({
        "model": target,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
        "stream": False,
    })

    start = time.time()
    try:
        async with make_provider_async_client(
            proxy_url=(
                str(model_settings.get("proxy_url") or "").strip()
                if model_settings.get("use_proxy")
                else ""
            ),
            timeout=httpx.Timeout(15),
        ) as client:
            resp = await client.post(chat_url, json=chat_body, headers=headers)
            elapsed = (time.time() - start) * 1000

            if resp.status_code == 200:
                return {
                    "status": "ok",
                    "elapsed_ms": round(elapsed, 1),
                    "message": f"连接成功 ({resp.status_code})",
                }

            # 如果 chat 失败且提示不支持该 API，回退到生图端点
            resp_text = resp.text.lower()
            if resp.status_code in (400, 404, 405):
                img_url = adapter.build_image_gen_url()
                img_body = adapter.preprocess_image_gen_request({
                    "model": target,
                    "prompt": "test",
                    "n": 1,
                })
                img_start = time.time()
                img_resp = await client.post(img_url, json=img_body, headers=headers)
                img_elapsed = (time.time() - img_start) * 1000

                if img_resp.status_code == 200:
                    return {
                        "status": "ok",
                        "elapsed_ms": round(img_elapsed, 1),
                        "message": f"生图连接成功 ({img_resp.status_code})",
                    }
                return {
                    "status": "error",
                    "elapsed_ms": round(img_elapsed, 1),
                    "message": f"HTTP {img_resp.status_code}: {img_resp.text[:200]}",
                }

            return {
                "status": "error",
                "elapsed_ms": round(elapsed, 1),
                "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
    except httpx.TimeoutException:
        return {"status": "error", "message": "连接超时（15秒）"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ═══════════════════════════════════════════════════════════════════
# 功能模型槽位（新版桌面 UI）
# ═══════════════════════════════════════════════════════════════════

_SLOT_DEFAULT_ALIASES = {
    "text": "gpt-5.5",
    "reasoning_text": "gpt-5.5-reasoning",
    "responses": "deepseek-v4-pro-responses",
    "vision": "gpt-5",
    "image_gen": "dall-e-3",
    "video_gen": "gpt-video",
}


def _inferred_slot_alias(slot_id: str, mapping: dict) -> str:
    preferred = _SLOT_DEFAULT_ALIASES[slot_id]
    if preferred in mapping:
        return preferred
    for alias, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        if slot_id == "vision" and entry.get("is_multimodal", False):
            return alias
        if slot_id == "image_gen" and entry.get("is_image_gen", False):
            return alias
        if slot_id == "video_gen" and entry.get("is_video_gen", False):
            return alias
        if slot_id == "responses" and entry.get("wire_api") == "responses":
            return alias
        if slot_id == "text" and not any(
            entry.get(flag, False)
            for flag in ("is_multimodal", "is_image_gen", "is_video_gen")
        ) and not entry.get("is_reasoning_text") and entry.get("wire_api", "chat") != "responses":
            return alias
        if slot_id == "reasoning_text" and entry.get("is_reasoning_text", False) and entry.get("wire_api", "chat") != "responses":
            return alias
    return preferred


def _slot_payload(slot_id: str, cfg) -> dict:
    mapping = cfg.model_mapping
    saved = cfg._data.get("model_slots", {}).get(slot_id, {})
    alias = saved.get("alias") or _inferred_slot_alias(slot_id, mapping)
    entry = mapping.get(alias, {})
    entry = entry if isinstance(entry, dict) else {"target": entry}
    target = saved.get("target") or saved.get("target_model") or entry.get("target", "")
    provider_name = saved.get("provider") or entry.get("provider", "")
    if not provider_name and target:
        provider_name = _find_provider_for_target(target, cfg.providers) or ""
    provider = cfg.providers.get(provider_name, {})
    wire_api = str(entry.get("wire_api") or provider.get("wire_api") or "chat").strip().lower()
    return {
        "slot_id": slot_id,
        "alias": alias,
        "target_model": target,
        "provider": provider_name,
        "enabled": saved.get("enabled", entry.get("enabled", True)),
        "adapter": provider.get("adapter", "openai"),
        "base_url": provider.get("base_url", ""),
        "api_key_env": provider.get("api_key_env", ""),
        "api_key_set": bool(provider.get("api_key", "")),
        "wire_api": wire_api,
        "is_responses": slot_id == "responses",
        "is_multimodal": slot_id == "vision",
        "is_image_gen": slot_id == "image_gen",
        "is_video_gen": slot_id == "video_gen",
        "is_reasoning_text": slot_id == "reasoning_text",
        "available_adapters": [],
    }


@router.get("/slots")
async def list_slots():
    """Return functional model assignments for the newer desktop UI."""
    cfg = get_config()
    return {"slots": [_slot_payload(slot_id, cfg) for slot_id in _SLOT_DEFAULT_ALIASES]}


@router.put("/slots/{slot_id}")
async def update_slot(slot_id: str, data: dict):
    if slot_id not in _SLOT_DEFAULT_ALIASES:
        return {"error": f"未知功能槽位 '{slot_id}'"}, 404
    cfg = get_config()
    alias = str(data.get("alias") or _SLOT_DEFAULT_ALIASES[slot_id]).strip()
    mapping = cfg._data.setdefault("model_mapping", {})
    existing = mapping.get(alias)
    if isinstance(existing, dict):
        if existing.get("route_kind") == "native_codex" or existing.get("provider") == "native_codex":
            return {"error": "Native 官方模型不能作为第三方能力 Slot"}, 400
        target = str(existing.get("target", "")).strip()
        provider_name = str(existing.get("provider", "")).strip()
    else:
        # Legacy callers may still create a missing model inline. Preserve that
        # migration path, but new clients select an existing registry alias.
        target = str(data.get("target_model", "")).strip()
        provider_name = str(data.get("provider", "")).strip()
    if not alias or not target or not provider_name:
        return {"error": "请选择已配置模型，或提供 alias、provider 和 target_model"}, 400

    with _edit_config(cfg) as config_data:
        mapping = config_data.setdefault("model_mapping", {})
        providers = config_data.setdefault("providers", {})
        provider = providers.setdefault(provider_name, {})
        if data.get("adapter"):
            provider["adapter"] = str(data["adapter"]).strip()
        else:
            provider.setdefault("adapter", "openai")
        if "base_url" in data:
            provider["base_url"] = data["base_url"]
        if "api_key_env" in data:
            provider["api_key_env"] = data["api_key_env"]
        if data.get("api_key"):
            provider["api_key"] = data["api_key"]

        slots = config_data.setdefault("model_slots", {})
        slots[slot_id] = {
            "alias": alias,
            "provider": provider_name,
            "target": target,
            "enabled": True,
            "wire_api": "responses" if slot_id == "responses" else str(existing.get("wire_api") or "chat") if isinstance(existing, dict) else "chat",
        }

        if not isinstance(existing, dict):
            mapping[alias] = {
                "display_name": alias,
                "target": target,
                "provider": provider_name,
                "route_kind": "custom",
                "wire_api": "responses" if slot_id == "responses" else "chat",
                "enabled": True,
                "is_multimodal": slot_id == "vision",
                "is_image_gen": slot_id == "image_gen",
                "is_video_gen": slot_id == "video_gen",
                "is_reasoning_text": slot_id == "reasoning_text",
            }
    return {"status": "ok", "slot": _slot_payload(slot_id, cfg)}


@router.post("/slots/{slot_id}/test")
async def test_slot(slot_id: str, data: dict | None = None):
    if slot_id not in _SLOT_DEFAULT_ALIASES:
        return {"status": "error", "message": f"未知功能槽位 '{slot_id}'"}
    cfg = get_config()
    slot = _slot_payload(slot_id, cfg)
    alias = str((data or {}).get("alias") or slot["alias"])
    return await test_connection(alias, data)


# ═══════════════════════════════════════════════════════════════════
# 全局设置
# ═══════════════════════════════════════════════════════════════════

@router.get("/settings")
async def get_settings():
    """获取全局设置"""
    cfg = get_config()
    server_cfg = cfg._data.get("server", {})
    log_level = server_cfg.get("log_level", "info")
    audit_log_path = server_cfg.get("audit_log_path", "")
    if str(log_level).lower() == "debug" and not str(audit_log_path).strip():
        audit_log_path = cfg.default_audit_log_path()
    native_auth = server_cfg.get("native_auth_injection", {})
    if not isinstance(native_auth, dict):
        native_auth = {}
    return {
        "server": {
            "host": cfg.server_host,
            "port": cfg.server_port,
            "log_level": log_level,
            "launch_at_login": server_cfg.get("launch_at_login", False),
            "auto_start": server_cfg.get("auto_start", False),
            "close_to_tray": server_cfg.get("close_to_tray", True),
            "audit_log_path": audit_log_path,
            "codex_official_proxy_url": server_cfg.get("codex_official_proxy_url", ""),
            "native_auth_injection": {
                "enabled": bool(native_auth.get("enabled", False)),
                "auth_file": str(native_auth.get("auth_file") or ""),
                "auth_file_found": resolve_auth_file(cfg).is_file(),
            },
        },
        "config_path": str(cfg._config_path) if cfg._config_path else "",
    }


@router.put("/settings")
async def update_settings(data: dict):
    """更新全局设置"""
    cfg = get_config()
    native_update = data.get("native_auth_injection")
    if isinstance(native_update, dict):
        if native_update.get("enabled") is True:
            if not access_control_enabled(cfg):
                raise HTTPException(status_code=400, detail="Enable LAN BRIDGE access control before login injection")
            try:
                has_enabled_key = any(
                    record.get("enabled", True)
                    for record in get_access_key_store(cfg).list_records()
                )
            except BridgeAccessError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not has_enabled_key:
                raise HTTPException(status_code=400, detail="Create an access key before enabling login injection")
    requested_port = None
    if "port" in data:
        try:
            requested_port = int(data["port"])
        except (TypeError, ValueError):
            return {"error": "服务端口必须是整数"}, 400
        if not 1 <= requested_port <= 65535:
            return {"error": "服务端口必须在 1 到 65535 之间"}, 400

    with _edit_config(cfg) as config_data:
        server_cfg = config_data.setdefault("server", {})
        if "host" in data:
            host = str(data["host"]).strip()
            if not host:
                return {"error": "服务监听地址不能为空"}, 400
            server_cfg["host"] = host
        if requested_port is not None:
            server_cfg["port"] = requested_port
        if "log_level" in data:
            server_cfg["log_level"] = data["log_level"]
        if "launch_at_login" in data:
            server_cfg["launch_at_login"] = bool(data["launch_at_login"])
        if "auto_start" in data:
            server_cfg["auto_start"] = bool(data["auto_start"])
        if "close_to_tray" in data:
            server_cfg["close_to_tray"] = bool(data["close_to_tray"])
        if "audit_log_path" in data:
            server_cfg["audit_log_path"] = data["audit_log_path"]
        if "codex_official_proxy_url" in data:
            server_cfg["codex_official_proxy_url"] = str(data["codex_official_proxy_url"]).strip()
        if isinstance(data.get("native_auth_injection"), dict):
            update = data["native_auth_injection"]
            native_auth = server_cfg.setdefault("native_auth_injection", {})
            if "enabled" in update:
                native_auth["enabled"] = bool(update["enabled"])
            if "auth_file" in update:
                native_auth["auth_file"] = str(update["auth_file"] or "").strip()

        if str(server_cfg.get("log_level", "")).lower() == "debug" and not str(server_cfg.get("audit_log_path", "")).strip():
            server_cfg["audit_log_path"] = cfg.default_audit_log_path()
    return {"status": "ok", "message": "设置已保存，部分设置需重启后生效"}


# ═══════════════════════════════════════════════════════════════════
# 联网搜索设置
# ═══════════════════════════════════════════════════════════════════

def _public_web_search_config(cfg) -> dict:
    settings = cfg._data.get("web_search", {})
    providers = {}
    for name, stored in settings.get("providers", {}).items():
        provider = dict(stored)
        env_key = provider.get("api_key_env", "")
        provider["api_key_set"] = bool(provider.get("api_key") or (env_key and os.environ.get(env_key)))
        provider.pop("api_key", None)
        providers[name] = provider
    return {
        "enabled": settings.get("enabled", False),
        "active_provider": settings.get("active_provider", "bocha"),
        "max_rounds": settings.get("max_rounds", 3),
        "providers": providers,
    }


@router.get("/web-search")
async def get_web_search_settings():
    """获取联网搜索配置，密钥仅返回是否已设置。"""
    return _public_web_search_config(get_config())


@router.put("/web-search")
async def update_web_search_settings(data: dict):
    """更新联网搜索配置。"""
    cfg = get_config()
    with _edit_config(cfg) as config_data:
        settings = config_data.setdefault("web_search", {})
        if "enabled" in data:
            settings["enabled"] = bool(data["enabled"])
        settings["active_provider"] = str(data.get("active_provider") or settings.get("active_provider") or "bocha")

        update = data.get("provider") or {}
        provider_name = settings["active_provider"]
        providers = settings.setdefault("providers", {})
        stored = providers.setdefault(provider_name, {})
        for key in (
            "adapter", "display_name", "base_url", "api_key_env", "enabled",
            "timeout", "max_results", "summary", "freshness",
        ):
            if key in update:
                stored[key] = update[key]
        if update.get("api_key"):
            stored["api_key"] = update["api_key"]
    return {"status": "ok", "web_search": _public_web_search_config(cfg)}


@router.post("/web-search/test")
async def test_web_search(data: dict):
    """使用表单中的 Bocha 配置执行一次连接测试。"""
    cfg = get_config()
    settings = cfg._data.get("web_search", {})
    name = str(data.get("active_provider") or settings.get("active_provider") or "bocha")
    stored = dict(settings.get("providers", {}).get(name, {}))
    form_provider = dict(data.get("provider") or {})
    form_api_key = form_provider.pop("api_key", "")
    stored.update(form_provider)
    if form_api_key:
        stored["api_key"] = form_api_key
    api_key = stored.get("api_key", "")
    env_key = stored.get("api_key_env", "")
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key:
        return {"status": "error", "message": "API Key 未设置"}
    url = str(stored.get("base_url", "")).strip()
    if not url:
        return {"status": "error", "message": "搜索 API 地址未设置"}

    body = {
        "query": str(data.get("query") or "OpenAI").strip(),
        "freshness": stored.get("freshness", "noLimit"),
        "summary": bool(stored.get("summary", True)),
        "count": int(stored.get("max_results", 5)),
    }
    start = time.time()
    try:
        timeout = float(stored.get("timeout", 30))
        async with make_async_client(timeout=httpx.Timeout(timeout)) as client:
            response = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        elapsed = round((time.time() - start) * 1000, 1)
        if response.status_code != 200:
            return {"status": "error", "elapsed_ms": elapsed, "message": f"HTTP {response.status_code}"}
        payload = response.json()
        items = payload.get("data", {}).get("webPages", {}).get("value", [])
        results = [
            {"title": item.get("name", ""), "url": item.get("url", "")}
            for item in items[:body["count"]]
        ]
        return {"status": "ok", "elapsed_ms": elapsed, "result_count": len(items), "results": results}
    except httpx.TimeoutException:
        return {"status": "error", "message": "连接超时"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ═══════════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════════

@router.get("/logs")
async def get_logs(limit: int = 100):
    """获取最近请求日志"""
    stats = get_stats()
    return {"logs": stats.get_recent_logs(limit)}


@router.get("/usage")
async def get_usage(day: str | None = None):
    """Get persisted token usage totals. Pass day=YYYY-MM-DD for one day."""
    stats = get_stats()
    return {"usage": stats.get_usage_summary(day)}


def _available_access_models(cfg) -> list[dict[str, str]]:
    models: dict[str, dict[str, str]] = {}
    has_image_model = False
    for alias, entry in cfg.native_models.items():
        if isinstance(entry, dict) and not entry.get("enabled", True):
            continue
        models[str(alias)] = {
            "alias": str(alias),
            "display_name": str(entry.get("display_name") or alias) if isinstance(entry, dict) else str(alias),
            "route_kind": "native_codex",
        }
    for alias, entry in cfg.model_mapping.items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        native = (
            entry.get("route_kind") == "native_codex"
            or entry.get("kind") == "native_codex"
            or entry.get("provider") == "native_codex"
        )
        models[str(alias)] = {
            "alias": str(alias),
            "display_name": str(entry.get("display_name") or alias),
            "route_kind": "native_codex" if native else "custom",
        }
        has_image_model = has_image_model or bool(entry.get("is_image_gen"))
    if has_image_model:
        for alias in ("gpt-image-1", "gpt-image-1.5", "gpt-image-2"):
            models.setdefault(alias, {
                "alias": alias,
                "display_name": f"{alias} (image bridge alias)",
                "route_kind": "custom",
            })
    return sorted(models.values(), key=lambda item: (item["route_kind"] != "native_codex", item["alias"]))


def _access_key_payload(record: dict, usage: dict) -> dict:
    payload = public_access_key_record(record)
    totals = usage.get("by_key", {}).get(payload["id"], {})
    payload.update({
        "last_used_at": totals.get("last_used_at") or None,
        "request_count": int(totals.get("requests", 0)),
        "total_tokens": int(totals.get("tokens", 0)),
    })
    return payload


@router.get("/access-keys")
async def list_access_keys():
    cfg = get_config()
    try:
        records = get_access_key_store(cfg).list_records()
    except BridgeAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    usage = get_stats().get_usage_summary()
    return {
        "keys": [_access_key_payload(record, usage) for record in records],
        "available_models": _available_access_models(cfg),
    }


@router.post("/access-keys")
async def create_access_key(data: dict):
    cfg = get_config()
    allowed = {item["alias"] for item in _available_access_models(cfg)}
    requested = [str(item) for item in data.get("allowed_models", [])]
    invalid = sorted(set(requested) - allowed - {"*"})
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown model permission: {invalid[0]}")
    try:
        raw_key, record = get_access_key_store(cfg).create(data.get("name", ""), requested)
        if not access_control_enabled(cfg):
            with _edit_config(cfg) as config_data:
                config_data.setdefault("access_control", {})["enabled"] = True
    except (ValueError, BridgeAccessError) as exc:
        status = exc.status_code if isinstance(exc, BridgeAccessError) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"key": raw_key, "record": _access_key_payload(record, {})}


@router.put("/access-keys/{key_id}")
async def update_access_key(key_id: str, data: dict):
    cfg = get_config()
    allowed = {item["alias"] for item in _available_access_models(cfg)}
    if "allowed_models" in data:
        requested = [str(item) for item in data.get("allowed_models", [])]
        invalid = sorted(set(requested) - allowed - {"*"})
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown model permission: {invalid[0]}")
    try:
        record = get_access_key_store(cfg).update(key_id, data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Access key not found") from exc
    except (ValueError, BridgeAccessError) as exc:
        status = exc.status_code if isinstance(exc, BridgeAccessError) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _access_key_payload(record, get_stats().get_usage_summary())


@router.post("/access-keys/{key_id}/rotate")
async def rotate_access_key(key_id: str):
    cfg = get_config()
    try:
        raw_key, record = get_access_key_store(cfg).rotate(key_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Access key not found") from exc
    except BridgeAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "key": raw_key,
        "record": _access_key_payload(record, get_stats().get_usage_summary()),
    }


@router.delete("/access-keys/{key_id}")
async def delete_access_key(key_id: str):
    try:
        get_access_key_store(get_config()).revoke(key_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Access key not found") from exc
    except BridgeAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"status": "ok"}


@router.post("/logs/clear")
async def clear_logs():
    """清空日志"""
    get_stats().clear_logs()
    return {"status": "ok"}


@router.websocket("/logs/stream")
async def logs_stream(websocket: WebSocket):
    """WebSocket 实时日志流"""
    await websocket.accept()

    queue: asyncio.Queue = asyncio.Queue()

    def on_log(entry: dict):
        try:
            queue.put_nowait(entry)
        except Exception:
            pass

    stats = get_stats()
    stats.add_listener(on_log)

    try:
        while True:
            entry = await queue.get()
            await websocket.send_json(entry)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stats.remove_listener(on_log)


# ═══════════════════════════════════════════════════════════════════
# 配置导入导出
# ═══════════════════════════════════════════════════════════════════

@router.get("/config/export")
async def export_config():
    """导出完整配置（YAML 格式）"""
    import yaml
    cfg = get_config()
    # 深拷贝并移除敏感信息
    data = json.loads(json.dumps(cfg._data))
    for p in data.get("providers", {}).values():
        p.pop("api_key", None)
        p.pop("_api_key_from_env", None)
    for p in data.get("web_search", {}).get("providers", {}).values():
        p.pop("api_key", None)
        p.pop("_api_key_from_env", None)
    native_auth = data.get("server", {}).get("native_auth_injection", {})
    if isinstance(native_auth, dict):
        native_auth.pop("client_token", None)
        native_auth.pop("client_token_env", None)
    access_control = data.get("access_control", {})
    if isinstance(access_control, dict):
        access_control.pop("keys", None)
    return {
        "yaml": yaml.dump(data, allow_unicode=True, default_flow_style=False),
        "config_path": str(cfg._config_path) if cfg._config_path else "",
    }


@router.post("/config/import")
async def import_config(data: dict):
    """导入配置"""
    import yaml
    cfg = get_config()
    yaml_str = data.get("yaml", "")
    if not yaml_str:
        return {"error": "缺少 yaml 字段"}, 400
    try:
        new_data = yaml.safe_load(yaml_str)
        if not isinstance(new_data, dict):
            return {"error": "配置根节点必须是对象"}, 400
        access_control = new_data.get("access_control", {})
        if isinstance(access_control, dict):
            access_control.pop("keys", None)
        with _edit_config(cfg) as config_data:
            _deep_merge(config_data, new_data)
        return {"status": "ok"}
    except Exception as exc:
        return {"error": str(exc)}, 400


@router.post("/shutdown")
async def shutdown():
    """安全关闭代理"""
    import os
    import signal

    def _do_shutdown():
        # 延迟一瞬让响应返回
        import time
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    import threading
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "ok", "message": "正在关闭..."}


# ═══════════════════════════════════════════════════════════════════
# Codex config.toml 切换
# ═══════════════════════════════════════════════════════════════════

_CODEX_TOML = Path.home() / ".codex" / "config.toml"
_DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
_CODEX_NETWORK_PROXY_SECTION = "features.network_proxy"
_CODEX_CONFIG_BACKUP_LIMIT = 10
_MINIMAL_OFFICIAL_CODEX_CONFIG = [
    "# Emergency reset by LAN BRIDGE: use Codex built-in OpenAI defaults.",
]

# ── TOML 微型写入器（不依赖外部库） ──────────────────────────

def _read_toml_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if lines:
        lines[0] = lines[0].lstrip("\ufeff")
    return lines


def _write_toml_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(lines).rstrip("\n") + "\n"
    rendered_bytes = rendered.encode("utf-8")
    backup_dir: Path | None = None
    if path.exists():
        current = path.read_bytes()
        if current == rendered_bytes or current == b"\xef\xbb\xbf" + rendered_bytes:
            return
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(path, backup_dir / f"{path.name}.{stamp}.bak")

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_file.write(rendered_bytes)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temporary = Path(temp_file.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    if backup_dir is not None:
        backups = sorted(
            backup_dir.glob(f"{path.name}.*.bak"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in backups[_CODEX_CONFIG_BACKUP_LIMIT:]:
            stale.unlink(missing_ok=True)


def _section_range(lines: list[str], section: str | None = None) -> tuple[int, int]:
    if section is None:
        end = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
        return 0, end

    header = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$")
    start = next((i + 1 for i, line in enumerate(lines) if header.match(line)), -1)
    if start < 0:
        return -1, -1
    end = next((i for i in range(start, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
    return start, end


def _set_toml_key(lines: list[str], key: str, value: str, section: str | None = None) -> None:
    """Set a key within the top level or one explicit table."""
    start, end = _section_range(lines, section)
    if start < 0:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[{section}]", f"{key} = {value}"])
        return

    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start, end):
        if key_pattern.match(lines[i]):
            lines[i] = f"{key} = {value}"
            return
    lines.insert(end, f"{key} = {value}")


def _remove_toml_key(lines: list[str], key: str, section: str | None = None) -> None:
    """Remove a key from the top level or one explicit table."""
    start, end = _section_range(lines, section)
    if start < 0:
        return
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start, end):
        if key_pattern.match(lines[i]):
            lines.pop(i)
            return


def _remove_toml_section(lines: list[str], section: str) -> None:
    """Remove a [section_name] and all its keys until next [section] or EOF."""
    header = f"[{section}]"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        end += 1
    del lines[start:end]


def _get_toml_key(lines: list[str], key: str, section: str | None = None) -> str | None:
    start, end = _section_range(lines, section)
    if start < 0:
        return None
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*(?:#.*)?$")
    for line in lines[start:end]:
        match = key_pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        return value
    return None


def _ensure_codex_model(lines: list[str]) -> None:
    """Keep the user's selected Codex model; only seed a default when absent."""
    if _get_toml_key(lines, "model"):
        return
    _set_toml_key(lines, "model", f'"{_DEFAULT_CODEX_MODEL}"')


def _codex_bridge_settings_path() -> Path:
    return Path.home() / ".codex" / "lan-bridge" / _CODEX_BRIDGE_SETTINGS_FILENAME


def _legacy_codex_bridge_settings_path() -> Path:
    return get_bridge_root_dir() / _CODEX_BRIDGE_SETTINGS_FILENAME


def _write_codex_bridge_settings(path: Path, settings: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Global Codex settings written when switching Codex to Bridge.",
        "# Model context windows and compaction limits are configured per custom model.",
    ]
    for key in _DEFAULT_CODEX_BRIDGE_SETTINGS:
        lines.append(f"{key}: {settings[key]}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_file.write("\n".join(lines) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temporary = Path(temp_file.name)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _load_codex_bridge_settings() -> dict[str, int]:
    path = _codex_bridge_settings_path()
    defaults = dict(_DEFAULT_CODEX_BRIDGE_SETTINGS)
    if not path.exists():
        legacy_path = _legacy_codex_bridge_settings_path()
        if legacy_path != path and legacy_path.is_file():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_path, path)
            except OSError as exc:
                logger.warning("迁移旧 Codex Bridge 设置失败，将使用默认值: %s", exc)
        if not path.exists():
            _write_codex_bridge_settings(path, defaults)
            return defaults

    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("无法读取 Codex Bridge 设置，使用默认值: %s", exc)
        return defaults

    if not isinstance(loaded, dict):
        logger.warning("Codex Bridge 设置格式无效，使用默认值: %s", path)
        return defaults

    settings = {
        key: _positive_int(loaded.get(key), fallback)
        for key, fallback in defaults.items()
    }
    if any(key not in loaded for key in defaults) or _LEGACY_CODEX_BRIDGE_MODEL_SETTINGS.intersection(loaded):
        _write_codex_bridge_settings(path, settings)
    return settings


def _official_proxy_url() -> str:
    return str(get_config()._data.get("server", {}).get("codex_official_proxy_url", "")).strip()


def _codex_desktop_process() -> tuple[int, str] | None:
    """Return the running Codex/ChatGPT desktop root PID and executable, if present."""
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'Codex.exe' OR Name = 'ChatGPT.exe'\" | "
        "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        data = json.loads(completed.stdout)
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            executable = str(entry.get("ExecutablePath") or "")
            command_line = str(entry.get("CommandLine") or "")
            if (
                not executable
                or "--type=" in command_line
                or " app-server" in command_line
                or Path(executable).parent.name.lower() != "app"
                or Path(executable).name.lower() not in {"codex.exe", "chatgpt.exe"}
            ):
                continue
            return int(entry["ProcessId"]), executable
    except Exception:
        logger.exception("检测 Codex 桌面进程失败")
    return None


def _stop_codex_desktop(process: tuple[int, str] | None) -> None:
    if process is None:
        return
    pid, _ = process
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            logger.warning("关闭 Codex 桌面端失败 (PID %s): %s", pid, completed.stderr.strip())
    except Exception as exc:
        logger.warning("关闭 Codex 桌面端失败 (PID %s)，继续执行配置切换: %s", pid, exc)


def _restart_codex_desktop(process: tuple[int, str] | None, proxy_url: str = "") -> bool:
    if process is None:
        return False
    _, executable = process
    launch_command = [executable]
    package = re.search(
        r"\\WindowsApps\\(OpenAI\.Codex)_[^\\]*__([^\\]+)\\app\\(?:Codex|ChatGPT)\.exe$",
        executable,
        re.IGNORECASE,
    )
    if package:
        app_id = f"{package.group(1)}_{package.group(2)}!App"
        launch_command = ["explorer.exe", rf"shell:AppsFolder\{app_id}"]
    try:
        subprocess.Popen(
            launch_command,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        logger.exception("重新启动 Codex 桌面端失败")
        return False


def _restart_codex_after_response(process: tuple[int, str] | None, proxy_url: str = "") -> None:
    if process is None:
        return

    def _restart() -> None:
        try:
            time.sleep(1.5)
            _restart_codex_desktop(process, proxy_url)
        except Exception as exc:
            logger.warning("重新启动 Codex 桌面端失败，配置切换已完成: %s", exc)

    try:
        threading.Thread(target=_restart, daemon=True).start()
    except Exception as exc:
        logger.warning("无法创建 Codex 重启任务，配置切换已完成: %s", exc)


@router.get("/codex/status")
async def codex_config_status():
    """Read current Codex config.toml and determine if it's using Bridge."""
    toml_path = _CODEX_TOML
    exists = toml_path.exists()
    lines = _read_toml_lines(toml_path)
    provider = _get_toml_key(lines, "model_provider")
    openai_base_url = _get_toml_key(lines, "openai_base_url") or ""
    bridge_base_url = _bridge_base_url()
    using_builtin_bridge = openai_base_url.rstrip("/") == bridge_base_url.rstrip("/")
    details = {
        "model_provider": provider or "openai",
        "model": _get_toml_key(lines, "model") or "",
        "base_url": (
            openai_base_url
            or _get_toml_key(lines, "base_url", "model_providers.cnbridge")
            or _get_toml_key(lines, "base_url", "model_providers.custom")
            or ""
        ),
        "model_catalog_json": _get_toml_key(lines, "model_catalog_json") or "",
        "official_proxy_url": _official_proxy_url(),
    }

    return {
        "exists": exists,
        "using_bridge": using_builtin_bridge or provider in {"cnbridge", "custom"},
        "details": details,
        "catalog_available": get_model_catalog_path() is not None,
    }


def _bridge_base_url() -> str:
    cfg = get_config()
    host = cfg.server_host
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    port = cfg.server_port
    return f"http://{host}:{port}/v1"


@router.post("/codex/enable-unified")
@router.post("/codex/switch-to-custom")
async def codex_switch_to_custom():
    """Route the built-in OpenAI provider through Bridge for Remote visibility."""
    lines = _read_toml_lines(_CODEX_TOML)
    base_url = _bridge_base_url()
    bridge_settings = _load_codex_bridge_settings()
    try:
        catalog_path = _refresh_codex_model_catalog(get_config())
    except Exception as exc:
        logger.exception("生成 Codex Bridge 模型目录失败")
        return {"status": "error", "message": str(exc), "using_bridge": False}

    _remove_toml_key(lines, "model_provider")
    _set_toml_key(lines, "openai_base_url", json.dumps(base_url))
    _ensure_codex_model(lines)
    _remove_toml_key(lines, "model_context_window")
    _remove_toml_key(lines, "model_auto_compact_token_limit")
    _set_toml_key(lines, "tool_output_token_limit", str(bridge_settings["tool_output_token_limit"]))
    _set_toml_key(lines, "model_catalog_json", json.dumps(str(catalog_path)))

    _remove_toml_section(lines, "model_providers.cnbridge")
    _remove_toml_section(lines, "model_providers.custom")
    _set_toml_key(lines, "enable_request_compression", "false", "features")
    _remove_toml_section(lines, _CODEX_NETWORK_PROXY_SECTION)
    _write_toml_lines(_CODEX_TOML, lines)
    return {
        "status": "ok",
        "message": "已使用 Codex 内置 OpenAI Provider 接入统一 Bridge；新任务保留 Remote 可见性，DeepSeek Responses 原生续接由 Bridge 处理。重新加载 Codex 后生效，当前运行实例未被关闭。",
        "using_bridge": True,
        "codex_restarted": False,
        "catalog_available": True,
    }


@router.post("/codex/restore-official")
@router.post("/codex/switch-to-official")
async def codex_switch_to_official():
    """Restore official routing without deleting unrelated Codex settings.

    A valid config is edited surgically so MCP servers, projects, plugins and
    other user-owned sections survive the switch. Malformed or undecodable
    TOML still falls back to the minimal emergency config; the writer keeps a
    complete backup before either replacement.
    """
    try:
        lines = _read_toml_lines(_CODEX_TOML)
        tomllib.loads("\n".join(lines))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        lines = list(_MINIMAL_OFFICIAL_CODEX_CONFIG)
        preserved_settings = False
    else:
        for key in (
            "model_provider",
            "openai_base_url",
            "model_catalog_json",
            "model_context_window",
            "model_auto_compact_token_limit",
            "tool_output_token_limit",
        ):
            _remove_toml_key(lines, key)
        _set_toml_key(lines, "model", json.dumps(_DEFAULT_CODEX_MODEL))
        _remove_toml_section(lines, "model_providers.cnbridge")
        _remove_toml_section(lines, "model_providers.custom")
        _remove_toml_key(lines, "enable_request_compression", "features")
        _remove_toml_section(lines, _CODEX_NETWORK_PROXY_SECTION)
        preserved_settings = True

    _write_toml_lines(_CODEX_TOML, lines)
    return {
        "status": "ok",
        "message": (
            "已恢复官方直连并保留 MCP、项目、插件等用户配置。重新加载 Codex 后生效。"
            if preserved_settings
            else "原 Codex 配置无法解析，已备份并恢复最小官方直连。重新加载 Codex 后生效。"
        ),
        "using_bridge": False,
        "codex_restarted": False,
        "official_proxy_url": "",
        "preserved_settings": preserved_settings,
    }


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════

def _find_provider_for_target(target: str, providers: dict) -> str | None:
    """根据 target 名查找对应的 provider"""
    for pname, pinfo in providers.items():
        if pinfo.get("adapter") == target or pname == target:
            return pname
    for pname in providers:
        if pname in target.lower():
            return pname
    return None


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
