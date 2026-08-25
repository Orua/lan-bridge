"""配置管理 —— YAML 配置文件加载、环境变量注入、热加载"""

from __future__ import annotations

import copy
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

from .access_control import DEFAULT_ACCESS_CONTROL

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".lan-bridge.yaml",
    Path("config.yaml"),
]

_CONFIG_BACKUP_LIMIT = 10

DEFAULT_WEB_SEARCH = {
    "enabled": False,
    "active_provider": "bocha",
    "max_rounds": 3,
    "providers": {
        "bocha": {
            "adapter": "bocha",
            "display_name": "Bocha Web Search",
            "base_url": "https://api.bocha.cn/v1/web-search",
            "api_key_env": "BOCHA_API_KEY",
            "enabled": True,
            "timeout": 30,
            "max_results": 5,
            "summary": True,
            "freshness": "noLimit",
        },
    },
}

DEFAULT_NATIVE_MODELS = {
    "gpt-5.6-sol": {
        "display_name": "GPT-5.6 Sol",
        "description": "OpenAI Codex native model",
        "enabled": True,
    },
    "gpt-5.6-terra": {
        "display_name": "GPT-5.6 Terra",
        "description": "OpenAI Codex native model",
        "enabled": True,
    },
    "gpt-5.6-luna": {
        "display_name": "GPT-5.6 Luna",
        "description": "OpenAI Codex native model",
        "enabled": True,
    },
}

DEFAULT_NATIVE_AUTH_INJECTION = {
    "enabled": False,
    "auth_file": "",
}


def _load_dotenv(dotenv_path: Path) -> None:
    """简易 .env 解析器，无需 python-dotenv 依赖"""
    if not dotenv_path.is_file():
        return
    try:
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    """配置管理器"""

    def __init__(self, config_path: str | Path | None = None):
        self._config_path: Path | None = None
        self._data: dict[str, Any] = {}
        self._runtime_server_host: str | None = None
        self._runtime_server_port: int | None = None
        self._file_signature: tuple[int, int] | None = None
        self._reload_attempt_signature: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self.load(config_path)

    # ── 加载 ─────────────────────────────────────────────────────

    def load(self, config_path: str | Path | None = None) -> None:
        """加载配置文件并注入环境变量"""
        path = self._resolve_path(config_path)
        # 自动加载 .env 文件（优先级：config 目录 > 用户主目录）
        if path:
            _load_dotenv(path.parent / ".env")
        _load_dotenv(Path.home() / ".lan-bridge.env")
        if path and path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"配置文件根节点必须是对象: {path}")
        else:
            loaded = {}
        with self._lock:
            self._config_path = path
            self._data = loaded
            self._inject_env()
            self._file_signature = self._stat_signature(path)
            self._reload_attempt_signature = self._file_signature

    def reload(self) -> None:
        """重新加载配置（热加载用）"""
        self.load(self._config_path)

    def reload_if_changed(self) -> bool:
        """Reload an externally changed config while keeping the last valid state."""
        with self._lock:
            path = self._config_path
            previous = self._reload_attempt_signature
        current = self._stat_signature(path)
        if current == previous:
            return False
        # Some editors replace a file via rename. Keep serving the last valid
        # config while the path is briefly absent, then reload when it returns.
        if path is not None and current is None:
            return False
        with self._lock:
            self._reload_attempt_signature = current
        self.load(path)
        return True

    @staticmethod
    def _stat_signature(path: Path | None) -> tuple[int, int] | None:
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def save(self) -> None:
        """保存当前配置到文件"""
        with self._lock:
            if self._config_path:
                self.sync_slots_to_mapping()
                data = copy.deepcopy(self._data)
                self._redact_injected_keys_for_save(data.get("providers", {}))
                self._redact_injected_keys_for_save(data.get("web_search", {}).get("providers", {}))
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                yaml_text = yaml.dump(data, allow_unicode=True, default_flow_style=False)
                temp_path: Path | None = None
                backup_dir: Path | None = None
                try:
                    # Keep the temporary file on the same volume so os.replace
                    # is atomic on Windows as well as POSIX.
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        newline="\n",
                        prefix=f".{self._config_path.name}.",
                        suffix=".tmp",
                        dir=self._config_path.parent,
                        delete=False,
                    ) as temp_file:
                        temp_file.write(yaml_text)
                        temp_file.flush()
                        os.fsync(temp_file.fileno())
                        temp_path = Path(temp_file.name)
                    if self._config_path.exists():
                        current = self._config_path.read_text(encoding="utf-8")
                        if current != yaml_text:
                            backup_dir = self._config_path.parent / f".{self._config_path.stem}-backups"
                            backup_dir.mkdir(parents=True, exist_ok=True)
                            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                            current_data = yaml.safe_load(current) or {}
                            if not isinstance(current_data, dict):
                                raise ValueError(f"配置文件根节点必须是对象: {self._config_path}")
                            self._redact_all_provider_keys_for_backup(
                                current_data.get("providers", {})
                            )
                            self._redact_all_provider_keys_for_backup(
                                current_data.get("web_search", {}).get("providers", {})
                            )
                            self._redact_native_auth_for_backup(current_data)
                            backup_path = backup_dir / f"{self._config_path.name}.{stamp}.bak"
                            backup_path.write_text(
                                yaml.dump(current_data, allow_unicode=True, default_flow_style=False),
                                encoding="utf-8",
                            )
                    os.replace(temp_path, self._config_path)
                    temp_path = None
                    if backup_dir is not None:
                        backups = sorted(
                            backup_dir.glob(f"{self._config_path.name}.*.bak"),
                            key=lambda item: item.stat().st_mtime_ns,
                            reverse=True,
                        )
                        for stale in backups[_CONFIG_BACKUP_LIMIT:]:
                            try:
                                stale.unlink(missing_ok=True)
                            except OSError:
                                # Retention cleanup must never turn an already
                                # successful atomic save into a reported failure.
                                pass
                    self._file_signature = self._stat_signature(self._config_path)
                    self._reload_attempt_signature = self._file_signature
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Edit and persist a private copy, rolling memory back on failure."""
        with self._lock:
            original = self._data
            candidate = copy.deepcopy(original)
            try:
                yield candidate
                self._data = candidate
                self.save()
            except BaseException:
                self._data = original
                raise

    def set_runtime_server_address(self, host: str, port: int) -> None:
        """Record the address actually bound by the current process.

        CLI overrides must be visible to status responses and Codex URL
        generation without being persisted back into the user's YAML file.
        """
        normalized_host = str(host).strip()
        normalized_port = int(port)
        if not normalized_host:
            raise ValueError("服务监听地址不能为空")
        if not 1 <= normalized_port <= 65535:
            raise ValueError("服务端口必须在 1 到 65535 之间")
        with self._lock:
            self._runtime_server_host = normalized_host
            self._runtime_server_port = normalized_port

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    def default_audit_log_path(self) -> str:
        base_dir = self._config_path.parent if self._config_path else Path.home()
        return str(base_dir / "lan-bridge-audit.jsonl")

    @property
    def data(self) -> dict:
        """直接访问原始配置数据（用于管理 API 变更）"""
        return self._data

    def _resolve_path(self, config_path: str | Path | None) -> Path | None:
        if config_path:
            return Path(config_path)
        for p in DEFAULT_CONFIG_PATHS:
            if p.is_file():
                return p
        return DEFAULT_CONFIG_PATHS[0]

    def _inject_env(self) -> None:
        """将环境变量中的 API Key 注入 providers 配置"""
        providers = self._data.setdefault("providers", {})
        self._inject_provider_env(providers)
        web_search = _deep_merge(copy.deepcopy(DEFAULT_WEB_SEARCH), self._data.get("web_search", {}))
        self._data["web_search"] = web_search
        self._data.setdefault("native_models", copy.deepcopy(DEFAULT_NATIVE_MODELS))
        self._data["access_control"] = _deep_merge(
            copy.deepcopy(DEFAULT_ACCESS_CONTROL),
            self._data.get("access_control", {}),
        )
        self._data["access_control"].pop("keys", None)
        server = self._data.setdefault("server", {})
        server["native_auth_injection"] = _deep_merge(
            copy.deepcopy(DEFAULT_NATIVE_AUTH_INJECTION),
            server.get("native_auth_injection", {}),
        )
        server["native_auth_injection"].pop("client_token", None)
        server["native_auth_injection"].pop("client_token_env", None)
        self._inject_provider_env(web_search.get("providers", {}))
        self._normalize_mapping()

    @staticmethod
    def _inject_provider_env(providers: dict) -> None:
        for info in providers.values():
            env_var = info.get("api_key_env", "")
            if env_var:
                env_val = os.environ.get(env_var, "")
                if env_val:
                    info["api_key"] = env_val
                    info["_api_key_from_env"] = True
                elif "api_key" not in info:
                    info["api_key"] = ""
            elif "api_key" not in info:
                info["api_key"] = ""

    @staticmethod
    def _redact_injected_keys_for_save(providers: dict) -> None:
        for info in providers.values():
            if info.pop("_api_key_from_env", False):
                info.pop("api_key", None)

    @staticmethod
    def _redact_all_provider_keys_for_backup(providers: dict) -> None:
        """Backups retain routing settings but never persist live credentials."""
        for info in providers.values():
            if isinstance(info, dict):
                info.pop("api_key", None)
                info.pop("_api_key_from_env", None)

    @staticmethod
    def _redact_native_auth_for_backup(data: dict) -> None:
        server = data.get("server", {}) if isinstance(data, dict) else {}
        settings = server.get("native_auth_injection", {}) if isinstance(server, dict) else {}
        if isinstance(settings, dict):
            settings.pop("client_token", None)
            settings.pop("client_token_env", None)
        access_control = data.get("access_control", {}) if isinstance(data, dict) else {}
        if isinstance(access_control, dict):
            access_control.pop("keys", None)

    def _normalize_mapping(self) -> None:
        """将旧格式 model_mapping ({alias: target_string}) 迁移到新格式 ({alias: {target, ...}})"""
        mapping = self._data.get("model_mapping", {})
        normalized = {}
        for alias, entry in mapping.items():
            if isinstance(entry, str):
                normalized[alias] = {
                    "target": entry,
                    "provider": "",
                    "enabled": True,
                    "is_multimodal": False,
                    "vision_alias": None,
                    "is_image_gen": False,
                    "image_gen_alias": None,
                    "is_video_gen": False,
                    "video_gen_alias": None,
                    "is_reasoning_text": False,
                }
            elif isinstance(entry, dict):
                entry.setdefault("display_name", alias)
                entry.setdefault("route_kind", "custom")
                entry.setdefault("provider", "")
                entry.setdefault("is_multimodal", False)
                entry.setdefault("vision_alias", None)
                entry.setdefault("is_image_gen", False)
                entry.setdefault("image_gen_alias", None)
                entry.setdefault("is_video_gen", False)
                entry.setdefault("video_gen_alias", None)
                entry.setdefault("is_reasoning_text", False)
                entry.setdefault("use_proxy", False)
                entry.setdefault("proxy_url", "")
                entry.setdefault("enabled", True)
                normalized[alias] = entry
        self._data["model_mapping"] = normalized

    # ── 属性访问 ─────────────────────────────────────────────────

    @property
    def server_host(self) -> str:
        return self._runtime_server_host or self._data.get("server", {}).get("host", "127.0.0.1")

    @property
    def server_port(self) -> int:
        if self._runtime_server_port is not None:
            return self._runtime_server_port
        return self._data.get("server", {}).get("port", 8765)

    @property
    def vision_routing(self) -> dict:
        return self._data.get("vision_routing", {})

    @property
    def providers(self) -> dict:
        return self._data.get("providers", {})

    @property
    def web_search(self) -> dict:
        return self._data.get("web_search", {})

    @property
    def model_mapping(self) -> dict[str, dict]:
        """模型映射: {alias: {target, provider, is_multimodal, vision_alias}}"""
        return self._data.get("model_mapping", {})

    @property
    def native_models(self) -> dict[str, dict]:
        """Native ChatGPT Codex models displayed alongside custom models."""
        return self._data.get("native_models", {})

    # === Model Slots ===
    MODEL_SLOTS_CONFIG: dict[str, dict] = {
        "text":           {"alias": "gpt-5.5",           "is_multimodal": False, "is_image_gen": False, "is_video_gen": False, "is_reasoning_text": False},
        "reasoning_text": {"alias": "gpt-5.5-reasoning", "is_multimodal": False, "is_image_gen": False, "is_video_gen": False, "is_reasoning_text": True},
        "responses":      {"alias": "deepseek-v4-pro-responses", "wire_api": "responses", "is_multimodal": False, "is_image_gen": False, "is_video_gen": False, "is_reasoning_text": True},
        "vision":         {"alias": "gpt-5",             "is_multimodal": True,  "is_image_gen": False, "is_video_gen": False, "is_reasoning_text": False},
        "image_gen":      {"alias": "dall-e-3",          "is_multimodal": False, "is_image_gen": True,  "is_video_gen": False, "is_reasoning_text": False},
        "video_gen":      {"alias": "gpt-video",         "is_multimodal": False, "is_image_gen": False, "is_video_gen": True, "is_reasoning_text": False},
    }

    @property
    def model_slots(self) -> dict[str, dict]:
        return self._data.get("model_slots", {})

    def slot_alias(self, slot_id: str) -> str:
        entry = self.model_slots.get(slot_id, {})
        alias = entry.get("alias", "") if isinstance(entry, dict) else ""
        if str(alias).strip():
            return str(alias).strip()
        if isinstance(entry, dict):
            matching = self._matching_slot_aliases(slot_id, entry)
            if matching:
                return matching[0]
        return self.MODEL_SLOTS_CONFIG[slot_id]["alias"]

    def _matching_slot_aliases(self, slot_id: str, entry: dict) -> list[str]:
        matching = []
        for alias, mapped in self.model_mapping.items():
            if not isinstance(mapped, dict):
                continue
            if mapped.get("target", "") != entry.get("target", "") or mapped.get("provider", "") != entry.get("provider", ""):
                continue
            flags = ("is_multimodal", "is_image_gen", "is_video_gen")
            same_kind = (
                (slot_id == "text" and not any(mapped.get(flag) for flag in flags) and not mapped.get("is_reasoning_text") and mapped.get("wire_api", "chat") != "responses")
                or (slot_id == "reasoning_text" and mapped.get("is_reasoning_text") and mapped.get("wire_api", "chat") != "responses")
                or (slot_id == "responses" and mapped.get("wire_api") == "responses")
                or (slot_id == "vision" and mapped.get("is_multimodal"))
                or (slot_id == "image_gen" and mapped.get("is_image_gen"))
                or (slot_id == "video_gen" and mapped.get("is_video_gen"))
            )
            if same_kind:
                matching.append(alias)
        canonical = self.MODEL_SLOTS_CONFIG[slot_id]["alias"]
        matching.sort(key=lambda alias: (alias == canonical, alias))
        return matching

    def sync_slots_to_mapping(self) -> None:
        """Migrate legacy inline slots without making slots own model records."""
        slots = self.model_slots
        if not slots:
            return
        mapping = self._data.setdefault("model_mapping", {})
        for slot_id, slot_config in self.MODEL_SLOTS_CONFIG.items():
            entry = slots.get(slot_id)
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            alias = self.slot_alias(slot_id)
            entry["alias"] = alias
            if alias in mapping:
                continue
            mapping[alias] = {
                "display_name": alias,
                "target": entry.get("target", ""),
                "provider": entry.get("provider", ""),
                "route_kind": "custom",
                "wire_api": slot_config.get("wire_api", "chat"),
                "enabled": entry.get("enabled", True),
                "is_multimodal": slot_config["is_multimodal"],
                "vision_alias": None,
                "is_image_gen": slot_config["is_image_gen"],
                "image_gen_alias": None,
                "is_video_gen": slot_config["is_video_gen"],
                "video_gen_alias": None,
                "is_reasoning_text": slot_config["is_reasoning_text"],
                "use_proxy": False,
                "proxy_url": "",
            }
        self._data["model_mapping"] = mapping

    def get_provider(self, name: str) -> dict | None:
        return self.providers.get(name)

    def get_web_search_provider(self, name: str | None = None) -> dict | None:
        provider_name = name or self.web_search.get("active_provider", "bocha")
        return self.web_search.get("providers", {}).get(provider_name)

    def _has_api_key(self, provider: dict) -> bool:
        """检查 provider 是否有可用的 API key"""
        if provider.get("api_key", ""):
            return True
        env_var = provider.get("api_key_env", "")
        if env_var and os.environ.get(env_var, ""):
            return True
        return False

    def _enabled_providers(self) -> dict:
        """返回所有启用且有 API key 的 provider"""
        return {k: v for k, v in self.providers.items()
                if v.get("enabled", True) and self._has_api_key(v)}

    def resolve_slot_model(self, model_name: str) -> tuple[str, str] | None:
        """Resolve current and legacy slot aliases without crossing providers."""
        requested = str(model_name or "").strip()
        for slot_id, slot_config in self.MODEL_SLOTS_CONFIG.items():
            entry = self.model_slots.get(slot_id)
            if not isinstance(entry, dict):
                continue

            aliases = {
                str(slot_config.get("alias") or "").strip(),
                self.slot_alias(slot_id),
            }
            if requested not in aliases:
                continue
            if not entry.get("enabled", True):
                return "unknown", requested

            target = str(entry.get("target") or "").strip()
            provider_name = str(entry.get("provider") or "").strip()
            if not provider_name and target:
                provider_name = self._find_provider_for_target(target) or ""
            provider = self.providers.get(provider_name)
            if (
                target
                and isinstance(provider, dict)
                and provider.get("enabled", True)
                and self._has_api_key(provider)
            ):
                return provider_name, target
            return "unknown", requested
        return None

    def resolve_model(self, model_name: str) -> tuple[str, str]:
        """
        解析 code 模型名 → (provider_name, target_model)

        返回: (provider_name, target_model)
        例如: resolve_model("gpt-5-code") → ("qwen", "qwen-plus")
        """
        # 0. 槽位配置优先，允许 UI 中配置的标准模型别名立即生效。
        slot_route = self.resolve_slot_model(model_name)
        if slot_route is not None:
            return slot_route

        # 1. 先查 model_mapping 精确映射（仅启用的条目）
        #    模型明确指定了 provider 时，只检查该 provider 有无 API key，
        #    不检查 provider 级别的 enabled 标志（那个由模型级别 enabled 控制）
        entry = self.model_mapping.get(model_name)
        if isinstance(entry, dict) and entry.get("enabled", True):
            target = entry.get("target", model_name)
            provider_name = entry.get("provider", "")
            if not provider_name:
                provider_name = self._find_provider_for_target(target)
            if provider_name and provider_name in self.providers:
                p = self.providers[provider_name]
                if self._has_api_key(p):
                    return provider_name, target

        # Unknown aliases must not fall through to an unrelated provider.
        return "unknown", model_name

    def _find_provider_for_target(self, target: str) -> str | None:
        """根据 target 名查找对应的 provider（仅查找有 API key 的）"""
        for pname, pinfo in self.providers.items():
            if not pinfo.get("enabled", True) or not self._has_api_key(pinfo):
                continue
            if pinfo.get("adapter") == target or pname == target:
                return pname
        for pname, pinfo in self.providers.items():
            if not pinfo.get("enabled", True) or not self._has_api_key(pinfo):
                continue
            if pname in target.lower():
                return pname
        return None

    # ── 生成默认配置 ─────────────────────────────────────────────

    @staticmethod
    def generate_default(output_path: Path) -> None:
        """生成默认配置文件"""
        default = {
            "server": {
                "host": "127.0.0.1",
                "port": 8765,
                "launch_at_login": False,
                "codex_official_proxy_url": "",
                "native_codex_base_url": "https://chatgpt.com/backend-api/codex",
                "native_stream_timeout": 600,
                "native_auth_injection": copy.deepcopy(DEFAULT_NATIVE_AUTH_INJECTION),
            },
            "access_control": copy.deepcopy(DEFAULT_ACCESS_CONTROL),
            "native_models": copy.deepcopy(DEFAULT_NATIVE_MODELS),
            "providers": {
                "qwen": {
                    "adapter": "qwen",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key_env": "QWEN_API_KEY",
                },
                "deepseek": {
                    "adapter": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key_env": "DEEPSEEK_API_KEY",
                },
                "kimi": {
                    "adapter": "kimi",
                    "base_url": "https://api.moonshot.cn/v1",
                    "api_key_env": "KIMI_API_KEY",
                },
            },
            "model_mapping": {
                "gpt-5-code": {"target": "qwen-plus", "provider": "qwen", "is_multimodal": False, "vision_alias": None},
                "gpt-5-code-light": {"target": "qwen-turbo", "provider": "qwen", "is_multimodal": False, "vision_alias": None},
                "gpt-5": {"target": "qwen-plus", "provider": "qwen", "is_multimodal": False, "vision_alias": None},
            },
            "web_search": copy.deepcopy(DEFAULT_WEB_SEARCH),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml.dump(default, allow_unicode=True, default_flow_style=False), encoding="utf-8")


# 全局单例
_config_instance: Config | None = None


def get_config(config_path: str | Path | None = None) -> Config:
    global _config_instance
    if _config_instance is None:
        _config_instance = Config(config_path)
    return _config_instance


def reload_config() -> Config:
    cfg = get_config()
    cfg.reload()
    return cfg


def reload_config_if_changed() -> Config | None:
    cfg = get_config()
    return cfg if cfg.reload_if_changed() else None


def get_bridge_root_dir() -> Path:
    """Return the bridge installation root directory.

    In a PyInstaller bundle (sys.frozen), the exe lives in
    resources/backend/lan-bridge.exe -> root is two levels up.
    In source mode, use the project root (parent of the package).
    """
    import sys

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        # resources/backend/ -> root
        root = exe_dir.parent.parent
        if (root / "codex-model-catalog-tool-compatible.json").exists():
            return root
        # fallback: just two levels up
        return exe_dir.parent.parent

    # Source mode: up two levels from this file
    root = Path(__file__).resolve().parent.parent
    # Also check parent of project root (for bridge-test-app layout)
    if not (root / "codex-model-catalog-tool-compatible.json").exists():
        parent_root = root.parent
        if (parent_root / "codex-model-catalog-tool-compatible.json").exists():
            return parent_root
    return root


def get_model_catalog_path() -> Path | None:
    """Return the generated Codex catalog, with legacy bundle fallback."""
    generated = Path.home() / ".codex" / "lan-bridge" / "merged-models.json"
    if generated.exists():
        return generated
    root = get_bridge_root_dir()
    candidate = root / "codex-model-catalog-tool-compatible.json"
    return candidate if candidate.exists() else None


def get_model_catalog_target_path() -> Path:
    """Return the stable per-user path used for the generated Codex catalog."""
    return Path.home() / ".codex" / "lan-bridge" / "merged-models.json"
