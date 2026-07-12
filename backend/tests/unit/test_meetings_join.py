"""Phase 4 M_meeting_history：会议历史 4 个 GET endpoint 的 DB join 单测。

关注点：
- 一个 meeting_id 能从 SQLite 查出 transcript / minutes / artifacts 三件套
- 列表 endpoint 的计数（n_segments / n_speakers / has_minutes）口径正确
- 404 边界：会议不存在 / 还未生成纪要

不重测 pipeline 本身（test_meeting_pipeline_repo.py 已覆盖）；这里只校 HTTP
层 + repo join 是否一致。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import app.api.meetings as meetings_api
import pytest
from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.deps import (
    aclose_repository,
    get_event_bus,
    get_repository,
    get_workflow_service,
    reset_deps_for_test,
)
from app.api.meetings import reset_meeting_pipeline
from app.api.retrieval import get_rag
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.main import create_app
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.meeting import TranscriptSegment
from app.schemas.workflow import WorkflowRunCreate
from app.workflows.kernel import WorkflowDispatcher
from app.workflows.service import WorkflowService
from fastapi.testclient import TestClient


@pytest.fixture
async def repo(tmp_path: Path) -> SQLiteRepository:
    """每个测试一份干净 sqlite。"""
    db_path = tmp_path / "echo.db"
    result = await run_migrations(db_path)
    assert result.errors == []
    r = SQLiteRepository(db_path)
    await r.init()
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
def client(tmp_path: Path, repo: SQLiteRepository) -> TestClient:
    """注入 repo 单例 + 关掉 lifespan 副作用。

    create_app 的 lifespan 默认会跑 prober / workspace scan，这些在单测里都不
    需要；TestClient 只在用 with-block 时才触发 lifespan，我们直接 TestClient(app)
    所以路径上不会跑那些后台 task。但要小心 dependency_overrides 必须先注册。
    """
    reset_deps_for_test()
    reset_meeting_pipeline()
    app = create_app()
    settings = Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        rag_index_dir=tmp_path / "rag",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_repository] = lambda: repo
    return TestClient(app)


async def _seed_meeting(
    repo: RepositoryPort,
    meeting_id: str,
    *,
    title: str,
    started_at: datetime,
    segments: list[TranscriptSegment],
    minutes_payload: dict[str, object] | None = None,
    ended_at: datetime | None = None,
) -> None:
    """直接落 DB（绕开 pipeline / LLM），单独验 endpoint 行为。"""
    await repo.create_meeting(meeting_id, started_at=started_at, title=title)
    captured = started_at
    for seg in segments:
        await repo.append_meeting_segment(meeting_id, seg, captured_at=captured)
    if minutes_payload is not None:
        await repo.update_meeting_state(
            meeting_id,
            state="finalized",
            ended_at=ended_at or started_at + timedelta(minutes=10),
            finalized_at=ended_at or started_at + timedelta(minutes=10),
            minutes_json=json.dumps(minutes_payload, ensure_ascii=False),
        )
    elif ended_at is not None:
        await repo.update_meeting_state(meeting_id, state="ended", ended_at=ended_at)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "echo.db",
        storage_dir=tmp_path / "storage",
        skill_executor_build_dir=tmp_path / "skill_build",
        rag_index_dir=tmp_path / "rag",
    )


async def _seed_artifact_link(
    tmp_path: Path,
    *,
    artifact_id: str,
    meeting_id: str,
    artifact_type: str = "pdf",
    title: str = "会议产物",
    body: bytes = b"artifact",
    todo_id: str | None = None,
) -> GeneratedArtifact:
    ext = "pdf" if artifact_type == "pdf" else artifact_type
    build_dir = tmp_path / "skill_build" / artifact_id
    build_dir.mkdir(parents=True, exist_ok=True)
    output = build_dir / f"output.{ext}"
    output.write_bytes(body)
    artifact = GeneratedArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        title=title,
        file_path=str(output),
        mime_type="application/pdf" if ext == "pdf" else "text/plain",
        size_bytes=output.stat().st_size,
        generation_latency_ms=1.0,
        model="test-model",
        metadata={"kind": artifact_type},
    )
    artifact_repo = ArtifactRepository(_settings(tmp_path))
    await artifact_repo.save_artifact(artifact)
    await artifact_repo.link_artifact(
        artifact_id=artifact.artifact_id,
        source="todo" if todo_id else "meeting",
        meeting_id=meeting_id,
        todo_id=todo_id,
    )
    return artifact


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_meetings_aggregates_counts(client: TestClient, repo: SQLiteRepository) -> None:
    """两个 meeting：A 有 3 段 + 2 说话人 + 已 finalize；B 仅 1 段 + 进行中。"""
    t0 = datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    await _seed_meeting(
        repo,
        "mtg-A",
        title="Q3 复盘",
        started_at=t0,
        segments=[
            TranscriptSegment(
                text="开场", start_ms=0, end_ms=500, speaker_id="spk_A", speaker_label="说话人1"
            ),
            TranscriptSegment(
                text="数据", start_ms=600, end_ms=1100, speaker_id="spk_B", speaker_label="说话人2"
            ),
            TranscriptSegment(
                text="收尾",
                start_ms=1200,
                end_ms=1800,
                speaker_id="spk_A",
                speaker_label="说话人1",
            ),
        ],
        minutes_payload={
            "meeting_id": "mtg-A",
            "title": "Q3 复盘",
            "duration_sec": 120,
            "speakers": ["说话人1", "说话人2"],
            "summary": "Q3 销售达成 95%",
            "sections": [{"heading": "亮点", "bullets": ["新签 3 单", "客单价 +12%"]}],
            "decisions": ["Q4 重点扩张"],
            "action_items": ["李明 周五前出方案"],
            "created_at": "2026-05-28T09:10:00+00:00",
        },
    )
    await _seed_meeting(
        repo,
        "mtg-B",
        title="临时同步",
        started_at=t0 + timedelta(hours=1),
        segments=[
            TranscriptSegment(
                text="先聊一下", start_ms=0, end_ms=900, speaker_id="spk_A", speaker_label="说话人1"
            ),
        ],
    )

    r = client.get("/meetings")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    by_id = {m["meeting_id"]: m for m in data}

    a = by_id["mtg-A"]
    assert a["title"] == "Q3 复盘"
    assert a["state"] == "finalized"
    assert a["n_segments"] == 3
    assert a["n_speakers"] == 2
    assert a["has_minutes"] is True
    assert a["finalized_at"] is not None

    b = by_id["mtg-B"]
    assert b["state"] == "in_meeting"
    assert b["n_segments"] == 1
    assert b["n_speakers"] == 1
    assert b["has_minutes"] is False

    # 排序：started_at DESC，所以 mtg-B 在前
    assert data[0]["meeting_id"] == "mtg-B"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_transcript_returns_segments(client: TestClient, repo: SQLiteRepository) -> None:
    t0 = datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    await _seed_meeting(
        repo,
        "mtg-1",
        title="t1",
        started_at=t0,
        segments=[
            TranscriptSegment(
                text="一段", start_ms=0, end_ms=500, speaker_id="spk_A", speaker_label="说话人1"
            ),
            TranscriptSegment(
                text="两段",
                start_ms=600,
                end_ms=1200,
                speaker_id="spk_B",
                speaker_label="说话人2",
            ),
        ],
    )
    r = client.get("/meetings/mtg-1/transcript")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 2
    assert body[0]["text"] == "一段"
    assert body[0]["speaker_label"] == "说话人1"
    assert body[1]["text"] == "两段"


@pytest.mark.unit
def test_get_transcript_404_on_missing(client: TestClient) -> None:
    r = client.get("/meetings/no-such-id/transcript")
    assert r.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_minutes_roundtrips_json(client: TestClient, repo: SQLiteRepository) -> None:
    t0 = datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    payload = {
        "meeting_id": "mtg-min",
        "title": "落库纪要",
        "duration_sec": 600,
        "speakers": ["说话人1"],
        "summary": "短总结",
        "sections": [{"heading": "议题", "bullets": ["要点"]}],
        "decisions": [],
        "action_items": [],
        "created_at": "2026-05-28T09:30:00+00:00",
    }
    await _seed_meeting(
        repo,
        "mtg-min",
        title="落库纪要",
        started_at=t0,
        segments=[
            TranscriptSegment(text="x", start_ms=0, end_ms=500, speaker_id="spk_A"),
        ],
        minutes_payload=payload,
    )
    r = client.get("/meetings/mtg-min/minutes")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "落库纪要"
    assert data["summary"] == "短总结"
    assert data["sections"][0]["heading"] == "议题"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_minutes_404_when_not_finalized(
    client: TestClient, repo: SQLiteRepository
) -> None:
    t0 = datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    await _seed_meeting(
        repo,
        "mtg-no-minutes",
        title="进行中",
        started_at=t0,
        segments=[TranscriptSegment(text="x", start_ms=0, end_ms=500)],
    )
    r = client.get("/meetings/mtg-no-minutes/minutes")
    assert r.status_code == 404


@pytest.mark.unit
def test_get_minutes_404_on_missing_meeting(client: TestClient) -> None:
    r = client.get("/meetings/no-such-id/minutes")
    assert r.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_artifacts_returns_db_linked_artifacts(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
) -> None:
    """0.3 起会议产物以 artifact_links 为事实源。"""
    t0 = datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    await _seed_meeting(
        repo,
        "mtg-art",
        title="t",
        started_at=t0,
        segments=[TranscriptSegment(text="x", start_ms=0, end_ms=500)],
    )
    artifact = await _seed_artifact_link(
        tmp_path,
        artifact_id="artifact-mtg-001",
        meeting_id="mtg-art",
        title="会议 PDF",
        todo_id="todo-1",
    )
    r = client.get("/meetings/mtg-art/artifacts")
    assert r.status_code == 200, r.text
    data = r.json()
    assert [item["artifact_id"] for item in data] == [artifact.artifact_id]
    assert data[0]["title"] == "会议 PDF"


@pytest.mark.unit
def test_get_artifacts_404_on_missing(client: TestClient) -> None:
    r = client.get("/meetings/no-such-id/artifacts")
    assert r.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_share_page_includes_minutes_and_artifacts(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
) -> None:
    artifact_id = "artifact-share-001"
    await _seed_meeting(
        repo,
        "mtg-share",
        title="扫码会议",
        started_at=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": "mtg-share",
            "title": "扫码会议纪要",
            "duration_sec": 60,
            "summary": "这是一段可以扫码保存的纪要。",
            "sections": [{"heading": "重点", "bullets": ["扫码保存", "下载产物"]}],
            "decisions": ["保留分享链接"],
            "todos": [{"id": "todo-1", "text": "生成 PDF"}],
            "action_items": [],
        },
    )
    await _seed_artifact_link(
        tmp_path,
        artifact_id=artifact_id,
        meeting_id="mtg-share",
        title="扫码会议输出",
        body=b"%PDF-1.4\nmock",
        todo_id="todo-1",
    )

    r = client.get("/meetings/mtg-share/share")

    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "扫码会议纪要" in r.text
    assert "这是一段可以扫码保存的纪要" in r.text
    assert "保存纪要.md" in r.text
    assert "/meetings/mtg-share/minutes.md" in r.text
    assert "扫码会议输出" in r.text
    assert f"/artifacts/{artifact_id}/download" in r.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_minutes_markdown(
    client: TestClient,
    repo: SQLiteRepository,
) -> None:
    await _seed_meeting(
        repo,
        "mtg-minutes-md",
        title="扫码会议",
        started_at=datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": "mtg-minutes-md",
            "title": "扫码会议纪要",
            "duration_sec": 60,
            "summary": "这是一段可以保存为 Markdown 的纪要。",
            "sections": [{"heading": "重点", "bullets": ["扫码保存", "下载纪要"]}],
            "decisions": ["保留分享链接"],
            "todos": [{"id": "todo-1", "text": "生成 PDF", "status": "pending"}],
            "action_items": [],
            "created_at": "2026-05-28T10:01:00+00:00",
        },
    )

    r = client.get("/meetings/mtg-minutes-md/minutes.md")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in r.headers["content-disposition"]
    assert "# 扫码会议纪要" in r.text
    assert "这是一段可以保存为 Markdown 的纪要" in r.text
    assert "- [待处理] 生成 PDF" in r.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_meeting_outputs_clears_minutes_and_deletes_artifacts(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
) -> None:
    artifact_id = "artifact-delete-001"
    await _seed_meeting(
        repo,
        "mtg-clear",
        title="清理会议",
        started_at=datetime(2026, 5, 28, 11, 0, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": "mtg-clear",
            "title": "清理会议纪要",
            "duration_sec": 60,
            "summary": "待删除",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    linked_artifact = await _seed_artifact_link(
        tmp_path,
        artifact_id=artifact_id,
        meeting_id="mtg-clear",
        artifact_type="txt",
        title="待清理产物",
        body=b"delete me",
    )
    build_dir = tmp_path / "skill_build" / artifact_id
    rogue_dir = tmp_path / "skill_build" / "rogue-from-client"
    rogue_dir.mkdir(parents=True)
    (rogue_dir / "output.txt").write_text("do not delete", encoding="utf-8")

    r = client.request(
        "DELETE",
        "/meetings/mtg-clear/outputs",
        json={"artifact_ids": [artifact_id, "rogue-from-client"], "clear_minutes": True},
    )

    assert r.status_code == 200
    assert r.json()["artifacts_deleted"] == 1
    assert r.json()["artifact_ids"] == [linked_artifact.artifact_id]
    assert not build_dir.exists()
    assert rogue_dir.exists()
    artifact_repo = ArtifactRepository(_settings(tmp_path))
    assert await artifact_repo.get_artifact(artifact_id) is None
    rec = await repo.get_meeting("mtg-clear")
    assert rec is not None
    assert rec.state == "ended"
    assert rec.minutes_json is None
    assert rec.minutes_status is None
    assert rec.minutes_cleared_at is not None
    runs = client.get("/workflows/runs?meeting_id=mtg-clear").json()
    cleanup_run = next(item for item in runs if item["kind"] == "meeting.outputs.clear")
    assert cleanup_run["state"] == "succeeded"
    assert cleanup_run["output"]["file_cleanup_targets"] == [
        {
            "artifact_id": artifact_id,
            "root": "skill_build",
            "relative_path": f"{artifact_id}/output.txt",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_meeting_outputs_uses_new_request_after_minutes_regeneration(
    client: TestClient,
    repo: SQLiteRepository,
) -> None:
    meeting_id = "mtg-clear-regenerated"
    started_at = datetime(2026, 5, 28, 11, 30, tzinfo=UTC)
    await _seed_meeting(
        repo,
        meeting_id,
        title="重复清理会议",
        started_at=started_at,
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "第一版纪要",
            "duration_sec": 60,
            "summary": "第一版",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )

    first = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )
    assert first.status_code == 200
    cleared = await repo.get_meeting(meeting_id)
    assert cleared is not None and cleared.minutes_json is None

    regenerated_at = started_at + timedelta(hours=1)
    await repo.update_meeting_state(
        meeting_id,
        state="finalized",
        finalized_at=regenerated_at,
        minutes_json=json.dumps(
            {
                "meeting_id": meeting_id,
                "title": "第二版纪要",
                "duration_sec": 60,
                "summary": "第二版必须能再次清理",
                "sections": [],
                "decisions": [],
                "todos": [],
                "action_items": [],
            },
            ensure_ascii=False,
        ),
        minutes_status="ok",
        rag_projection_state="index_pending",
    )

    second = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )

    assert second.status_code == 200
    cleared_again = await repo.get_meeting(meeting_id)
    assert cleared_again is not None
    assert cleared_again.minutes_json is None
    runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    cleanup_runs = [item for item in runs if item["kind"] == "meeting.outputs.clear"]
    assert len(cleanup_runs) == 2
    assert {item["state"] for item in cleanup_runs} == {"succeeded"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_meeting_outputs_removes_bm25_projection_before_response(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
) -> None:
    meeting_id = "mtg-clear-bm25"
    await _seed_meeting(
        repo,
        meeting_id,
        title="清理检索投影",
        started_at=datetime(2026, 5, 28, 11, 45, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "清理检索投影",
            "duration_sec": 60,
            "summary": "孔雀石投影必须立即消失",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    rag = get_rag(_settings(tmp_path))
    await rag.ingest_meeting(meeting_id, "孔雀石投影必须立即消失", "清理检索投影")
    assert any(hit.doc_id == f"meeting-{meeting_id}" for hit in await rag.query("孔雀石"))

    response = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )

    assert response.status_code == 200
    assert all(hit.doc_id != f"meeting-{meeting_id}" for hit in await rag.query("孔雀石"))
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.rag_projection_state == "deleted"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_meeting_outputs_fails_closed_when_physical_bm25_delete_fails(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "mtg-clear-bm25-failure"
    await _seed_meeting(
        repo,
        meeting_id,
        title="失败也必须隐藏",
        started_at=datetime(2026, 5, 28, 11, 50, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "失败也必须隐藏",
            "duration_sec": 60,
            "summary": "绿松石物理文件暂时删不掉",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    rag = get_rag(_settings(tmp_path))
    await rag.ingest_meeting(meeting_id, "绿松石物理文件暂时删不掉", "失败也必须隐藏")
    assert any(hit.doc_id == f"meeting-{meeting_id}" for hit in await rag.query("绿松石"))

    async def fail_delete(_doc_id: str, **_kwargs: object) -> None:
        raise OSError("simulated index file lock")

    monkeypatch.setattr(rag, "delete", fail_delete)
    response = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )

    assert response.status_code == 200
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.rag_projection_state == "delete_failed"
    assert "index file lock" in (meeting.rag_projection_error or "")
    assert all(hit.doc_id != f"meeting-{meeting_id}" for hit in await rag.query("绿松石"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_meeting_outputs_rolls_back_domain_writes_when_progress_commit_crashes(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_meeting(
        repo,
        "mtg-cleanup-crash",
        title="清理崩溃回滚",
        started_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": "mtg-cleanup-crash",
            "title": "仍应保留",
            "duration_sec": 60,
            "summary": "rollback",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    artifact = await _seed_artifact_link(
        tmp_path,
        artifact_id="artifact-cleanup-crash",
        meeting_id="mtg-cleanup-crash",
        artifact_type="txt",
        body=b"must survive rollback",
    )
    service = get_workflow_service(_settings(tmp_path), get_event_bus())
    original_commit = service.commit_run_progress_atomic

    async def crash_after_domain_writer(run_id: str, **kwargs: object) -> object:
        domain_writer = kwargs["domain_writer"]

        async def write_then_crash(conn: object) -> None:
            await domain_writer(conn)  # type: ignore[operator]
            raise RuntimeError("injected crash before workflow terminal commit")

        kwargs["domain_writer"] = write_then_crash
        return await original_commit(run_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "commit_run_progress_atomic", crash_after_domain_writer)

    response = client.request(
        "DELETE",
        "/meetings/mtg-cleanup-crash/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )

    assert response.status_code == 500
    artifact_repo = ArtifactRepository(_settings(tmp_path))
    assert await artifact_repo.get_artifact(artifact.artifact_id) is not None
    assert len(await artifact_repo.list_links_for_artifact(artifact.artifact_id)) == 1
    assert Path(artifact.file_path).exists()
    meeting = await repo.get_meeting("mtg-cleanup-crash")
    assert meeting is not None
    assert meeting.state == "finalized"
    assert meeting.minutes_json is not None
    runs = client.get("/workflows/runs?meeting_id=mtg-cleanup-crash").json()
    cleanup_run = next(item for item in runs if item["kind"] == "meeting.outputs.clear")
    assert cleanup_run["state"] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("clear_minutes", [True, False])
async def test_repeated_output_cleanup_does_not_rewrite_minutes_generation(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    clear_minutes: bool,
) -> None:
    meeting_id = f"mtg-cleanup-repeat-{clear_minutes}"
    await _seed_meeting(
        repo,
        meeting_id,
        title="幂等清理",
        started_at=datetime(2026, 5, 28, 12, 10, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "幂等清理",
            "duration_sec": 60,
            "summary": "不得重复推进 generation",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    payload = {"artifact_ids": [], "clear_minutes": clear_minutes}
    first = client.request("DELETE", f"/meetings/{meeting_id}/outputs", json=payload)
    assert first.status_code == 200
    after_first = await repo.get_meeting(meeting_id)
    assert after_first is not None
    first_runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    first_cleanup = next(item for item in first_runs if item["kind"] == "meeting.outputs.clear")
    first_events = client.get(f"/workflows/runs/{first_cleanup['run_id']}/events").json()["events"]

    second = client.request("DELETE", f"/meetings/{meeting_id}/outputs", json=payload)
    assert second.status_code == 200
    after_second = await repo.get_meeting(meeting_id)
    assert after_second is not None
    assert after_second.rag_projection_generation == after_first.rag_projection_generation
    assert after_second.minutes_cleared_at == after_first.minutes_cleared_at
    if clear_minutes:
        assert after_second.minutes_json is None
    else:
        assert after_second.minutes_json == after_first.minutes_json
    runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    cleanup_runs = [item for item in runs if item["kind"] == "meeting.outputs.clear"]
    assert len(cleanup_runs) == 1
    assert cleanup_runs[0]["run_id"] == first_cleanup["run_id"]
    assert cleanup_runs[0]["revision"] == first_cleanup["revision"]
    second_events = client.get(f"/workflows/runs/{first_cleanup['run_id']}/events").json()["events"]
    assert len(second_events) == len(first_events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_artifact_after_minutes_clear_does_not_advance_minutes_generation(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
) -> None:
    meeting_id = "mtg-cleanup-new-artifact"
    await _seed_meeting(
        repo,
        meeting_id,
        title="先清纪要后加产物",
        started_at=datetime(2026, 5, 28, 12, 20, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "先清纪要后加产物",
            "duration_sec": 60,
            "summary": "纪要 generation 只能推进一次",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    first = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )
    assert first.status_code == 200
    cleared = await repo.get_meeting(meeting_id)
    assert cleared is not None and cleared.minutes_cleared_at is not None

    artifact = await _seed_artifact_link(
        tmp_path,
        artifact_id="artifact-after-minutes-clear",
        meeting_id=meeting_id,
        artifact_type="txt",
        body=b"new artifact",
    )
    second = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": True},
    )
    assert second.status_code == 200
    assert second.json()["artifact_ids"] == [artifact.artifact_id]
    cleared_again = await repo.get_meeting(meeting_id)
    assert cleared_again is not None
    assert cleared_again.rag_projection_generation == cleared.rag_projection_generation
    assert cleared_again.minutes_cleared_at == cleared.minutes_cleared_at
    runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    cleanup_runs = [item for item in runs if item["kind"] == "meeting.outputs.clear"]
    assert len(cleanup_runs) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_delete_projection_retries_with_same_generation(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "mtg-cleanup-same-generation-retry"
    await _seed_meeting(
        repo,
        meeting_id,
        title="同代重试投影",
        started_at=datetime(2026, 5, 28, 12, 30, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "同代重试投影",
            "duration_sec": 60,
            "summary": "投影失败不能推进 generation",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    rag = get_rag(_settings(tmp_path))
    await rag.ingest_meeting(meeting_id, "同代重试投影", "同代重试投影")
    original_delete = rag.delete
    generations: list[int | None] = []

    async def fail_once(doc_id: str, *, projection_generation: int | None = None) -> None:
        generations.append(projection_generation)
        if len(generations) == 1:
            raise OSError("transient projection failure")
        await original_delete(doc_id, projection_generation=projection_generation)

    monkeypatch.setattr(rag, "delete", fail_once)
    payload = {"artifact_ids": [], "clear_minutes": True}
    first = client.request("DELETE", f"/meetings/{meeting_id}/outputs", json=payload)
    assert first.status_code == 200
    failed = await repo.get_meeting(meeting_id)
    assert failed is not None and failed.rag_projection_state == "delete_failed"

    second = client.request("DELETE", f"/meetings/{meeting_id}/outputs", json=payload)
    assert second.status_code == 200
    recovered = await repo.get_meeting(meeting_id)
    assert recovered is not None and recovered.rag_projection_state == "deleted"
    assert recovered.rag_projection_generation == failed.rag_projection_generation
    assert generations == [failed.rag_projection_generation, failed.rag_projection_generation]
    runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    cleanup_runs = [item for item in runs if item["kind"] == "meeting.outputs.clear"]
    assert len(cleanup_runs) == 1
    assert cleanup_runs[0]["output"]["rag_projection_deleted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_cleanup_error_returns_503_and_same_receipt_retries_target(  # noqa: PLR0915
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "mtg-file-cleanup-retry"
    artifact_id = "artifact-file-cleanup-retry"
    await _seed_meeting(
        repo,
        meeting_id,
        title="文件清理重试",
        started_at=datetime(2026, 5, 28, 12, 40, tzinfo=UTC),
        segments=[],
        minutes_payload={
            "meeting_id": meeting_id,
            "title": "文件清理重试",
            "duration_sec": 60,
            "summary": "文件删除失败必须显式重试",
            "sections": [],
            "decisions": [],
            "todos": [],
            "action_items": [],
        },
    )
    artifact = await _seed_artifact_link(
        tmp_path,
        artifact_id=artifact_id,
        meeting_id=meeting_id,
        artifact_type="txt",
        body=b"delete after retry",
    )
    import app.artifacts.recovery as artifact_recovery

    original_rmtree = artifact_recovery.shutil.rmtree

    def fail_rmtree(_path: Path) -> None:
        raise OSError("simulated artifact directory lock")

    monkeypatch.setattr(artifact_recovery.shutil, "rmtree", fail_rmtree)
    payload = {"artifact_ids": [], "clear_minutes": True}
    first = client.request("DELETE", f"/meetings/{meeting_id}/outputs", json=payload)
    assert first.status_code == 503
    assert first.json()["detail"] == "artifact file cleanup incomplete; retry the request"
    assert "directory lock" not in first.text
    assert Path(artifact.file_path).exists()
    first_runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    cleanup_runs = [item for item in first_runs if item["kind"] == "meeting.outputs.clear"]
    assert len(cleanup_runs) == 1
    assert cleanup_runs[0]["state"] == "succeeded"
    assert cleanup_runs[0]["output"]["post_commit_complete"] is False
    assert artifact_id in cleanup_runs[0]["output"]["file_cleanup_errors"]

    monkeypatch.setattr(artifact_recovery.shutil, "rmtree", original_rmtree)
    service = WorkflowService(_settings(tmp_path), InMemoryEventBus())
    dispatcher = WorkflowDispatcher(service)
    receipt = await service.get_run(cleanup_runs[0]["run_id"])
    assert receipt is not None
    assert receipt.output["file_cleanup_errors"] == {artifact_id: "file cleanup failed"}
    original_replay = meetings_api.replay_artifact_file_cleanup_target
    entered = 0
    both_entered = asyncio.Event()

    async def synchronized_replay(*args: object, **kwargs: object) -> str:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        return await original_replay(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(meetings_api, "replay_artifact_file_cleanup_target", synchronized_replay)
    await asyncio.gather(
        meetings_api._replay_cleanup_receipt_files(dispatcher, _settings(tmp_path), receipt),
        meetings_api._replay_cleanup_receipt_files(dispatcher, _settings(tmp_path), receipt),
    )
    concurrent = await service.get_run(receipt.run_id)
    assert concurrent is not None
    assert concurrent.output["artifacts_deleted"] == 1
    assert concurrent.output["file_cleanup_deleted_ids"] == [artifact_id]
    assert concurrent.output["missing_artifact_ids"] == []
    assert concurrent.output["file_cleanup_errors"] == {}
    assert concurrent.output["post_commit_complete"] is True
    await dispatcher.aclose()

    monkeypatch.setattr(
        meetings_api,
        "replay_artifact_file_cleanup_target",
        original_replay,
    )
    second = client.request("DELETE", f"/meetings/{meeting_id}/outputs", json=payload)
    assert second.status_code == 200
    assert not Path(artifact.file_path).exists()
    second_runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    cleanup_runs = [item for item in second_runs if item["kind"] == "meeting.outputs.clear"]
    assert len(cleanup_runs) == 1
    assert cleanup_runs[0]["output"]["file_cleanup_errors"] == {}
    assert cleanup_runs[0]["output"]["post_commit_complete"] is True
    assert cleanup_runs[0]["output"]["artifacts_deleted"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initial_cleanup_rejects_path_reused_after_domain_commit(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "mtg-initial-cleanup-path-reuse"
    artifact_id = "artifact-initial-cleanup-path-reuse"
    await _seed_meeting(
        repo,
        meeting_id,
        title="路径复用防护",
        started_at=datetime.now(UTC),
        segments=[],
    )
    old = await _seed_artifact_link(
        tmp_path,
        artifact_id=artifact_id,
        meeting_id=meeting_id,
        artifact_type="txt",
        body=b"new owner must survive",
    )
    original_replay = meetings_api.replay_artifact_file_cleanup_target
    replacement_id = "artifact-replacement-owner"
    injected = False

    async def reuse_before_delete(*args: object, **kwargs: object) -> str:
        nonlocal injected
        if not injected:
            injected = True
            artifacts = ArtifactRepository(_settings(tmp_path))
            assert await artifacts.get_artifact(artifact_id) is None
            await artifacts.save_artifact(
                GeneratedArtifact(
                    artifact_id=replacement_id,
                    artifact_type="txt",
                    title="replacement",
                    file_path=old.file_path,
                    mime_type="text/plain",
                    size_bytes=Path(old.file_path).stat().st_size,
                    generation_latency_ms=0,
                    model="test",
                )
            )
        return await original_replay(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(meetings_api, "replay_artifact_file_cleanup_target", reuse_before_delete)
    response = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": False},
    )
    assert response.status_code == 503
    assert Path(old.file_path).read_bytes() == b"new owner must survive"
    replacement = await ArtifactRepository(_settings(tmp_path)).get_artifact(replacement_id)
    assert replacement is not None and replacement.file_path == old.file_path
    runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    [cleanup] = [item for item in runs if item["kind"] == "meeting.outputs.clear"]
    raw_cleanup = await WorkflowService(_settings(tmp_path), InMemoryEventBus()).get_run(
        cleanup["run_id"]
    )
    assert raw_cleanup is not None
    assert raw_cleanup.output["file_cleanup_errors"] == {artifact_id: "cleanup target is protected"}
    assert raw_cleanup.output["post_commit_complete"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initial_cleanup_rejects_symlink_replacement_after_domain_commit(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meeting_id = "mtg-initial-cleanup-symlink"
    artifact_id = "artifact-initial-cleanup-symlink"
    await _seed_meeting(
        repo,
        meeting_id,
        title="符号链接防护",
        started_at=datetime.now(UTC),
        segments=[],
    )
    old = await _seed_artifact_link(
        tmp_path,
        artifact_id=artifact_id,
        meeting_id=meeting_id,
        artifact_type="txt",
        body=b"old",
    )
    build_dir = Path(old.file_path).parent
    outside = tmp_path / "outside-replacement"
    outside.mkdir()
    outside_output = outside / Path(old.file_path).name
    outside_output.write_bytes(b"outside must survive")
    original_replay = meetings_api.replay_artifact_file_cleanup_target
    injected = False

    async def symlink_before_delete(*args: object, **kwargs: object) -> str:
        nonlocal injected
        if not injected:
            injected = True
            assert await ArtifactRepository(_settings(tmp_path)).get_artifact(artifact_id) is None
            shutil.rmtree(build_dir)
            build_dir.symlink_to(outside, target_is_directory=True)
        return await original_replay(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(meetings_api, "replay_artifact_file_cleanup_target", symlink_before_delete)
    response = client.request(
        "DELETE",
        f"/meetings/{meeting_id}/outputs",
        json={"artifact_ids": [], "clear_minutes": False},
    )
    assert response.status_code == 503
    assert build_dir.is_symlink()
    assert outside_output.read_bytes() == b"outside must survive"
    runs = client.get(f"/workflows/runs?meeting_id={meeting_id}").json()
    [cleanup] = [item for item in runs if item["kind"] == "meeting.outputs.clear"]
    raw_cleanup = await WorkflowService(_settings(tmp_path), InMemoryEventBus()).get_run(
        cleanup["run_id"]
    )
    assert raw_cleanup is not None
    assert raw_cleanup.output["file_cleanup_errors"] == {artifact_id: "cleanup target is unsafe"}
    assert raw_cleanup.output["post_commit_complete"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_meeting_finalize_unadoptable_active_key_conflict_is_http_409(
    client: TestClient,
    repo: SQLiteRepository,
    tmp_path: Path,
) -> None:
    meeting_id = "mtg-finalize-conflict-409"
    await _seed_meeting(
        repo,
        meeting_id,
        title="冲突会议",
        started_at=datetime.now(UTC),
        segments=[TranscriptSegment(text="冲突不能变成 500", start_ms=0, end_ms=800)],
    )
    settings = _settings(tmp_path)
    service = get_workflow_service(settings, get_event_bus())
    winner = await service.create_run(
        WorkflowRunCreate(
            kind="rag.query",
            source="conflicting-owner",
            intent_text="invalid owner of meeting finalize active key",
            active_key=f"meeting.finalize:{meeting_id}",
        )
    )

    response = client.post(
        f"/meetings/{meeting_id}/finalize",
        data={"title": "冲突会议"},
    )

    assert response.status_code == 409
    assert "cannot own this meeting finalize" in response.json()["detail"]
    meeting = await repo.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.minutes_status is None
    assert meeting.minutes_generation_run_id is None
    assert (await service.get_run(winner.run_id)) is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_join_consistency_across_endpoints(
    client: TestClient, repo: SQLiteRepository
) -> None:
    """同一个 meeting_id 通过三个 endpoint 拿到的元信息应一致。

    业务目标：用户在前端选中 meeting A → 中右面板显示的转写段数 == list 上
    显示的 n_segments；显示的纪要 title == list.title。这是"数据库关联好"
    用户期望的最低保证。
    """
    t0 = datetime(2026, 5, 28, 9, 0, tzinfo=UTC)
    payload = {
        "meeting_id": "mtg-join",
        "title": "联调测试",
        "duration_sec": 90,
        "speakers": ["说话人1", "说话人2"],
        "summary": "x",
        "sections": [],
        "decisions": [],
        "action_items": [],
        "created_at": "2026-05-28T09:30:00+00:00",
    }
    segs = [
        TranscriptSegment(
            text=f"段{i}",
            start_ms=i * 500,
            end_ms=i * 500 + 400,
            speaker_label=f"说话人{(i % 2) + 1}",
        )
        for i in range(5)
    ]
    await _seed_meeting(
        repo,
        "mtg-join",
        title="联调测试",
        started_at=t0,
        segments=segs,
        minutes_payload=payload,
    )

    list_r = client.get("/meetings")
    assert list_r.status_code == 200
    [item] = [m for m in list_r.json() if m["meeting_id"] == "mtg-join"]

    transcript_r = client.get("/meetings/mtg-join/transcript")
    minutes_r = client.get("/meetings/mtg-join/minutes")

    assert transcript_r.status_code == 200
    assert minutes_r.status_code == 200
    assert len(transcript_r.json()) == item["n_segments"] == 5
    assert minutes_r.json()["title"] == item["title"] == "联调测试"
    assert item["n_speakers"] == 2
    assert item["has_minutes"] is True


@pytest.mark.asyncio
async def teardown_test_deps() -> None:
    """避免单例残留污染相邻 test。"""
    reset_deps_for_test()
    reset_meeting_pipeline()
    await aclose_repository()
