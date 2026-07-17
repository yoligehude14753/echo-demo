"""The single persistent source for Echo model-runtime configuration."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from app.model_runtime.config import compile_model_runtime_config
from app.model_runtime.errors import (
    MODEL_CONFIG_NOT_FOUND,
    MODEL_CONFIG_REVISION_CONFLICT,
    MODEL_CONFIG_STORAGE_INVALID,
    ModelRuntimeConfigError,
)
from app.model_runtime.types import ModelRuntimeConfig

MODEL_RUNTIME_CONFIG_KEY = "model_runtime"
_MODEL_RUNTIME_ALIASES = ("model_runtime", "modelruntime")
_SCHEMA_VERSION = 1


def _default_config_path() -> Path:
    raw = os.environ.get("ECHO_USER_DIR", "~/.echodesk")
    return Path(raw).expanduser() / "config.json"


def _storage_error(field: str) -> ModelRuntimeConfigError:
    return ModelRuntimeConfigError(MODEL_CONFIG_STORAGE_INVALID, field=field)


def migrate_model_runtime_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the pre-v1 single-route payload into the v1 compiler shape.

    Migration is deliberately input-only: it does not inspect legacy Settings,
    environment variables, provider SDKs, or any external credential file.
    Missing legacy revision/activation metadata receives deterministic schema
    defaults and is immediately compiled and persisted in canonical form.
    """

    if not isinstance(value, Mapping):
        raise _storage_error("model_runtime")
    migrated = dict(value)

    if "schemaVersion" not in migrated and "schema_version" not in migrated:
        migrated["schemaVersion"] = _SCHEMA_VERSION
    if "revision" not in migrated:
        legacy_revision = migrated.pop("configRevision", migrated.pop("config_revision", None))
        migrated["revision"] = legacy_revision if legacy_revision is not None else 1
    if "activatedAt" not in migrated and "activated_at" not in migrated:
        migrated["activatedAt"] = datetime.now(UTC).isoformat()

    if "routes" not in migrated:
        legacy_route = migrated.pop("route", None)
        if not isinstance(legacy_route, Mapping):
            legacy_route = {
                key: migrated.pop(key)
                for key in (
                    "routeId",
                    "route_id",
                    "protocol",
                    "baseUrl",
                    "base_url",
                    "endpoint",
                    "credentialHandle",
                    "credential_handle",
                    "model",
                    "fallbackRouteIds",
                    "fallback_route_ids",
                    "capabilities",
                    "limits",
                    "tokenizer",
                    "reasoning",
                )
                if key in migrated
            }
        if not legacy_route:
            raise _storage_error("routes")
        migrated["routes"] = {"agent_main": dict(legacy_route)}

    return migrated


class ModelRuntimeConfigStore:
    """Atomic save/read path for the authoritative model-runtime config."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else _default_config_path()
        self._lock = RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _load_document(self) -> dict[str, Any]:
        if self._path == _default_config_path():
            from app.config_io import load_user_config_json

            return load_user_config_json()
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError):
            raise _storage_error("document") from None
        if not isinstance(value, dict):
            raise _storage_error("document")
        return {str(key).lower(): item for key, item in value.items()}

    def _write_document(self, document: Mapping[str, Any]) -> None:
        if self._path == _default_config_path():
            from app.config_io import write_user_config_json

            write_user_config_json(
                {MODEL_RUNTIME_CONFIG_KEY: dict(document[MODEL_RUNTIME_CONFIG_KEY])}
            )
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def _payload(self) -> Mapping[str, Any] | None:
        document = self._load_document()
        matches = [document[key] for key in _MODEL_RUNTIME_ALIASES if key in document]
        if not matches:
            return None
        if len(matches) > 1 and matches[0] != matches[1]:
            raise _storage_error("model_runtime")
        if not isinstance(matches[0], Mapping):
            raise _storage_error("model_runtime")
        return matches[0]

    def read(self) -> ModelRuntimeConfig:
        """Read, migrate, and compile the one authoritative config."""

        with self._lock:
            payload = self._payload()
            if payload is None:
                raise ModelRuntimeConfigError(MODEL_CONFIG_NOT_FOUND, field="model_runtime")
            try:
                return compile_model_runtime_config(migrate_model_runtime_config(payload))
            except ModelRuntimeConfigError:
                raise
            except Exception:
                raise _storage_error("model_runtime") from None

    def read_or_none(self) -> ModelRuntimeConfig | None:
        try:
            return self.read()
        except ModelRuntimeConfigError as error:
            if error.code == MODEL_CONFIG_NOT_FOUND:
                return None
            raise

    def save(
        self,
        value: ModelRuntimeConfig | Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> ModelRuntimeConfig:
        """Compile and atomically save a strictly newer config revision."""

        compiled = compile_model_runtime_config(
            migrate_model_runtime_config(value)
            if isinstance(value, Mapping)
            else value.model_dump()
        )
        with self._lock:
            current = self.read_or_none()
            if expected_revision is not None:
                actual = current.revision if current is not None else None
                if actual != expected_revision:
                    raise ModelRuntimeConfigError(
                        MODEL_CONFIG_REVISION_CONFLICT,
                        field="expected_revision",
                    )
            if current is not None and compiled.revision <= current.revision:
                raise ModelRuntimeConfigError(
                    MODEL_CONFIG_REVISION_CONFLICT,
                    field="revision",
                )
            payload = compiled.model_dump(mode="json", by_alias=True)
            document = self._load_document()
            document[MODEL_RUNTIME_CONFIG_KEY] = payload
            document.pop("modelruntime", None)
            self._write_document(document)
            return compiled


__all__ = [
    "MODEL_RUNTIME_CONFIG_KEY",
    "ModelRuntimeConfigStore",
    "migrate_model_runtime_config",
]
