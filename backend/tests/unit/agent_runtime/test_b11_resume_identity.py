from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from app.adapters.repo.migrator import run_migrations
from app.runtime.session_checkpoint_persistence import (
    PersistenceError,
    ResumeIdentity,
    SessionCheckpointRepository,
)

CREATED_AT = "2026-07-15T00:00:00+00:00"
RESUME_NOW = "2026-07-15T00:00:02+00:00"


def make_identity(
    *,
    session_id: str = "session-resume-proof",
    task_id: str = "task-resume-proof",
    operation_key: str = "operation-resume-proof",
    model_revision: int = 7,
    grant_id: str = "grant-resume-v3",
    grant_revision: int = 3,
    build_id: str = "kernel-resume-v1",
    grant_expires_at: str = "2026-07-16T00:00:00+00:00",
) -> ResumeIdentity:
    return ResumeIdentity(
        session_id=session_id,
        task_id=task_id,
        operation_key=operation_key,
        model_config_revision=model_revision,
        grant_snapshot={
            "schemaVersion": 1,
            "grantId": grant_id,
            "revision": grant_revision,
            "taskId": task_id,
            "operationKey": operation_key,
            "deviceId": "device-resume-proof",
            "issuedAt": CREATED_AT,
            "expiresAt": grant_expires_at,
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
            "runtimeFingerprint": {
                "electron": "43.1.0",
                "node": "25.2.1",
                "v8": "15.0.245.13-electron.0",
                "modules": "148",
                "napi": "10",
            },
        },
    )


def make_checkpoint(identity: ResumeIdentity, *, event_seq: int = 1) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "checkpointId": "checkpoint-resume-proof-1",
        "taskId": identity.task_id,
        "operationKey": identity.operation_key,
        "modelConfigRevision": identity.model_config_revision,
        "grantRevision": identity.grant_snapshot["revision"],
        "grantSnapshot": identity.grant_snapshot,
        "lastDurableEventSeq": event_seq,
        "messages": [
            {
                "messageId": "message-before-restart",
                "role": "user",
                "content": [{"type": "text", "text": "before restart"}],
            }
        ],
        "compactState": {
            "schemaVersion": 1,
            "strategy": "none",
            "summaryHash": None,
            "messageCountAtBoundary": 1,
            "clearedToolUseIds": [],
        },
        "budgetState": {
            "turnsUsed": 1,
            "toolCallsUsed": 0,
            "modelInputTokens": 4,
            "modelOutputTokens": 2,
        },
        "createdAt": CREATED_AT,
    }


async def prepared_repository(
    tmp_path: Path,
    *,
    identity: ResumeIdentity | None = None,
    checkpoint_event_seq: int = 1,
) -> tuple[SessionCheckpointRepository, ResumeIdentity, dict[str, Any], Path]:
    db_path = tmp_path / "resume-proof.db"
    migration = await run_migrations(db_path)
    assert migration.errors == []

    bound = identity or make_identity()
    repository = SessionCheckpointRepository(db_path)
    await repository.create_session(bound, created_at=CREATED_AT)
    if checkpoint_event_seq >= 1:
        await repository.append_event(
            bound.session_id,
            event_seq=checkpoint_event_seq,
            event_type="agent.summary.updated",
            payload={"summary": "durable resume proof"},
            durable_event_seq=checkpoint_event_seq,
            occurred_at=CREATED_AT,
        )
    checkpoint = make_checkpoint(bound, event_seq=checkpoint_event_seq)
    await repository.save_checkpoint(
        bound.session_id,
        checkpoint,
        saved_at=CREATED_AT,
    )
    return repository, bound, checkpoint, db_path


@pytest.mark.unit
async def test_turn_checkpoint_save_restart_resume_round_trip_preserves_full_identity(
    tmp_path: Path,
) -> None:
    repository, identity, checkpoint, _db_path = await prepared_repository(tmp_path)

    resumed = await repository.resume(
        identity.session_id,
        identity,
        current_durable_event_seq=1,
        now=RESUME_NOW,
        max_age_seconds=60,
    )

    assert resumed == {**checkpoint, "checksum": resumed["checksum"]}
    assert resumed["taskId"] == identity.task_id
    assert resumed["operationKey"] == identity.operation_key
    assert resumed["modelConfigRevision"] == identity.model_config_revision
    assert resumed["grantRevision"] == identity.grant_snapshot["revision"]
    assert identity.grant_snapshot["grantId"] == "grant-resume-v3"
    assert identity.kernel_build_identity["buildId"] == "kernel-resume-v1"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("task", lambda identity: make_identity(task_id="other-task")),
        (
            "operation",
            lambda identity: make_identity(operation_key="other-operation"),
        ),
        (
            "model revision",
            lambda identity: make_identity(model_revision=identity.model_config_revision + 1),
        ),
        (
            "grant snapshot",
            lambda identity: make_identity(grant_id="different-grant-same-revision"),
        ),
        (
            "kernel build",
            lambda identity: make_identity(build_id="kernel-resume-v2"),
        ),
    ],
)
async def test_resume_identity_mismatch_fails_closed(
    tmp_path: Path,
    case: str,
    mutate: Callable[[ResumeIdentity], ResumeIdentity],
) -> None:
    repository, identity, _checkpoint, _db_path = await prepared_repository(tmp_path)

    with pytest.raises(PersistenceError) as raised:
        await repository.resume(
            identity.session_id,
            mutate(identity),
            current_durable_event_seq=1,
            now=RESUME_NOW,
        )

    assert raised.value.code == "CHECKPOINT_IDENTITY_MISMATCH", case


@pytest.mark.unit
async def test_corrupt_checkpoint_fails_closed_before_resume(tmp_path: Path) -> None:
    repository, identity, _checkpoint, db_path = await prepared_repository(tmp_path)

    async with aiosqlite.connect(str(db_path)) as connection:
        cursor = await connection.execute(
            "SELECT payload_json FROM agent_runtime_checkpoints WHERE session_id = ?",
            (identity.session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        payload = json.loads(row[0])
        payload["messages"][0]["content"][0]["text"] = "tampered"
        await connection.execute(
            "UPDATE agent_runtime_checkpoints SET payload_json = ? WHERE session_id = ?",
            (json.dumps(payload), identity.session_id),
        )
        await connection.commit()

    with pytest.raises(PersistenceError) as raised:
        await repository.resume(
            identity.session_id,
            identity,
            current_durable_event_seq=1,
            now=RESUME_NOW,
        )

    assert raised.value.code == "CHECKPOINT_CORRUPT"


@pytest.mark.unit
async def test_stale_checkpoint_fails_closed(tmp_path: Path) -> None:
    repository, identity, _checkpoint, _db_path = await prepared_repository(tmp_path)

    with pytest.raises(PersistenceError) as raised:
        await repository.resume(
            identity.session_id,
            identity,
            current_durable_event_seq=1,
            now="2026-07-15T00:02:00+00:00",
            max_age_seconds=60,
        )

    assert raised.value.code == "CHECKPOINT_STALE"


@pytest.mark.unit
async def test_expired_grant_and_future_durable_sequence_fail_closed(tmp_path: Path) -> None:
    expired_identity = make_identity(grant_expires_at="2026-07-15T00:00:01+00:00")
    repository, identity, _checkpoint, _db_path = await prepared_repository(
        tmp_path,
        identity=expired_identity,
    )

    with pytest.raises(PersistenceError) as expired:
        await repository.resume(
            identity.session_id,
            identity,
            current_durable_event_seq=1,
            now=RESUME_NOW,
        )
    assert expired.value.code == "GRANT_EXPIRED"

    with pytest.raises(PersistenceError) as ahead:
        await repository.save_checkpoint(
            identity.session_id,
            make_checkpoint(identity, event_seq=2),
            saved_at=CREATED_AT,
        )
    assert ahead.value.code == "CHECKPOINT_EVENT_SEQ_AHEAD"
