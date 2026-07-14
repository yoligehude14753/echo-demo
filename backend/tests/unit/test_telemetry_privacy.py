"""Local privacy-contract tests for the independent telemetry module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.telemetry import (
    DeletionReason,
    FailureReason,
    FailureReasonCount,
    HmacPseudonymizer,
    InMemoryAggregateTelemetry,
    NoopTelemetryAdapter,
    ProviderRegistry,
    TelemetryDeleteRequest,
    TelemetryIdentityInput,
    TelemetryObservation,
    TelemetryPlatform,
    TelemetryProvider,
    TelemetryQuery,
    TelemetryRuntimeConfig,
    build_test_telemetry_adapter,
)
from pydantic import ValidationError

FAKE_SECRET = b"fixed-test-secret"
ROTATED_SECRET = b"rotated-test-secret"
BASE_TIME = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def identity(suffix: str = "one") -> TelemetryIdentityInput:
    return TelemetryIdentityInput(
        tenant_id=f"tenant-sentinel-{suffix}",
        user_id=f"user-sentinel-{suffix}",
        device_id=f"device-sentinel-{suffix}",
    )


def observation(
    event_id: str,
    *,
    subject: str = "one",
    occurred_at: datetime = BASE_TIME,
    success: bool = True,
    **overrides: object,
) -> TelemetryObservation:
    payload: dict[str, object] = {
        "event_id": event_id,
        "identity": identity(subject),
        "occurred_at": occurred_at,
        "success": success,
        "operation": "request",
        "platform": "desktop",
        "app_version": "0.3.2",
        "provider": "local",
    }
    payload.update(overrides)
    return TelemetryObservation.model_validate(payload)


def adapter(*, retention_s: int = 3600, k_threshold: int = 1) -> InMemoryAggregateTelemetry:
    pseudonymizer = HmacPseudonymizer(
        {"v1": FAKE_SECRET},
        current_key_version="v1",
        rotation_period_s=60,
    )
    return InMemoryAggregateTelemetry(
        pseudonymizer,
        retention_s=retention_s,
        k_threshold=k_threshold,
    )


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_default_flag_off_is_strict_zero_write() -> None:
    telemetry = build_test_telemetry_adapter(TelemetryRuntimeConfig())
    assert isinstance(telemetry, NoopTelemetryAdapter)

    await telemetry.record(observation("evt-noop"))
    result = await telemetry.query(TelemetryQuery(k_threshold=1))
    receipt = await telemetry.delete(TelemetryDeleteRequest(tenant_pseudonym="0" * 64))
    purged = await telemetry.purge_expired(now=BASE_TIME)

    assert result == ()
    assert receipt.deleted_event_count == 0
    assert purged == 0
    assert telemetry.stored_event_count == 0
    assert telemetry.deletion_audit == ()


def test_runtime_config_does_not_repr_secret() -> None:
    config = TelemetryRuntimeConfig(enabled=True, hmac_secret=FAKE_SECRET)
    assert FAKE_SECRET.decode() not in repr(config)


def test_hmac_pseudonym_is_stable_domain_separated_and_has_no_raw_identity() -> None:
    pseudonymizer = HmacPseudonymizer(
        {"v1": FAKE_SECRET},
        current_key_version="v1",
        rotation_period_s=60,
    )
    first = pseudonymizer.materialize(observation("evt-one"))
    second = pseudonymizer.materialize(observation("evt-two"))
    serialized = first.model_dump_json()

    assert first.identity == second.identity
    assert (
        len(
            {
                first.identity.tenant_pseudonym,
                first.identity.user_pseudonym,
                first.identity.device_pseudonym,
            }
        )
        == 3
    )
    assert all(
        raw not in serialized
        for raw in (
            "tenant-sentinel-one",
            "user-sentinel-one",
            "device-sentinel-one",
        )
    )
    assert first.identity.key_version == "v1"
    assert first.identity.epoch == pseudonymizer.epoch_for(BASE_TIME)


def test_key_rotation_and_epoch_rotation_break_cross_period_linkability() -> None:
    pseudonymizer = HmacPseudonymizer(
        {"v1": FAKE_SECRET},
        current_key_version="v1",
        rotation_period_s=60,
    )
    current = pseudonymizer.materialize(observation("evt-current"))
    rotated = pseudonymizer.rotate(
        key_version="v2",
        secret=ROTATED_SECRET,
    ).materialize(observation("evt-rotated"))
    next_epoch = pseudonymizer.materialize(
        observation("evt-next", occurred_at=BASE_TIME + timedelta(seconds=60))
    )

    assert current.identity.user_pseudonym != rotated.identity.user_pseudonym
    assert current.identity.user_pseudonym != next_epoch.identity.user_pseudonym
    assert rotated.identity.key_version == "v2"
    assert current.identity.epoch != next_epoch.identity.epoch


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "field_name",
    (
        "raw_audio",
        "transcript",
        "summary",
        "prompt",
        "authorization",
        "cookie",
        "api_key",
        "error",
        "body",
        "url_query",
    ),
)
def test_forbidden_payload_fields_are_rejected(field_name: str) -> None:
    payload = observation("evt-forbidden").model_dump()
    payload[field_name] = "forbidden-sentinel"

    with pytest.raises(ValidationError):
        TelemetryObservation.model_validate(payload)


def test_raw_identity_is_rejected_by_query_and_delete_contracts() -> None:
    with pytest.raises(ValidationError):
        TelemetryQuery(user_pseudonym="user-sentinel-one")
    with pytest.raises(ValidationError):
        TelemetryDeleteRequest(user_pseudonym="user-sentinel-one")


def test_allowlists_reject_provider_platform_version_and_free_text_failure() -> None:
    with pytest.raises(ValidationError):
        observation("evt-bad-platform", platform="ios")
    with pytest.raises(ValidationError):
        observation("evt-bad-provider", provider="provider-free-text")
    with pytest.raises(ValidationError):
        observation("evt-bad-version", app_version="release candidate")
    with pytest.raises(ValidationError):
        observation("evt-bad-failure", success=False, failure_reason="free text")
    with pytest.raises(ValidationError):
        TelemetryQuery.model_validate({"failure_reason": "free text"})
    with pytest.raises(ValidationError):
        observation("evt-success-reason", success=True, failure_reason=FailureReason.INTERNAL)
    assert ProviderRegistry == frozenset(TelemetryProvider)
    assert TelemetryPlatform.DESKTOP.value == "desktop"


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_failure_without_reason_uses_stable_unknown_enum() -> None:
    telemetry = adapter()
    await telemetry.record(observation("evt-failed", success=False))

    stored = telemetry.stored_events()[0]
    assert stored.failure_reason is FailureReason.UNKNOWN


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_duplicate_event_id_is_idempotent_and_conflicting_replay_is_rejected() -> None:
    telemetry = adapter()
    event = observation("evt-idempotent")
    await telemetry.record(event)
    await telemetry.record(event)
    assert telemetry.stored_event_count == 1

    with pytest.raises(ValueError, match="event_id"):
        await telemetry.record(event.model_copy(update={"success": False}))


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_aggregate_contains_required_metrics_and_k_suppression() -> None:
    telemetry = adapter(k_threshold=2)
    await telemetry.record(
        observation(
            "evt-success",
            end_to_end_latency_ms=100,
            queue_wait_ms=20,
            audio_duration_ms=900,
        )
    )
    await telemetry.record(
        observation(
            "evt-failure",
            success=False,
            end_to_end_latency_ms=300,
            queue_wait_ms=40,
            audio_duration_ms=1_100,
        )
    )
    visible = await telemetry.query(TelemetryQuery(k_threshold=2))
    suppressed = await telemetry.query(TelemetryQuery(k_threshold=3))

    assert len(visible) == 1
    aggregate = visible[0]
    assert aggregate.request_count == 2
    assert aggregate.success_count == 1
    assert aggregate.failure_count == 1
    assert aggregate.success_rate == 0.5
    assert aggregate.failure_reason_counts == (
        FailureReasonCount(reason=FailureReason.UNKNOWN, event_count=1),
    )
    assert aggregate.latency_sum_ms == 400
    assert aggregate.queue_wait_sum_ms == 60
    assert aggregate.audio_duration_sum_ms == 2_000
    assert aggregate.audio_duration_event_count == 2
    assert suppressed == ()
    serialized = aggregate.model_dump_json()
    assert all(raw not in serialized for raw in ("tenant-sentinel-one", "user-sentinel-one"))


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_retention_removes_expired_events_before_aggregate_rebuild() -> None:
    telemetry = adapter(retention_s=60)
    await telemetry.record(
        observation(
            "evt-expired",
            occurred_at=BASE_TIME - timedelta(seconds=61),
        )
    )
    await telemetry.record(observation("evt-live"))

    removed = await telemetry.purge_expired(now=BASE_TIME)
    result = await telemetry.query(TelemetryQuery(k_threshold=1))

    assert removed == 1
    assert telemetry.stored_event_count == 1
    assert len(result) == 1
    assert result[0].request_count == 1


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_delete_hook_removes_only_target_pseudonymous_identity_and_audits() -> None:
    telemetry = adapter()
    await telemetry.record(observation("evt-target", subject="target"))
    await telemetry.record(observation("evt-other", subject="other"))
    target = telemetry.stored_events()[0].identity.user_pseudonym

    receipt = await telemetry.delete(
        TelemetryDeleteRequest(
            user_pseudonym=target,
            reason=DeletionReason.USER_REQUEST,
        )
    )

    assert receipt.deleted_event_count == 1
    assert len(telemetry.deletion_audit) == 1
    assert telemetry.stored_event_count == 1
    assert all(raw not in receipt.model_dump_json() for raw in ("target", "tenant-sentinel"))


@pytest.mark.asyncio  # type: ignore[untyped-decorator]
async def test_old_client_missing_optional_fields_uses_safe_defaults() -> None:
    legacy = TelemetryObservation(
        event_id="evt-legacy",
        identity=identity(),
        success=True,
    )
    telemetry = adapter()
    await telemetry.record(legacy)
    stored = telemetry.stored_events()[0]

    assert stored.platform is TelemetryPlatform.UNKNOWN
    assert stored.provider is TelemetryProvider.UNKNOWN
    assert stored.queue_wait_ms == 0
    assert stored.audio_duration_ms is None


def test_independent_module_has_no_production_wiring_or_sync_dependencies() -> None:
    module_root = Path(__file__).parents[2] / "app" / "telemetry"
    source = "\n".join(path.read_text(encoding="utf-8") for path in module_root.glob("*.py"))

    assert "app.adapters.repo" not in source
    assert "app.security" not in source
    assert "workflow_outbox" not in source
    assert "revision" not in source
    assert "migrations" not in source
