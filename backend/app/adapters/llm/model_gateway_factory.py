"""EchoDesk's single credential, binding, and model-gateway call boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import stat
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Mapping

from yoli_llm.model_gateway import (
    GatewayRequestPolicy,
    GatewayTranscriptionChunk,
    ModelGatewayClient,
    ModelGatewayConfig,
    ModelGatewayError,
)

_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BINDING_CONTEXT = b"model-gateway-capability-binding-v3"
_BINDING_MAX_BYTES = 2 * 1024 * 1024
_BINDING_ROOT_FIELDS = frozenset({"schema_version", "generated_at", "refresh_after", "binding", "models"})
_BINDING_KEY_FIELDS = frozenset({"base_url", "credential_hmac_sha256", "catalog_model_ids"})
_BINDING_MODEL_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "capability",
        "aliases",
        "request",
        "limits",
        "defaults",
        "output",
        "stream_output",
        "retry",
        "sources",
    }
)
_BINDING_MODEL_OPTIONAL_FIELDS = frozenset({"modalities"})
logger = logging.getLogger("echodesk.model_gateway.binding")


def _secret(value: str | None) -> str:
    value = (value or "").strip()
    if not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise RuntimeError("invalid secret format")
    return value


def _read_private_file(path: Path) -> str:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            return _secret(handle.read())
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("invalid secret format") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _default_key_path(service_name: str) -> Path:
    if not _SERVICE_NAME_RE.fullmatch(service_name):
        raise RuntimeError("invalid secret format")
    return Path.home() / ".config" / "yoliyoli" / "model-gateway" / service_name / "api_key"


def _credential_file_path(*, service_name: str, configured_key: str | None, configured_file: str | None) -> Path | None:
    if os.environ.get("MODEL_GATEWAY_API_KEY") is not None or configured_key is not None:
        return None
    file_value = os.environ.get("MODEL_GATEWAY_API_KEY_FILE", configured_file)
    if file_value is not None:
        if not file_value:
            raise RuntimeError("invalid secret format")
        return Path(file_value).expanduser()
    return _default_key_path(service_name)


def _binding_location(key_file: Path | None) -> tuple[Path | None, bool]:
    explicit = os.environ.get("MODEL_GATEWAY_CAPABILITY_PROFILE_FILE")
    if explicit is not None:
        if not explicit:
            raise RuntimeError("capability binding unavailable")
        return Path(explicit).expanduser(), True
    if key_file is None:
        return None, False
    return key_file.with_name("capabilities.json"), True


def _read_binding(path: Path) -> dict[str, Any]:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            raw = handle.read(_BINDING_MAX_BYTES + 1)
        if len(raw.encode("utf-8")) > _BINDING_MAX_BYTES:
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("capability binding unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _merge_binding(
    catalog: Any,
    payload: Mapping[str, Any],
    config: ModelGatewayConfig,
    *,
    now: int | None = None,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(catalog, (tuple, list)) or any(not isinstance(item, Mapping) for item in catalog):
        raise RuntimeError("capability binding unavailable")
    catalog_ids = [item.get("id") for item in catalog]
    binding = payload.get("binding")
    models = payload.get("models")
    generated_at = payload.get("generated_at")
    refresh_after = payload.get("refresh_after")
    current_time = int(time.time()) if now is None else now
    expected_hmac = hmac.new(
        config.api_key.encode("utf-8"), _BINDING_CONTEXT, hashlib.sha256
    ).hexdigest()
    if (
        payload.get("schema_version") != 3
        or set(payload) != _BINDING_ROOT_FIELDS
        or not isinstance(binding, Mapping)
        or set(binding) != _BINDING_KEY_FIELDS
        or binding.get("base_url") != config.base_url
        or binding.get("catalog_model_ids") != catalog_ids
        or not isinstance(binding.get("credential_hmac_sha256"), str)
        or not hmac.compare_digest(binding["credential_hmac_sha256"], expected_hmac)
        or not isinstance(generated_at, int)
        or isinstance(generated_at, bool)
        or generated_at > current_time + 300
        or not isinstance(refresh_after, int)
        or isinstance(refresh_after, bool)
        or refresh_after <= generated_at
        or any(not isinstance(model_id, str) or not model_id for model_id in catalog_ids)
        or len(set(catalog_ids)) != len(catalog_ids)
        or not isinstance(models, list)
        or len(models) != len(catalog_ids)
    ):
        raise RuntimeError("capability binding unavailable")
    by_id: dict[str, dict[str, Any]] = {}
    for item in models:
        if not isinstance(item, dict):
            raise RuntimeError("capability binding unavailable")
        fields = set(item)
        if (
            not _BINDING_MODEL_REQUIRED_FIELDS.issubset(fields)
            or not fields.issubset(
                _BINDING_MODEL_REQUIRED_FIELDS | _BINDING_MODEL_OPTIONAL_FIELDS
            )
        ):
            raise RuntimeError("capability binding unavailable")
        modalities = item.get("modalities")
        if modalities is not None and (
            not isinstance(modalities, list)
            or any(not isinstance(value, str) or not value for value in modalities)
            or len(set(modalities)) != len(modalities)
        ):
            raise RuntimeError("capability binding unavailable")
        model_id = item.get("id")
        if not isinstance(model_id, str) or model_id not in catalog_ids or model_id in by_id:
            raise RuntimeError("capability binding unavailable")
        by_id[model_id] = json.loads(json.dumps(item))
    if set(by_id) != set(catalog_ids):
        raise RuntimeError("capability binding unavailable")
    if refresh_after < current_time:
        logger.warning(
            "model gateway capability refresh overdue; binding remains authoritative profile=%s refresh_after=%d",
            "capabilities.json",
            refresh_after,
        )
    return tuple(by_id[str(model_id)] for model_id in catalog_ids)


def resolve_model_gateway_credential(
    *,
    base_url: str,
    service_name: str,
    configured_key: str | None = None,
    configured_file: str | None = None,
) -> str:
    """Resolve an injected key or owner-private key file without bootstrap side effects."""

    _ = base_url
    env_key = os.environ.get("MODEL_GATEWAY_API_KEY")
    if env_key is not None:
        return _secret(env_key)
    if configured_key is not None:
        return _secret(configured_key)
    file_value = os.environ.get("MODEL_GATEWAY_API_KEY_FILE", configured_file)
    if file_value is not None and not file_value:
        raise RuntimeError("invalid secret format")
    return _read_private_file(
        Path(file_value).expanduser() if file_value is not None else _default_key_path(service_name)
    )


class GatewayClient:
    """Typed EchoDesk façade; canonical values are passed exactly once."""

    def __init__(
        self,
        client: Any,
        config: ModelGatewayConfig,
        *,
        binding_file: Path | None,
        binding_required: bool,
    ) -> None:
        self.raw = client
        self.config = config
        self._binding_file = binding_file
        self._binding_required = binding_required

    async def aclose(self) -> None:
        await self.raw.aclose()

    async def discover_model_profiles(self, *, timeout_s: float | None = None) -> Any:
        catalog = await self.raw.discover_model_profiles(total_timeout=timeout_s)
        if self._binding_file is None:
            return catalog
        try:
            return _merge_binding(catalog, _read_binding(self._binding_file), self.config)
        except RuntimeError:
            if self._binding_required:
                raise ModelGatewayError("capability contract unavailable") from None
            return catalog

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        options: Mapping[str, Any],
        policy: GatewayRequestPolicy,
        timeout_s: float | None = None,
    ) -> Any:
        return await self.raw.chat(
            messages,
            max_tokens=max_tokens,
            stream=False,
            request_options=options,
            policy=policy,
            total_timeout=timeout_s,
        )

    async def iter_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        options: Mapping[str, Any],
        policy: GatewayRequestPolicy,
        timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.raw.iter_chat(
            messages,
            max_tokens=max_tokens,
            request_options=options,
            policy=policy,
            total_timeout=timeout_s,
        ):
            yield chunk

    async def iter_transcription(
        self,
        audio: bytes,
        *,
        options: Mapping[str, Any],
        policy: GatewayRequestPolicy,
        filename: str,
        mime: str,
        timeout_s: float | None = None,
    ) -> AsyncIterator[GatewayTranscriptionChunk]:
        async for chunk in self.raw.iter_transcription(
            audio,
            filename=filename,
            mime=mime,
            request_options=options,
            policy=policy,
            total_timeout=timeout_s,
        ):
            yield chunk

    async def synthesize_to_file(
        self,
        text: str,
        target: str | Path,
        *,
        voice: str,
        response_format: str,
        options: Mapping[str, Any],
        policy: GatewayRequestPolicy,
        timeout_s: float | None = None,
    ) -> Any:
        return await self.raw.synthesize_to_file(
            text,
            target,
            voice=voice,
            response_format=response_format,
            request_options=options,
            policy=policy,
            total_timeout=timeout_s,
        )


def create_model_gateway_client(
    settings: Any,
    supplied_client: Any | None = None,
    *,
    total_timeout: float | None = None,
) -> tuple[GatewayClient, ModelGatewayConfig]:
    key_file = _credential_file_path(
        service_name=settings.model_gateway_service_name,
        configured_key=settings.model_gateway_api_key,
        configured_file=settings.model_gateway_api_key_file,
    )
    key = resolve_model_gateway_credential(
        base_url=settings.model_gateway_base_url,
        service_name=settings.model_gateway_service_name,
        configured_key=settings.model_gateway_api_key,
        configured_file=settings.model_gateway_api_key_file,
    )
    config = ModelGatewayConfig(
        base_url=settings.model_gateway_base_url,
        api_key=key,
        service_name=settings.model_gateway_service_name,
    )
    raw = supplied_client or ModelGatewayClient(
        config,
        **({"total_timeout": total_timeout} if total_timeout is not None else {}),
    )
    binding_file, binding_required = _binding_location(key_file)
    return GatewayClient(
        raw,
        config,
        binding_file=binding_file,
        binding_required=binding_required,
    ), config


__all__ = ["GatewayClient", "create_model_gateway_client", "resolve_model_gateway_credential"]
