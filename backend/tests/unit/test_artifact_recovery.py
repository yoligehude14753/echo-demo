from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.artifacts.recovery import recover_skill_build_artifacts
from app.artifacts.repository import ArtifactRepository
from app.config import Settings
from app.schemas.artifact import GeneratedArtifact


def _write_artifact(
    root: Path,
    artifact_id: str,
    *,
    suffix: str,
    meta: dict[str, object] | None,
) -> None:
    directory = root / artifact_id
    directory.mkdir(parents=True)
    (directory / f"output.{suffix}").write_bytes(b"historic artifact")
    if meta is not None:
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )


@pytest.mark.unit
async def test_recovery_backfills_skill_build_and_links_historical_meeting(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    meeting_id = "m-history"
    artifact_id = "pptx-history-001"
    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="历史会议")
        await repo.update_meeting_state(
            meeting_id,
            state="finalized",
            minutes_json=json.dumps(
                {"todos": [{"id": "todo-history", "artifact_id": artifact_id}]}
            ),
        )
        _write_artifact(
            settings.skill_executor_build_dir,
            artifact_id,
            suffix="pptx",
            meta={
                "title": "历史方案演示",
                "artifact_type": "pptx",
                "meeting_id": meeting_id,
            },
        )
        _write_artifact(
            settings.skill_executor_build_dir,
            "html-unlinked-001",
            suffix="html",
            meta=None,
        )
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        linked_dir = settings.skill_executor_build_dir / "txt-outside-001"
        linked_dir.mkdir(parents=True)
        try:
            (linked_dir / "output.txt").symlink_to(outside)
        except OSError:
            pass

        artifact_repo = ArtifactRepository(settings)
        first = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )

        assert first.discovered == 2
        assert first.recovered == 2
        assert first.linked == 1
        assert {item.artifact_id for item in await artifact_repo.list_artifacts(limit=10)} == {
            "html-unlinked-001",
            artifact_id,
        }
        linked = await artifact_repo.list_meeting_artifacts(meeting_id)
        assert [item.artifact_id for item in linked] == [artifact_id]
        assert linked[0].metadata["recovered"] == "true"
        assert linked[0].metadata["meeting_id"] == meeting_id

        second = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )
        assert second.recovered == 0
        assert second.linked == 0
        assert second.already_recorded == 2
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_recovery_does_not_duplicate_existing_meeting_link(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    settings = Settings(
        db_path=db_path,
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
    )
    meeting_id = "m-existing-link"
    artifact_id = "html-existing-link"
    repo = SQLiteRepository(db_path)
    await repo.init()
    try:
        await repo.create_meeting(meeting_id, started_at=datetime.now(UTC), title="已有关联")
        _write_artifact(
            settings.skill_executor_build_dir,
            artifact_id,
            suffix="html",
            meta={"title": "已有产物", "artifact_type": "html", "meeting_id": meeting_id},
        )
        output = settings.skill_executor_build_dir / artifact_id / "output.html"

        artifact_repo = ArtifactRepository(settings)
        await artifact_repo.save_artifact(
            GeneratedArtifact(
                artifact_id=artifact_id,
                artifact_type="html",
                title="已有产物",
                file_path=str(output),
                mime_type="text/html",
                size_bytes=output.stat().st_size,
                generation_latency_ms=1,
                model="test",
                metadata={},
            )
        )
        await artifact_repo.link_artifact(
            artifact_id=artifact_id,
            source="artifact_generate",
            meeting_id=meeting_id,
        )

        report = await recover_skill_build_artifacts(
            settings=settings,
            repository=repo,
            artifact_repo=artifact_repo,
        )

        assert report.discovered == 1
        assert report.recovered == 0
        assert report.linked == 0
        assert report.already_recorded == 1
        assert len(await artifact_repo.list_links_for_artifact(artifact_id)) == 1
    finally:
        await repo.aclose()
