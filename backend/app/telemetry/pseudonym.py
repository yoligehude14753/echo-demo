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
        normalized = {version: bytes(secret) for version, secret in keys.items()}
        if current_key_version not in normalized or not normalized[current_key_version]:
            raise ValueError("current key version must have a non-empty secret")
        if any(not re.fullmatch(KEY_VERSION_PATTERN, version) for version in normalized):
            raise ValueError("invalid key version")
        self._keys = normalized
        self._current_key_version = current_key_version
        self._rotation_period_s = rotation_period_s

    @property
    def current_key_version(self) -> str:
        return self._current_key_version

    @property
    def rotation_period_s(self) -> int:
        return self._rotation_period_s

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
        version = key_version or self._current_key_version
        secret = self._keys.get(version)
        if secret is None:
            raise ValueError("unknown telemetry key version")
        epoch = self.epoch_for(occurred_at)
        message = f"echodesk-telemetry-v1\0{subject_kind}\0{epoch}\0{value}".encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    def materialize(self, observation: TelemetryObservation) -> TelemetryEvent:
        identity = observation.identity
        epoch = self.epoch_for(observation.occurred_at)
        pseudonymous = PseudonymousIdentity(
            tenant_pseudonym=self.pseudonymize(
                identity.tenant_id,
                "tenant",
                occurred_at=observation.occurred_at,
            ),
            user_pseudonym=self.pseudonymize(
                identity.user_id,
                "user",
                occurred_at=observation.occurred_at,
            ),
            device_pseudonym=self.pseudonymize(
                identity.device_id,
                "device",
                occurred_at=observation.occurred_at,
            ),
            key_version=self._current_key_version,
            epoch=epoch,
        )
        return TelemetryEvent(
            event_id=observation.event_id,
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
        if not secret:
            raise ValueError("rotated telemetry secret cannot be empty")
        keys = dict(self._keys)
        keys[key_version] = bytes(secret)
        return HmacPseudonymizer(
            keys,
            current_key_version=key_version,
            rotation_period_s=self._rotation_period_s,
        )


__all__ = ["HmacPseudonymizer", "SubjectKind"]
