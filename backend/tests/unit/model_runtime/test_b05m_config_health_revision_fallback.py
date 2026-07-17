"""B05M focused coverage for the config/health/revision/fallback write set."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from app.model_runtime.config_store import ModelRuntimeConfigStore, migrate_model_runtime_config
from app.model_runtime.credentials import InMemoryCredentialResolver
from app.model_runtime.errors import (
    MODEL_CONFIG_REVISION_CONFLICT,
    MODEL_CREDENTIAL_UNAVAILABLE,
    MODEL_FALLBACK_EXHAUSTED,
    ModelRuntimeConfigError,
)
from app.model_runtime.fallback import ExplicitFallbackRouter
from app.model_runtime.health import ModelHealthChecker, safe_model_diagnostic
from app.model_runtime.revision import TaskModelRevisionRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "b05m_model_runtime_config.json"


def _payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> ModelRuntimeConfigStore:
    store = ModelRuntimeConfigStore(tmp_path / "config.json")
    store.save(_payload())
    return store


def _capabilities() -> dict[str, bool]:
    return {
        "streaming": True,
        "tool_use": True,
        "parallel_tool_use": True,
        "tool_choice": True,
        "system_messages": True,
        "usage_in_stream": True,
        "prompt_cache": False,
        "multimodal_images": False,
        "multimodal_documents": False,
    }


@pytest.mark.unit
def test_b05m_save_read_uses_one_atomic_model_runtime_path_and_round_trips() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        store = ModelRuntimeConfigStore(Path(directory) / "config.json")
        saved = store.save(_payload())
        loaded = store.read()

        assert store.path == Path(directory) / "config.json"
        assert loaded == saved
        document = json.loads(store.path.read_text(encoding="utf-8"))
        assert set(document) == {"model_runtime"}
        assert document["model_runtime"]["configHash"] == saved.config_hash
        assert document["model_runtime"]["routes"]["agent_main"]["credentialHandle"] == "credential://primary"


@pytest.mark.unit
def test_b05m_migration_converts_legacy_single_route_without_second_source() -> None:
    legacy = _payload()["routes"]["agent_main"]  # type: ignore[index]
    migrated = migrate_model_runtime_config(legacy)

    assert migrated["schemaVersion"] == 1
    assert migrated["revision"] == 1
    assert set(migrated["routes"]) == {"agent_main"}  # type: ignore[arg-type]
    assert "credentialHandle" in migrated["routes"]["agent_main"]  # type: ignore[index]


@pytest.mark.unit
def test_b05m_revision_is_monotonic_and_expected_revision_is_checked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ModelRuntimeConfigError) as same_revision:
        store.save(_payload())
    assert same_revision.value.code == MODEL_CONFIG_REVISION_CONFLICT

    newer = _payload()
    newer["revision"] = 8
    with pytest.raises(ModelRuntimeConfigError) as wrong_expected:
        store.save(newer, expected_revision=6)
    assert wrong_expected.value.code == MODEL_CONFIG_REVISION_CONFLICT
    assert store.save(newer, expected_revision=7).revision == 8


@pytest.mark.unit
def test_b05m_task_snapshot_is_immutable_and_new_revision_applies_to_next_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = TaskModelRevisionRegistry(store)
    first = registry.begin_task("task-old")

    newer = copy.deepcopy(_payload())
    newer["revision"] = 8
    store.save(newer, expected_revision=7)

    assert registry.snapshot("task-old") is first
    assert registry.snapshot("task-old").revision == 7
    assert registry.begin_task("task-old").revision == 7
    assert registry.begin_task("task-new").revision == 8
    assert registry.snapshot_for_route("task-old", "backup").revision == 7


@pytest.mark.unit
def test_b05m_health_capability_probe_and_secret_safe_diagnostics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    route = store.read().routes["agent_main"]
    resolver = InMemoryCredentialResolver({"credential://primary": "sk-live-unit-secret"})

    def transport(_route, credential, _timeout):
        assert credential.value == "sk-live-unit-secret"
        return {"ok": True, "capabilities": _capabilities()}

    report = ModelHealthChecker(resolver, transport).probe_sync(
        route,
        required_capabilities=("streaming", "tool_use"),
    )
    assert report.status == "healthy"
    assert report.capability_probe is not None
    assert report.capability_probe.status == "supported"
    assert "sk-live-unit-secret" not in repr(report.public_dict())
    assert "credential" not in json.dumps(report.public_dict()).lower()

    assert "sk-live-unit-secret" not in safe_model_diagnostic(
        "provider failed sk-live-unit-secret https://provider.example/v1?token=raw"
    )
    assert "raw" not in safe_model_diagnostic(
        "provider failed sk-live-unit-secret https://provider.example/v1?token=raw"
    )


@pytest.mark.unit
def test_b05m_missing_external_credential_is_typed_and_non_blocking(tmp_path: Path) -> None:
    route = _store(tmp_path).read().routes["agent_main"]
    report = ModelHealthChecker(InMemoryCredentialResolver(), lambda *_: {"ok": True}).probe_sync(route)

    assert report.status == "unavailable"
    assert report.error_code == MODEL_CREDENTIAL_UNAVAILABLE
    assert report.public_dict()["diagnostic"] is None


@pytest.mark.unit
def test_b05m_fallback_event_is_typed_and_keeps_task_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = TaskModelRevisionRegistry(store)
    registry.begin_task("task-fallback")
    identity = registry.snapshot("task-fallback").identity(
        request_id="request-1",
        task_id="task-fallback",
        operation_key="turn-1",
    )
    # The call above intentionally exercises the single task-start path.
    backup = registry.binding("task-fallback").route("backup")
    report = ModelHealthChecker(
        InMemoryCredentialResolver({"credential://backup": "unit-secret"}),
        lambda *_: {"ok": True, "capabilities": _capabilities()},
    ).probe_sync(backup, required_capabilities=("streaming", "tool_use"))

    decision = ExplicitFallbackRouter(registry).select(
        "task-fallback",
        identity,
        "upstream_error",
        {"backup": report},
        required_capabilities=("streaming", "tool_use"),
    )
    assert decision.snapshot is not None
    assert decision.snapshot.route_id == "backup"
    assert decision.snapshot.revision == identity.config_revision == 7
    assert decision.event.public_dict()["type"] == "model.fallback"
    assert decision.event.public_dict()["toRouteId"] == "backup"
    assert "credential" not in json.dumps(decision.event.public_dict()).lower()

    exhausted = ExplicitFallbackRouter(registry).select(
        "task-fallback",
        identity,
        "health_unhealthy",
        {"backup": report.__class__(
            route_id="backup",
            status="unhealthy",
            capability_probe=report.capability_probe,
            error_code="MODEL_HEALTH_PROBE_FAILED",
            diagnostic=None,
            checked_at=report.checked_at,
            latency_ms=report.latency_ms,
        )},
    )
    assert exhausted.snapshot is None
    assert exhausted.event.outcome == "exhausted"
    assert exhausted.event.error_code == MODEL_FALLBACK_EXHAUSTED
