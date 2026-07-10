from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.repo.migrator import run_migrations
from app.artifacts.repository import ArtifactRepository
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact


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


@pytest.mark.unit
async def test_artifact_save_link_and_meeting_list(tmp_path: Path) -> None:
    repo = await _repo(tmp_path)
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
