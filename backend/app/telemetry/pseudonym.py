"""Server-side HMAC pseudonymization for telemetry identities."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from app.telemetry.contracts import (
    KEY_VERSION_PATTERN,
    PseudonymousIdentity,
    TelemetryEvent,
    TelemetryObservation,
)

SubjectKind = Literal["tenant", "user", "device"]
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_BYTES = 4096


def _normalize_key_ring(keys: Mapping[str, bytes]) -> dict[str, bytes]:
    normalized: dict[str, bytes] = {}
    owners: dict[bytes, str] = {}
    for version, secret in keys.items():
        if not isinstance(version, str) or not re.fullmatch(KEY_VERSION_PATTERN, version):
            raise ValueError("invalid telemetry key version")
        if not isinstance(secret, bytes):
            raise ValueError("telemetry key material must be bytes")
        if not MIN_HMAC_KEY_BYTES <= len(secret) <= MAX_HMAC_KEY_BYTES:
            raise ValueError("telemetry key material has invalid strength")
        previous_version = owners.get(secret)
        if previous_version is not None and previous_version != version:
            raise ValueError("telemetry key material cannot be reused across versions")
        normalized[version] = secret
        owners[secret] = version
    return normalized


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


class HmacPseudonymizer:
    """Convert identity material into epoch-bound, domain-separated digests."""

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        current_key_version: str,
        rotation_period_s: int,
    ) -> None:
        if rotation_period_s <= 0:
            raise ValueError("rotation_period_s must be positive")
        if not re.fullmatch(KEY_VERSION_PATTERN, current_key_version):
            raise ValueError("invalid current key version")
        normalized = _normalize_key_ring(keys)
        if current_key_version not in normalized or not normalized[current_key_version]:
            raise ValueError("current key version must have a non-empty secret")
        self._keys = normalized
        self._current_key_version = current_key_version
        self._rotation_period_s = rotation_period_s

    @property
    def current_key_version(self) -> str:
        return self._current_key_version

    @property
    def rotation_period_s(self) -> int:
        return self._rotation_period_s

    def _resolve_key_version(self, key_version: str | None) -> str:
        if key_version is None:
            return self._current_key_version
        if not isinstance(key_version, str) or not key_version.strip():
            raise ValueError("invalid telemetry key version")
        if not re.fullmatch(KEY_VERSION_PATTERN, key_version):
            raise ValueError("invalid telemetry key version")
        if key_version not in self._keys:
            raise ValueError("unknown telemetry key version")
        return key_version

    def epoch_for(self, occurred_at: datetime) -> int:
        epoch = int(_as_utc(occurred_at).timestamp()) // self._rotation_period_s
        if epoch < 0:
            raise ValueError("telemetry epoch cannot be negative")
        return epoch

    def pseudonymize(
        self,
        value: str,
        subject_kind: SubjectKind,
        *,
        occurred_at: datetime,
        key_version: str | None = None,
    ) -> str:
        if not value.strip():
            raise ValueError("identity value cannot be empty")
        version = self._resolve_key_version(key_version)
        secret = self._keys[version]
        epoch = self.epoch_for(occurred_at)
        message = (
            f"echodesk-telemetry-pseudonym-v1\0{version}\0{subject_kind}\0{epoch}\0{value}"
        ).encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    def _materialized_event_id(
        self,
        caller_event_id: str,
        identity: PseudonymousIdentity,
        *,
        key_version: str | None,
    ) -> str:
        version = self._resolve_key_version(key_version)
        secret = self._keys[version]
        message = (
            "echodesk-telemetry-event-id-v1\0"
            f"{version}\0{identity.epoch}\0"
            f"{identity.tenant_pseudonym}\0{identity.user_pseudonym}\0"
            f"{identity.device_pseudonym}\0{caller_event_id}"
        ).encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    def materialize(
        self,
        observation: TelemetryObservation,
        *,
        key_version: str | None = None,
    ) -> TelemetryEvent:
        identity = observation.identity
        version = self._resolve_key_version(key_version)
        epoch = self.epoch_for(observation.occurred_at)
        pseudonymous = PseudonymousIdentity(
            tenant_pseudonym=self.pseudonymize(
                identity.tenant_id,
                "tenant",
                occurred_at=observation.occurred_at,
                key_version=version,
            ),
            user_pseudonym=self.pseudonymize(
                identity.user_id,
                "user",
                occurred_at=observation.occurred_at,
                key_version=version,
            ),
            device_pseudonym=self.pseudonymize(
                identity.device_id,
                "device",
                occurred_at=observation.occurred_at,
                key_version=version,
            ),
            key_version=version,
            epoch=epoch,
        )
        return TelemetryEvent(
            event_id=self._materialized_event_id(
                observation.event_id,
                pseudonymous,
                key_version=version,
            ),
            occurred_at=observation.occurred_at,
            identity=pseudonymous,
            operation=observation.operation,
            platform=observation.platform,
            app_version=observation.app_version,
            provider=observation.provider,
            success=observation.success,
            failure_reason=observation.failure_reason,
            end_to_end_latency_ms=observation.end_to_end_latency_ms,
            queue_wait_ms=observation.queue_wait_ms,
            audio_duration_ms=observation.audio_duration_ms,
        )

    def rotate(self, *, key_version: str, secret: bytes) -> HmacPseudonymizer:
        keys = dict(self._keys)
        keys[key_version] = secret
        return HmacPseudonymizer(
            keys,
            current_key_version=key_version,
            rotation_period_s=self._rotation_period_s,
        )


__all__ = [
    "MAX_HMAC_KEY_BYTES",
    "MIN_HMAC_KEY_BYTES",
    "HmacPseudonymizer",
    "SubjectKind",
]
