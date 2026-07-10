"""Phase 4 M_meeting_history：会议历史 4 个 GET endpoint 的 DB join 单测。

关注点：
- 一个 meeting_id 能从 SQLite 查出 transcript / minutes / artifacts 三件套
- 列表 endpoint 的计数（n_segments / n_speakers / has_minutes）口径正确
- 404 边界：会议不存在 / 还未生成纪要

不重测 pipeline 本身（test_meeting_pipeline_repo.py 已覆盖）；这里只校 HTTP
层 + repo join 是否一致。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.adapters.repo.migrator import run_migrations
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.deps import (
    aclose_repository,
    get_repository,
    reset_deps_for_test,
)
from app.api.meetings import reset_meeting_pipeline
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.main import create_app
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.meeting import TranscriptSegment
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
