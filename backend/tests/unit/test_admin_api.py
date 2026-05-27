"""admin.py 单测：data-dir / meeting export / speakers reset（P2.5）。

测试结构：
- data-dir：同步 TestClient + override get_settings，验证 breakdown 字段
- export：async（httpx.AsyncClient）+ 真 SQLiteRepository，验证 zip 内容
- speakers/reset：async + 真 SQLiteRepository，验证 speakers 表清空但 segments
  行数保留

为什么 export / reset 用 async：
- 它们需要 SQLiteRepository（aiosqlite），跨事件循环用同一 connection 会出问题。
  统一在 pytest-asyncio 给的 loop 里 init + 调 endpoint，避免循环漂移。
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.adapters.repo.sqlite import SQLiteRepository
from app.api.deps import (
    get_diarizer_singleton,
    get_repository,
    reset_deps_for_test,
)
from app.config import Settings, get_settings
from app.main import create_app
from app.schemas.meeting import TranscriptSegment
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient


def _make_settings(tmp_path: Path) -> Settings:
    """统一构造测试 Settings：禁 diarizer / workspace scan 避免无关副作用。"""
    return Settings(  # type: ignore[call-arg]
        db_path=tmp_path / "echodesk.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag_index",
        skill_executor_build_dir=tmp_path / "skill_build",
        diarizer_enabled=False,
        workspace_scan_on_startup=False,
        _env_file=None,
    )


class _FakeDiarizer:
    """记录 reset() 是否被调用；不实际加载 ECAPA。"""

    def __init__(self) -> None:
        self.reset_called = False
        self.identify_calls = 0

    async def identify(self, audio_bytes: bytes, *, sample_rate: int = 16_000) -> str | None:
        self.identify_calls += 1
        return None

    async def identify_segments(
        self, audio_bytes: bytes, *, sample_rate: int = 16_000
    ) -> list[Any]:
        return []

    async def reset(self) -> None:
        self.reset_called = True


# ───────────────────── 1. data-dir ─────────────────────


@pytest.mark.unit
def test_data_dir_returns_breakdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """5 个 breakdown 项各写 dummy 字节，验证 endpoint 反映正确。

    create_app() 会触发 _setup_logging() 在 logs/ 下创建 backend.log（会写一行
    info 日志），所以 logs 用 >= 而不是严格等值；其它子项 create_app 不碰，
    用严格相等。
    """
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    (tmp_path / "echodesk.db").write_bytes(b"x" * 1024)
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "meetings").mkdir()
    (storage / "meetings" / "m1.json").write_bytes(b"a" * 4096)
    rag = tmp_path / "rag_index"
    rag.mkdir()
    (rag / "index.bin").write_bytes(b"b" * 2048)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "dummy.log").write_bytes(b"c" * 512)
    skill = tmp_path / "skill_build"
    skill.mkdir()
    (skill / "art-1").mkdir()
    (skill / "art-1" / "output.docx").write_bytes(b"d" * 256)

    settings = _make_settings(tmp_path)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    client = TestClient(app)
    r = client.get("/admin/data-dir")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["path"] == str(tmp_path)
    assert body["exists"] is True

    bd = body["breakdown"]
    # 严格相等：endpoint 不写这些
    assert bd["db"] == 1024
    assert bd["storage"] == 4096
    assert bd["rag_index"] == 2048
    assert bd["skill_build"] == 256
    # logs 由 _setup_logging 同时写了 backend.log（首行 info），>= dummy
    assert bd["logs"] >= 512

    # 总和应 >= 各子项之和（顶层还有 logs/backend.log 等）
    assert body["size_bytes"] >= 1024 + 4096 + 2048 + 512 + 256


@pytest.mark.unit
def test_data_dir_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整目录不存在时 exists=False / size_bytes=0 / 各 breakdown=0。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    settings = _make_settings(tmp_path / "nonexistent")
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    client = TestClient(app)
    r = client.get("/admin/data-dir")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exists"] is False
    assert body["size_bytes"] == 0
    for k in ("db", "storage", "rag_index", "logs", "skill_build"):
        assert body["breakdown"][k] == 0


# ───────────────────── 2. meeting export ─────────────────────


@pytest.mark.unit
async def test_export_meeting_returns_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """对预先 seed 的 meeting 拉 export → 200 + zip 内含 3 核心条目。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    repo = SQLiteRepository(tmp_path / "echodesk.db")
    await repo.init()
    try:
        started = datetime(2026, 5, 28, 10, 30, 0, tzinfo=UTC)
        meeting_id = "meeting-abc12345-rest"
        await repo.create_meeting(meeting_id, started_at=started, title="Q3 销售复盘")
        await repo.append_meeting_segment(
            meeting_id,
            TranscriptSegment(
                text="大家好,今天复盘 Q3",
                start_ms=0,
                end_ms=2000,
                speaker_id="spk-1",
                speaker_label="说话人1",
            ),
            captured_at=started,
        )
        await repo.append_meeting_segment(
            meeting_id,
            TranscriptSegment(
                text="销售额超目标 12%",
                start_ms=2000,
                end_ms=4500,
                speaker_id="spk-2",
                speaker_label="说话人2",
            ),
            captured_at=started,
        )
        await repo.upsert_meeting_speaker_label(meeting_id, "spk-1", "Alice")

        settings = _make_settings(tmp_path)
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_repository] = lambda: repo

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(f"/admin/meetings/{meeting_id}/export")

        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        cd = r.headers["content-disposition"]
        assert f"meeting-{meeting_id[:8]}" in cd

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(zf.namelist())
        assert "meeting.json" in names
        assert "transcript.md" in names
        assert "segments.json" in names

        mj = json.loads(zf.read("meeting.json").decode("utf-8"))
        assert mj["id"] == meeting_id
        assert mj["title"] == "Q3 销售复盘"
        assert mj["state"] == "in_meeting"
        assert mj["started_at"].startswith("2026-05-28T10:30:00")
        assert mj["speaker_labels"] == {"spk-1": "Alice"}

        md = zf.read("transcript.md").decode("utf-8")
        assert "Q3 销售复盘" in md  # title heading
        assert "说话人1" in md
        assert "说话人2" in md
        assert "大家好" in md
        assert "销售额超目标" in md

        segs = json.loads(zf.read("segments.json").decode("utf-8"))
        assert len(segs) == 2
        assert segs[0]["text"] == "大家好,今天复盘 Q3"
        assert segs[0]["speaker_id"] == "spk-1"
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_export_meeting_404_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不存在的 meeting_id → 404。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    repo = SQLiteRepository(tmp_path / "echodesk.db")
    await repo.init()
    try:
        settings = _make_settings(tmp_path)
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_repository] = lambda: repo

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/admin/meetings/no-such-id/export")

        assert r.status_code == 404
        assert r.json()["detail"] == "meeting not found"
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_export_meeting_includes_minutes_when_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize 后 meeting.minutes_json 应该被解析进 meeting.json.minutes。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    repo = SQLiteRepository(tmp_path / "echodesk.db")
    await repo.init()
    try:
        started = datetime(2026, 5, 28, 9, 0, 0, tzinfo=UTC)
        meeting_id = "meeting-final-001"
        await repo.create_meeting(meeting_id, started_at=started, title="Final")
        minutes_payload = {
            "summary": "重要纪要",
            "decisions": ["上线 P2.5"],
        }
        await repo.update_meeting_state(
            meeting_id,
            state="finalized",
            finalized_at=started,
            minutes_json=json.dumps(minutes_payload, ensure_ascii=False),
        )

        settings = _make_settings(tmp_path)
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_repository] = lambda: repo

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post(f"/admin/meetings/{meeting_id}/export")
        assert r.status_code == 200

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        mj = json.loads(zf.read("meeting.json").decode("utf-8"))
        assert mj["minutes"] == minutes_payload
        assert mj["state"] == "finalized"
    finally:
        await repo.aclose()


# ───────────────────── 3. speakers reset ─────────────────────


@pytest.mark.unit
async def test_speakers_reset_clears_data_keeps_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed speakers + ambient_segments + meeting_segments + label map →
    POST /admin/speakers/reset → speakers/label map 行数为 0；
    ambient & meeting segments 行数不变但 speaker_id / speaker_label = NULL；
    diarizer.reset() 被调用。
    """
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    repo = SQLiteRepository(tmp_path / "echodesk.db")
    await repo.init()
    try:
        captured = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)

        # seed: 2 speakers
        await repo.upsert_speaker("spk-1", captured_at=captured, label="说话人1")
        await repo.upsert_speaker("spk-2", captured_at=captured, label="说话人2")

        # seed: 1 meeting + 1 segment + 1 label
        await repo.create_meeting("m1", started_at=captured)
        await repo.upsert_meeting_speaker_label("m1", "spk-1", "Alice")
        await repo.append_meeting_segment(
            "m1",
            TranscriptSegment(
                text="hi from meeting",
                start_ms=0,
                end_ms=500,
                speaker_id="spk-1",
                speaker_label="说话人1",
            ),
            captured_at=captured,
        )

        # seed: 3 ambient segments
        for i in range(3):
            await repo.append_ambient_segment(
                audio_ref=f"/tmp/a-{i}.wav",
                text=f"ambient-{i}",
                captured_at=captured,
                speaker_id="spk-1",
                speaker_label="说话人1",
                duration_ms=500,
            )

        # 起点确认
        assert len(await repo.list_speakers()) == 2
        assert await repo.count_ambient_segments() == 3
        assert (await repo.get_meeting_speaker_labels("m1")) == {"spk-1": "Alice"}
        msegs_before = await repo.list_meeting_segments("m1")
        assert len(msegs_before) == 1
        assert msegs_before[0].speaker_label == "说话人1"

        settings = _make_settings(tmp_path)
        diar = _FakeDiarizer()
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_repository] = lambda: repo
        app.dependency_overrides[get_diarizer_singleton] = lambda: diar

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/admin/speakers/reset")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["speakers_deleted"] == 2
        # 3 ambient_segments + 1 meeting_segment 全部被清字段
        assert body["segments_cleared"] == 4
        assert body["diarizer_reset"] is True
        assert diar.reset_called is True

        # 终点：speakers / labels 表为空
        assert await repo.list_speakers() == []
        assert await repo.get_meeting_speaker_labels("m1") == {}

        # transcript 保留：meeting_segments 行数不变，但 speaker 字段 NULL
        msegs_after = await repo.list_meeting_segments("m1")
        assert len(msegs_after) == 1
        assert msegs_after[0].text == "hi from meeting"
        assert msegs_after[0].speaker_id is None
        assert msegs_after[0].speaker_label is None

        # ambient_segments 同理
        assert await repo.count_ambient_segments() == 3
        ambient_rows = await repo.list_ambient_segments(limit=100)
        assert len(ambient_rows) == 3
        for row in ambient_rows:
            assert row.speaker_id is None
            assert row.speaker_label is None
            # text 保留（确认没误删行）
            assert row.text.startswith("ambient-")
    finally:
        await repo.aclose()


@pytest.mark.unit
async def test_speakers_reset_when_already_empty_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空 DB 跑 reset 不该报错；返回 0/0/True。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()

    repo = SQLiteRepository(tmp_path / "echodesk.db")
    await repo.init()
    try:
        settings = _make_settings(tmp_path)
        diar = _FakeDiarizer()
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_repository] = lambda: repo
        app.dependency_overrides[get_diarizer_singleton] = lambda: diar

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/admin/speakers/reset")

        assert r.status_code == 200
        body = r.json()
        assert body == {
            "speakers_deleted": 0,
            "segments_cleared": 0,
            "diarizer_reset": True,
        }
    finally:
        await repo.aclose()
