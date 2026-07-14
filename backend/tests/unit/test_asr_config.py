"""Typed ASR runtime configuration contract tests."""

from __future__ import annotations

import pytest
from app.config import Settings
from pydantic import ValidationError


@pytest.mark.unit
def test_asr_scheduler_defaults_are_safe_and_keep_firered_compatibility() -> None:
    settings = Settings()
    assert settings.stt_backend == "firered"
    assert settings.asr_scheduler_enabled is False
    assert settings.asr_stepfun_enabled is False
    assert settings.asr_stepfun_transport == "sse_one_shot"
    assert settings.asr_local_enabled is False
    assert settings.asr_eligible_providers == ("firered",)
    assert settings.asr_provider_weights["firered"] == 1.0
    assert settings.asr_provider_concurrency["firered"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"asr_scheduler_max_concurrency": 0},
        {"asr_scheduler_queue_size": -1},
        {"asr_job_deadline_s": 0},
        {"asr_max_attempts": 0},
        {"asr_circuit_failure_threshold": 0},
        {"asr_circuit_cooldown_s": 0},
        {"asr_scope_max_concurrency": 0},
        {"asr_provider_weights": {"firered": 0}},
        {"asr_provider_concurrency": {"firered": 0}},
        {"asr_eligible_providers": ("",)},
    ],
)
def test_asr_limits_reject_unbounded_or_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)


@pytest.mark.unit
def test_enabling_stepfun_without_credential_fails_closed_without_printing_secret() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(asr_scheduler_enabled=True, asr_stepfun_enabled=True)
    assert "STEPFUN_API_KEY" not in str(error.value)


@pytest.mark.unit
def test_enabling_local_without_model_path_fails_closed() -> None:
    with pytest.raises(ValidationError):
        Settings(asr_scheduler_enabled=True, asr_local_enabled=True)


@pytest.mark.unit
def test_provider_maps_must_cover_eligible_set() -> None:
    with pytest.raises(ValidationError):
        Settings(
            asr_eligible_providers=("firered", "custom"),
            asr_provider_weights={"firered": 1.0},
            asr_provider_concurrency={"firered": 1, "custom": 1},
        )


@pytest.mark.unit
def test_settings_repr_does_not_include_stepfun_or_firered_key() -> None:
    settings = Settings(
        stt_firered_api_key="fixture-only",
        asr_stepfun_api_key="fixture-only-too",
    )
    rendered = repr(settings)
    assert "fixture-only" not in rendered


@pytest.mark.unit
def test_stepfun_transport_is_capability_driven_not_a_boolean_alias() -> None:
    settings = Settings(asr_stepfun_transport="websocket_stream")
    assert settings.asr_stepfun_transport == "websocket_stream"
    with pytest.raises(ValidationError):
        Settings(asr_stepfun_transport="streaming")
