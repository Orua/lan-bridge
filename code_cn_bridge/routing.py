"""Explicit model routing for native Codex and custom providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RouteKind = Literal["native_codex", "custom"]


@dataclass(frozen=True)
class Route:
    kind: RouteKind
    requested_model: str
    target_model: str
    provider: str = ""
    adapter: str = ""
    auth_mode: Literal["host_login", "provider_key"] = "provider_key"
    metadata: dict[str, Any] = field(default_factory=dict)


def _native_models(config) -> dict[str, dict[str, Any]]:
    configured = getattr(config, "native_models", {})
    if isinstance(configured, list):
        return {
            str(item): {"display_name": str(item)}
            for item in configured
            if str(item).strip()
        }
    if isinstance(configured, dict):
        return {
            str(slug): (dict(value) if isinstance(value, dict) else {})
            for slug, value in configured.items()
            if str(slug).strip()
            and (not isinstance(value, dict) or value.get("enabled", True))
        }
    return {}


def _is_native_mapping(entry: dict[str, Any]) -> bool:
    return (
        entry.get("route_kind") == "native_codex"
        or entry.get("kind") == "native_codex"
        or entry.get("provider") == "native_codex"
        or entry.get("adapter") == "native_codex"
    )


def resolve_route(config, model: str) -> Route:
    """Resolve a request without conflating native Codex with OpenAI-compatible APIs."""
    requested = str(model or "").strip()
    slot_resolver = getattr(config, "resolve_slot_model", None)
    slot_route = slot_resolver(requested) if callable(slot_resolver) else None
    if slot_route is not None:
        provider, target = slot_route
        provider_info = getattr(config, "providers", {}).get(provider, {})
        metadata = {"slot_alias": True}
        mapped = getattr(config, "model_mapping", {}).get(requested)
        if isinstance(mapped, dict):
            metadata.update(mapped)
            metadata["slot_alias"] = True
        return Route(
            kind="custom",
            requested_model=requested,
            target_model=target,
            provider=provider,
            adapter=str(provider_info.get("adapter") or "openai"),
            auth_mode="provider_key",
            metadata=metadata,
        )

    entry = getattr(config, "model_mapping", {}).get(requested)
    if isinstance(entry, dict) and entry.get("enabled", True):
        target = str(entry.get("target") or requested)
        if _is_native_mapping(entry):
            return Route(
                kind="native_codex",
                requested_model=requested,
                target_model=target,
                provider="native_codex",
                adapter="native_codex",
                auth_mode="host_login",
                metadata=dict(entry),
            )

        provider = str(entry.get("provider") or "")
        if not provider:
            provider, target = config.resolve_model(requested)
        provider_info = getattr(config, "providers", {}).get(provider, {})
        return Route(
            kind="custom",
            requested_model=requested,
            target_model=target,
            provider=provider,
            adapter=str(provider_info.get("adapter") or entry.get("adapter") or "openai"),
            auth_mode="provider_key",
            metadata=dict(entry),
        )

    native = _native_models(config)
    if requested in native:
        metadata = native[requested]
        return Route(
            kind="native_codex",
            requested_model=requested,
            target_model=str(metadata.get("target") or requested),
            provider="native_codex",
            adapter="native_codex",
            auth_mode="host_login",
            metadata=metadata,
        )

    provider, target = config.resolve_model(requested)
    provider_info = getattr(config, "providers", {}).get(provider, {})
    return Route(
        kind="custom",
        requested_model=requested,
        target_model=target,
        provider=provider,
        adapter=str(provider_info.get("adapter") or "openai"),
        auth_mode="provider_key",
        metadata=dict(entry) if isinstance(entry, dict) else {},
    )
