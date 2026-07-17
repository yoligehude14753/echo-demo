from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.agents.embedded_runtime import RuntimeFrame
from app.config import Settings
from app.runtime.b13_composition import (
    B13_RUNTIME_FD_ENV,
    B13CompositionError,
    B13SessionCheckpointPort,
    create_b13_runtime_composition,
    make_b13_resume_identity,
)
from app.runtime.session_checkpoint_persistence import (
    PersistenceError,
    ResumeIdentity,
    SessionCheckpointRepository,
)

CREATED_AT = "2026-07-16T00:00:00+00:00"
RESUME_NOW = "2026-07-16T00:00:02+00:00"


class FakeRuntimeTransport:
    def __init__(self) -> None:
        self.sent: list[RuntimeFrame] = []
        self.closed = False

    async def send(self, frame: RuntimeFrame) -> None:
        self.sent.append(frame)

    async def receive(self) -> RuntimeFrame:
        raise AssertionError("composition test must not start the runtime handshake")

    async def close(self) -> None:
        self.closed = True


def make_identity(
    *,
    task_id: str = "task-b13",
    operation_key: str = "operation-b13",
    build_id: str = "kernel-b13-v1",
) -> ResumeIdentity:
    return ResumeIdentity(
        session_id="session-b13",
        task_id=task_id,
        operation_key=operation_key,
        model_config_revision=7,
        grant_snapshot={
            "schemaVersion": 1,
            "grantId": "grant-b13",
            "revision": 3,
            "taskId": task_id,
            "operationKey": operation_key,
            "deviceId": "device-b13",
            "issuedAt": CREATED_AT,
            "expiresAt": "2026-07-17T00:00:00+00:00",
            "workspaceRoots": [],
            "command": {"mode": "deny"},
            "network": {"mode": "deny"},
            "artifacts": {"mode": "deny"},
            "secrets": {"handles": []},
            "skills": {"allowed": []},
        },
        kernel_build_identity={
            "schemaVersion": 1,
            "kernelApiVersion": 1,
            "workerProtocolVersion": 1,
            "buildId": build_id,
            "sourceManifestSha256": "a" * 64,
        },
    )


def make_checkpoint(identity: ResumeIdentity) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "checkpointId": "checkpoint-b13-1",
        "taskId": identity.task_id,
        "operationKey": identity.operation_key,
        "modelConfigRevision": identity.model_config_revision,
        "grantRevision": identity.grant_snapshot["revision"],
        "grantSnapshot": dict(identity.grant_snapshot),
        "lastDurableEventSeq": 1,
        "messages": [
            {
                "messageId": "message-b13-1",
                "role": "user",
                "content": [{"type": "text", "text": "before restart"}],
            }
        ],
        "compactState": {
            "schemaVersion": 1,
            "strategy": "none",
            "summaryHash": None,
            "messageCountAtBoundary": 1,
        },
        "budgetState": {
            "turnsUsed": 1,
            "toolCallsUsed": 0,
            "modelInputTokens": 4,
            "modelOutputTokens": 2,
        },
        "createdAt": CREATED_AT,
    }


async def prepare_port(tmp_path: Path) -> tuple[B13SessionCheckpointPort, ResumeIdentity]:
    db_path = tmp_path / "b13-runtime.db"
    migration = await run_migrations(db_path)
    assert migration.errors == []
    identity = make_identity()
    repository = SessionCheckpointRepository(db_path)
    port = B13SessionCheckpointPort(repository, identity)
    await port.startup(identity.kernel_build_identity)
    await repository.append_event(
        identity.session_id,
        event_seq=1,
        event_type="agent.summary.updated",
        payload={"summary": "B13 deterministic resume"},
        durable_event_seq=1,
        occurred_at=CREATED_AT,
    )
    await port.save_checkpoint(make_checkpoint(identity))
    return port, identity


@pytest.mark.unit
async def test_b13_composition_injects_concrete_backend_and_repositories(tmp_path: Path) -> None:
    db_path = tmp_path / "b13-composition.db"
    migration = await run_migrations(db_path)
    assert migration.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
        agent_os_enabled=False,
    )
    transport = FakeRuntimeTransport()

    composition = await create_b13_runtime_composition(
        settings,
        InMemoryEventBus(),
        transport=transport,
        holder_id="b13-test",
    )
    try:
        assert composition.service.backend is composition.backend
        assert composition.backend.enabled is True
        assert composition.repositories.session_checkpoints.db_path == db_path
        assert composition.repositories.artifact_skill_projection.settings is settings
    finally:
        await composition.service.aclose()
        await composition.backend.aclose()
    assert transport.closed is True


@pytest.mark.unit
async def test_b13_composition_fails_closed_without_inherited_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(B13_RUNTIME_FD_ENV, raising=False)
    settings = Settings(
        db_path=tmp_path / "b13-missing-runtime.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
        agent_os_enabled=False,
    )

    with pytest.raises(B13CompositionError) as raised:
        await create_b13_runtime_composition(settings, InMemoryEventBus())

    assert raised.value.code == "EMBEDDED_RUNTIME_UNAVAILABLE"


@pytest.mark.unit
async def test_b13_composition_uses_inherited_fd_when_host_transport_is_not_injected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    read_fd, write_fd = os.pipe()
    monkeypatch.setenv(B13_RUNTIME_FD_ENV, str(read_fd))
    settings = Settings(
        db_path=tmp_path / "b13-inherited-runtime.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill-build",
        agent_os_enabled=True,
        agent_os_url="https://http-backend-must-not-be-selected.invalid",
    )
    migration = await run_migrations(settings.db_path)
    assert migration.errors == []

    composition = await create_b13_runtime_composition(settings, InMemoryEventBus())
    try:
        assert composition.backend.enabled is True
        assert composition.backend.is_embedded is True
        assert composition.service.backend is composition.backend
    finally:
        await composition.service.aclose()
        await composition.backend.aclose()
        os.close(write_fd)


@pytest.mark.unit
async def test_b13_checkpoint_pause_restart_resume_preserves_identity(tmp_path: Path) -> None:
    port, identity = await prepare_port(tmp_path)
    await port.close()

    restarted = B13SessionCheckpointPort(port.repository, identity)
    resumed = await restarted.restart(now=RESUME_NOW, max_age_seconds=60)

    assert resumed["taskId"] == identity.task_id
    assert resumed["operationKey"] == identity.operation_key
    assert resumed["modelConfigRevision"] == identity.model_config_revision
    assert resumed["grantRevision"] == identity.grant_snapshot["revision"]
    assert await restarted.current_durable_event_seq() == 1


@pytest.mark.unit
async def test_b13_session_port_rejects_build_and_identity_mismatch(tmp_path: Path) -> None:
    port, identity = await prepare_port(tmp_path)

    with pytest.raises(PersistenceError) as build_error:
        await port.startup({**identity.kernel_build_identity, "buildId": "kernel-b13-v2"})
    assert build_error.value.code == "RUNTIME_BUILD_MISMATCH"

    with pytest.raises(PersistenceError) as identity_error:
        await port.repository.resume(
            identity.session_id,
            make_identity(operation_key="different-operation"),
            current_durable_event_seq=1,
            now=RESUME_NOW,
        )
    assert identity_error.value.code == "CHECKPOINT_IDENTITY_MISMATCH"


@pytest.mark.unit
def test_b13_runtime_identity_adapter_binds_b10_grant_and_stabilizes_session_id() -> None:
    b10_grant = dict(make_identity().grant_snapshot)
    b10_grant.pop("operationKey")
    build = make_identity().kernel_build_identity

    first = make_b13_resume_identity(
        task_id="task-b13",
        operation_key="operation-b13",
        model_config_revision=7,
        grant_snapshot=b10_grant,
        kernel_build_identity=build,
    )
    second = make_b13_resume_identity(
        task_id="task-b13",
        operation_key="operation-b13",
        model_config_revision=7,
        grant_snapshot=b10_grant,
        kernel_build_identity=build,
    )

    assert first.session_id == second.session_id
    assert first.grant_snapshot["operationKey"] == "operation-b13"
    assert "operationKey" not in b10_grant
