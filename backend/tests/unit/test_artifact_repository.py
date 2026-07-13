from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from app.adapters.repo.migrator import run_migrations
from app.artifacts.repository import ArtifactRepository
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact
from app.security import Principal
from app.security.context import bind_principal, current_principal, reset_principal


async def _repo(tmp_path: Path) -> ArtifactRepository:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    return ArtifactRepository(
        Settings(
            db_path=db_path,
            storage_dir=tmp_path / "storage",
            skill_executor_build_dir=tmp_path / "skill_build",
        )
    )


def _artifact(tmp_path: Path, artifact_id: str = "pdf-art-1") -> GeneratedArtifact:
    build_dir = tmp_path / "skill_build" / artifact_id
    build_dir.mkdir(parents=True, exist_ok=True)
    output = build_dir / "output.pdf"
    output.write_bytes(b"%PDF-1.4\n")
    return GeneratedArtifact(
        artifact_id=artifact_id,
        artifact_type="pdf",
        title="会议 PDF",
        file_path=str(output),
        mime_type="application/pdf",
        size_bytes=output.stat().st_size,
        generation_latency_ms=12.5,
        model="test-model",
        metadata={"kind": "pdf"},
    )


async def _seed_meeting(repo: ArtifactRepository, meeting_id: str) -> None:
    principal = current_principal()
    async with aiosqlite.connect(str(repo.settings.db_path)) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id)
               VALUES (?, 'ended', '2026-01-01', ?, ?, ?)
               ON CONFLICT(tenant_id, owner_id, id) DO NOTHING""",
            (
                meeting_id,
                principal.tenant_id,
                principal.device_id,
                principal.owner_id,
            ),
        )
        await conn.commit()


@pytest.mark.unit
async def test_artifact_save_link_and_meeting_list(tmp_path: Path) -> None:
    repo = await _repo(tmp_path)
    await _seed_meeting(repo, "mtg-1")
    artifact = await repo.save_artifact(_artifact(tmp_path))

    link = await repo.link_artifact(
        artifact_id=artifact.artifact_id,
        source="todo",
        meeting_id="mtg-1",
        todo_id="todo-1",
        run_id=None,
    )
    duplicate = await repo.link_artifact(
        artifact_id=artifact.artifact_id,
        source="todo",
        meeting_id="mtg-1",
        todo_id="todo-1",
        run_id=None,
    )

    assert duplicate.link_id == link.link_id
    assert await repo.count_links(artifact.artifact_id) == 1
    meeting_artifacts = await repo.list_meeting_artifacts("mtg-1")
    assert [item.artifact_id for item in meeting_artifacts] == [artifact.artifact_id]
    assert meeting_artifacts[0].metadata["kind"] == "pdf"
    todo_artifacts = await repo.list_todo_artifacts("mtg-1", "todo-1")
    assert [item.artifact_id for item in todo_artifacts] == [artifact.artifact_id]


@pytest.mark.unit
async def test_unlink_meeting_leaves_metadata_until_caller_deletes(tmp_path: Path) -> None:
    repo = await _repo(tmp_path)
    await _seed_meeting(repo, "mtg-1")
    artifact = await repo.save_artifact(_artifact(tmp_path))
    await repo.link_artifact(
        artifact_id=artifact.artifact_id,
        source="todo",
        meeting_id="mtg-1",
        todo_id="todo-1",
        run_id=None,
    )

    unlinked = await repo.unlink_meeting("mtg-1")

    assert [item.artifact_id for item in unlinked] == [artifact.artifact_id]
    assert await repo.count_links(artifact.artifact_id) == 0
    assert await repo.get_artifact(artifact.artifact_id) is not None
    assert await repo.delete_artifact_metadata(artifact.artifact_id) is True
    assert await repo.get_artifact(artifact.artifact_id) is None


@pytest.mark.unit
async def test_artifacts_and_links_are_principal_scoped(tmp_path: Path) -> None:
    repo = await _repo(tmp_path)
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")

    token_a = bind_principal(principal_a)
    try:
        await _seed_meeting(repo, "meeting-a")
        artifact = await repo.save_artifact(_artifact(tmp_path, "artifact-shared"))
        await repo.link_artifact(
            artifact_id=artifact.artifact_id,
            source="todo",
            meeting_id="meeting-a",
        )
    finally:
        reset_principal(token_a)

    token_b = bind_principal(principal_b)
    try:
        await _seed_meeting(repo, "meeting-b")
        assert await repo.get_artifact("artifact-shared") is None
        assert await repo.list_artifacts() == []
        assert await repo.list_meeting_artifacts("meeting-a") == []
        assert await repo.delete_artifact_metadata("artifact-shared") is False
        artifact_b = await repo.save_artifact(_artifact(tmp_path, "artifact-shared"))
        await repo.link_artifact(
            artifact_id=artifact_b.artifact_id,
            source="todo",
            meeting_id="meeting-b",
        )
        assert await repo.count_links("artifact-shared") == 1
        assert await repo.list_meeting_artifacts("meeting-a") == []
        assert await repo.delete_artifact_metadata("artifact-shared") is True
        assert await repo.get_artifact("artifact-shared") is None
    finally:
        reset_principal(token_b)

    token_a = bind_principal(principal_a)
    try:
        assert await repo.get_artifact("artifact-shared") is not None
        assert await repo.count_links("artifact-shared") == 1
    finally:
        reset_principal(token_a)
