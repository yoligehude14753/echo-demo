"""ModelRuntimeConfig -> ModelRuntimeSnapshot 的纯编译边界。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from app.model_runtime.errors import (
    MODEL_AUTH_INVALID,
    MODEL_AUTH_MISSING,
    MODEL_CAPABILITY_MISSING,
    MODEL_CONFIG_HASH_MISMATCH,
    MODEL_CONFIG_INVALID,
    MODEL_CONFIG_STALE_REVISION,
    MODEL_ENDPOINT_CONFLICT,
    MODEL_ENDPOINT_INVALID,
    MODEL_ENDPOINT_MISSING,
    MODEL_FALLBACK_INVALID,
    MODEL_MODEL_CONFLICT,
    MODEL_MODEL_MISSING,
    MODEL_ROUTE_CONFLICT,
    ModelRuntimeConfigError,
    ModelRuntimeStaleRevisionError,
)
from app.model_runtime.snapshot import ModelRuntimeSnapshot
from app.model_runtime.types import (
    MODEL_RUNTIME_SCHEMA_VERSION,
    ModelPurpose,
    ModelRoute,
    ModelRuntimeConfig,
)

_MISSING: Final = object()
_PURPOSES: Final = frozenset(
    {
        "agent_main",
        "agent_compact",
        "agent_summary",
        "agent_quality",
        "chat",
        "minutes",
        "memory",
    }
)
_CAPABILITY_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "streaming": ("streaming",),
    "tool_use": ("tool_use", "toolUse"),
    "parallel_tool_use": ("parallel_tool_use", "parallelToolUse"),
    "tool_choice": ("tool_choice", "toolChoice"),
    "system_messages": ("system_messages", "systemMessages"),
    "usage_in_stream": ("usage_in_stream", "usageInStream"),
    "prompt_cache": ("prompt_cache", "promptCache"),
    "multimodal_images": ("multimodal_images", "multimodalImages"),
    "multimodal_documents": ("multimodal_documents", "multimodalDocuments"),
}
_OPAQUE_HANDLE_RE: Final = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:(?://)?|(?:cred|handle)_)[A-Za-z0-9._~:/-]{2,120}$"
)


def _config_error(code: str, *, field: str | None = None) -> ModelRuntimeConfigError:
    return ModelRuntimeConfigError(code, field=field)


def _read_aliases(
    value: Mapping[str, object],
    names: tuple[str, ...],
    *,
    conflict_code: str,
    field: str,
) -> object:
    present = [(name, value[name]) for name in names if name in value]
    if not present:
        return _MISSING
    first = present[0][1]
    if any(candidate != first for _, candidate in present[1:]):
        raise _config_error(conflict_code, field=field)
    return first


def _preflight_raw_config(raw: Mapping[str, object]) -> None:
    routes = raw.get("routes", _MISSING)
    if not isinstance(routes, Mapping) or not routes:
        raise _config_error(MODEL_CONFIG_INVALID, field="routes")

    for purpose, raw_route in routes.items():
        if purpose not in _PURPOSES:
            raise _config_error(MODEL_CONFIG_INVALID, field="purpose")
        if not isinstance(raw_route, Mapping):
            raise _config_error(MODEL_CONFIG_INVALID, field="route")

        endpoint = _read_aliases(
            raw_route,
            ("base_url", "baseUrl", "endpoint"),
            conflict_code=MODEL_ENDPOINT_CONFLICT,
            field="endpoint",
        )
        if endpoint is _MISSING or not isinstance(endpoint, str) or not endpoint.strip():
            raise _config_error(MODEL_ENDPOINT_MISSING, field="endpoint")

        model = _read_aliases(
            raw_route,
            ("model",),
            conflict_code=MODEL_MODEL_CONFLICT,
            field="model",
        )
        if model is _MISSING or not isinstance(model, str) or not model.strip():
            raise _config_error(MODEL_MODEL_MISSING, field="model")

        credential = _read_aliases(
            raw_route,
            ("credential_handle", "credentialHandle"),
            conflict_code=MODEL_AUTH_INVALID,
            field="credential_handle",
        )
        if credential is _MISSING or not isinstance(credential, str) or not credential.strip():
            raise _config_error(MODEL_AUTH_MISSING, field="credential_handle")
        if (
            not _OPAQUE_HANDLE_RE.fullmatch(credential.strip())
            or credential.strip().lower().startswith(("http:", "https:"))
        ):
            raise _config_error(MODEL_AUTH_INVALID, field="credential_handle")

        auth_mode = _read_aliases(
            raw_route,
            ("auth_mode", "authMode"),
            conflict_code=MODEL_AUTH_INVALID,
            field="auth_mode",
        )
        if auth_mode is not _MISSING and auth_mode not in (None, "credential_handle"):
            raise _config_error(MODEL_AUTH_INVALID, field="auth_mode")

        capabilities = raw_route.get("capabilities", _MISSING)
        if not isinstance(capabilities, Mapping):
            raise _config_error(MODEL_CAPABILITY_MISSING, field="capabilities")
        for capability, aliases in _CAPABILITY_ALIASES.items():
            if _read_aliases(
                capabilities,
                aliases,
                conflict_code="MODEL_CAPABILITY_CONFLICT",
                field=capability,
            ) is _MISSING:
                raise _config_error(MODEL_CAPABILITY_MISSING, field=capability)


def _canonicalize_raw_aliases(raw: Mapping[str, object]) -> dict[str, object]:
    """把已通过冲突检查的输入别名压成一个 Pydantic canonical key。"""

    canonical = dict(raw)
    raw_routes = raw["routes"]
    assert isinstance(raw_routes, Mapping)
    canonical_routes: dict[object, object] = {}
    route_aliases = {
        "route_id": ("route_id", "routeId"),
        "base_url": ("base_url", "baseUrl", "endpoint"),
        "credential_handle": ("credential_handle", "credentialHandle"),
        "fallback_route_ids": ("fallback_route_ids", "fallbackRouteIds"),
    }
    for purpose, raw_route in raw_routes.items():
        assert isinstance(raw_route, Mapping)
        route = dict(raw_route)
        for field, aliases in route_aliases.items():
            value = _read_aliases(
                raw_route,
                aliases,
                conflict_code=MODEL_CONFIG_INVALID,
                field=field,
            )
            if value is not _MISSING:
                for alias in aliases:
                    route.pop(alias, None)
                route[field] = value
        auth_mode = _read_aliases(
            raw_route,
            ("auth_mode", "authMode"),
            conflict_code=MODEL_CONFIG_INVALID,
            field="auth_mode",
        )
        if auth_mode is not _MISSING:
            route.pop("auth_mode", None)
            route.pop("authMode", None)

        raw_capabilities = raw_route["capabilities"]
        assert isinstance(raw_capabilities, Mapping)
        capabilities: dict[str, object] = {}
        for field, aliases in _CAPABILITY_ALIASES.items():
            value = _read_aliases(
                raw_capabilities,
                aliases,
                conflict_code="MODEL_CAPABILITY_CONFLICT",
                field=field,
            )
            assert value is not _MISSING
            capabilities[field] = value
        route["capabilities"] = capabilities
        canonical_routes[purpose] = route
    canonical["routes"] = canonical_routes
    return canonical


def _coerce_config(value: ModelRuntimeConfig | Mapping[str, object]) -> ModelRuntimeConfig:
    if isinstance(value, ModelRuntimeConfig):
        return value
    if not isinstance(value, Mapping):
        raise _config_error(MODEL_CONFIG_INVALID, field="config")
    _preflight_raw_config(value)
    try:
        return ModelRuntimeConfig.model_validate(_canonicalize_raw_aliases(value))
    except ValidationError as exc:
        # Pydantic's detailed path can contain provider input. The public
        # compiler deliberately replaces it with a stable, secret-free code.
        del exc
        raise _config_error(MODEL_CONFIG_INVALID, field="config") from None


def _normalize_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint.strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise _config_error(MODEL_ENDPOINT_INVALID, field="endpoint")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise _config_error(MODEL_ENDPOINT_INVALID, field="endpoint") from None
    if hostname is None:
        raise _config_error(MODEL_ENDPOINT_INVALID, field="endpoint")
    normalized_host = hostname.lower()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def _validate_routes(routes: Mapping[ModelPurpose, ModelRoute]) -> None:
    route_ids: dict[str, ModelPurpose] = {}
    for purpose, route in routes.items():
        previous = route_ids.get(route.route_id)
        if previous is not None and previous != purpose:
            raise _config_error(MODEL_ROUTE_CONFLICT, field="route_id")
        route_ids[route.route_id] = purpose

    for route in routes.values():
        if len(set(route.fallback_route_ids)) != len(route.fallback_route_ids):
            raise _config_error(MODEL_FALLBACK_INVALID, field="fallback_route_ids")
        for fallback_id in route.fallback_route_ids:
            if fallback_id not in route_ids or fallback_id == route.route_id:
                raise _config_error(MODEL_FALLBACK_INVALID, field="fallback_route_ids")

    def depth(route_id: str, path: tuple[str, ...]) -> int:
        if route_id in path:
            raise _config_error(MODEL_FALLBACK_INVALID, field="fallback_route_ids")
        route = next(item for item in routes.values() if item.route_id == route_id)
        if not route.fallback_route_ids:
            return 0
        current_path = (*path, route_id)
        maximum = max(depth(item, current_path) for item in route.fallback_route_ids)
        if maximum + 1 > 2:
            raise _config_error(MODEL_FALLBACK_INVALID, field="fallback_route_ids")
        return maximum + 1

    for route_id in route_ids:
        depth(route_id, ())


def _canonical_payload(config: ModelRuntimeConfig) -> dict[str, object]:
    routes: dict[str, object] = {}
    for purpose in sorted(config.routes):
        route = config.routes[purpose]
        routes[purpose] = {
            "route_id": route.route_id,
            "protocol": route.protocol,
            "base_url": _normalize_endpoint(route.base_url),
            "model": route.model,
            "fallback_route_ids": list(route.fallback_route_ids),
            "capabilities": route.capabilities.model_dump(mode="json"),
            "limits": route.limits.model_dump(mode="json"),
            "tokenizer": route.tokenizer.model_dump(mode="json"),
            "reasoning": route.reasoning.model_dump(mode="json"),
            # credential_handle is intentionally absent from the hash.
        }
    return {"schema_version": config.schema_version, "routes": routes}


def canonical_config_hash(config: ModelRuntimeConfig) -> str:
    """对规范化、非秘密配置求稳定 SHA-256。"""

    canonical = json.dumps(
        _canonical_payload(config),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def normalize_model_runtime_config(
    value: ModelRuntimeConfig | Mapping[str, object],
) -> ModelRuntimeConfig:
    """校验并返回唯一规范化配置；不产生 IO 或读取第二配置源。"""

    config = _coerce_config(value)
    normalized_routes = {
        purpose: route.model_copy(
            update={
                "base_url": _normalize_endpoint(route.base_url),
                "fallback_route_ids": tuple(route.fallback_route_ids),
            }
        )
        for purpose, route in config.routes.items()
    }
    normalized = config.model_copy(update={"routes": normalized_routes})
    _validate_routes(normalized.routes)
    expected_hash = canonical_config_hash(normalized)
    if normalized.config_hash is not None and normalized.config_hash != expected_hash:
        raise _config_error(MODEL_CONFIG_HASH_MISMATCH, field="config_hash")
    return normalized.model_copy(update={"config_hash": expected_hash})


def compile_model_runtime_config(
    value: ModelRuntimeConfig | Mapping[str, object],
) -> ModelRuntimeConfig:
    """公开的 config compiler 入口。"""

    return normalize_model_runtime_config(value)


def compile_snapshot(
    value: ModelRuntimeConfig | Mapping[str, object],
    purpose: ModelPurpose,
    *,
    expected_revision: int | None = None,
    config_revision: int | None = None,
) -> ModelRuntimeSnapshot:
    """从一个配置事实源选择一个 purpose，并生成不可变 snapshot。"""

    if (
        expected_revision is not None
        and config_revision is not None
        and expected_revision != config_revision
    ):
        raise ModelRuntimeStaleRevisionError(
            MODEL_CONFIG_STALE_REVISION,
            field="revision",
        )
    bound_revision = config_revision if config_revision is not None else expected_revision
    config = normalize_model_runtime_config(value)
    if bound_revision is not None and config.revision != bound_revision:
        raise ModelRuntimeStaleRevisionError(MODEL_CONFIG_STALE_REVISION, field="revision")

    if purpose not in _PURPOSES:
        raise _config_error(MODEL_CONFIG_INVALID, field="purpose")
    route = config.routes.get(purpose)
    if route is None and purpose in {"agent_compact", "agent_summary"}:
        route = config.routes["agent_main"]
    if route is None:
        raise _config_error(MODEL_CONFIG_INVALID, field="purpose")

    return ModelRuntimeSnapshot(
        schemaVersion=MODEL_RUNTIME_SCHEMA_VERSION,
        revision=config.revision,
        configHash=config.config_hash or canonical_config_hash(config),
        purpose=purpose,
        routeId=route.route_id,
        protocol=route.protocol,
        model=route.model,
        capabilities=route.capabilities,
        limits=route.limits,
        tokenizer=route.tokenizer,
        reasoning=route.reasoning,
        credentialHandle=route.credential_handle,
    )


def compile_route_snapshot(
    value: ModelRuntimeConfig | Mapping[str, object],
    purpose: ModelPurpose,
    route_id: str,
    *,
    expected_revision: int | None = None,
) -> ModelRuntimeSnapshot:
    """Compile a named route without changing the task's pinned revision.

    Fallback selection is allowed to change the route, but never to silently
    select a route from a newer config revision.  The caller supplies the
    expected revision captured when the task started.
    """

    config = normalize_model_runtime_config(value)
    if expected_revision is not None and config.revision != expected_revision:
        raise ModelRuntimeStaleRevisionError(MODEL_CONFIG_STALE_REVISION, field="revision")
    route = next((candidate for candidate in config.routes.values() if candidate.route_id == route_id), None)
    if route is None:
        raise _config_error(MODEL_CONFIG_INVALID, field="route_id")
    return ModelRuntimeSnapshot(
        schemaVersion=1,
        revision=config.revision,
        configHash=config.config_hash or canonical_config_hash(config),
        purpose=purpose,
        routeId=route.route_id,
        protocol=route.protocol,
        model=route.model,
        capabilities=route.capabilities,
        limits=route.limits,
        tokenizer=route.tokenizer,
        reasoning=route.reasoning,
        credentialHandle=route.credential_handle,
    )


compile_model_runtime_snapshot = compile_snapshot


__all__ = [
    "canonical_config_hash",
    "compile_model_runtime_config",
    "compile_model_runtime_snapshot",
    "compile_route_snapshot",
    "compile_snapshot",
    "normalize_model_runtime_config",
]
