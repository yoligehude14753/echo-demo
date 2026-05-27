"""admin.py 单测：data-dir / meetings/{id}/export / speakers/reset 三件套。

P2.5（独立产品 Phase 2）：
- data-dir：UI 设置页给用户的"现在占了多少"反馈，breakdown 五项要稳定
- meetings export：用户上交单个会议 zip 给我们做问题定位（含 transcript +
  segments + minutes）；404 / finalized 含 minutes 是回归红线
- speakers reset：清说话人但**保留段历史**（text/audio_ref 不删；speaker
  字段 NULL）；幂等（空库再点不出错）
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
from app.api import admin as admin_mod
from app.api.deps import (
    get_diarizer_singleton,
    get_repository,
    reset_deps_for_test,
)
from app.config import Settings
from app.main import create_app
from app.schemas.meeting import MeetingMinutes, MinutesSection, TranscriptSegment
from fastapi.testclient import TestClient

# ─────────────────────── helpers ───────────────────────


class _FakeDiarizer:
    """记录 reset() 被调过、并提供可断言的 _profiles。

    实际 DiarizerPort 不要求 _profiles 属性，但 admin endpoint 的资产清单
    （任务里"diarizer._profiles.clear()"）暗示在线上是 ECAPADiarizer，
    所以这里复刻 ECAPADiarizer.reset 的语义。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, Any] = {"spk-1": object(), "spk-2": object()}
        self._counter = 2
        self.reset_called = False

    async def reset(self) -> None:
        self._profiles.clear()
        self._counter = 0
        self.reset_called = True

    async def hydrate(self) -> None:
        return None

    async def identify(self, *_a: Any, **_kw: Any) -> str | None:
        return None

    async def identify_segments(self, *_a: Any, **_kw: Any) -> list[Any]:
        return []


def _make_settings(tmp_path: Path) -> Settings:
    """构造一个完全落在 tmp_path 下的 Settings，避免污染 ~/.echodesk。"""
    return Settings(
        db_path=tmp_path / "echodesk.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag_index",
        skill_executor_build_dir=tmp_path / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )


# ─────────────────────── fixtures ───────────────────────


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个 case 用独立 ~/.echodesk/ 影子目录 + 清单例缓存。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _make_settings(tmp_path)


@pytest.fixture
async def repo(settings: Settings) -> SQLiteRepository:
    """已 init 的 SQLite repo（含 P2.4 schema）。"""
    r = SQLiteRepository(settings.db_path)
    await r.init()
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
def fake_diarizer() -> _FakeDiarizer:
    return _FakeDiarizer()


@pytest.fixture
def client(
    settings: Settings,
    repo: SQLiteRepository,
    fake_diarizer: _FakeDiarizer,
) -> TestClient:
    """构造覆盖了 settings / repo / diarizer 的 TestClient。

    走 dependency_overrides 而不是直接修改单例，避免影响其它 unit test。
    """
    app = create_app()
    app.dependency_overrides[admin_mod.get_settings] = lambda: settings
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_diarizer_singleton] = lambda: fake_diarizer
    return TestClient(app)


# ─────────────────────── /admin/data-dir ───────────────────────


@pytest.mark.unit
def test_data_dir_returns_breakdown(client: TestClient, tmp_path: Path) -> None:
    """所有目录在 tmp_path 下；写少量内容，断言 breakdown 字段齐 + 数值合理。"""
    # DB 已通过 fixture 建好（migrator 写了 schema_version 等表）
    (tmp_path / "storage").mkdir(parents=True, exist_ok=True)
    (tmp_path / "storage" / "a.bin").write_bytes(b"x" * 1234)
    (tmp_path / "rag_index").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rag_index" / "idx.json").write_text("{}")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "backend.log").write_bytes(b"l" * 500)
    (tmp_path / "skill_build").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skill_build" / "out.txt").write_bytes(b"o" * 99)

    r = client.get("/admin/data-dir")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == str(tmp_path)
    assert body["exists"] is True
    assert body["size_bytes"] > 0
    bd = body["breakdown"]
    assert set(bd.keys()) == {"db", "storage", "rag_index", "logs", "skill_build"}
    assert bd["db"] > 0
    assert bd["storage"] == 1234
    assert bd["rag_index"] >= 2  # "{}" = 2 bytes
    assert bd["logs"] == 500
    assert bd["skill_build"] == 99


@pytest.mark.unit
def test_data_dir_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整个数据目录不存在时仍返回 200，``exists=False`` + breakdown 全 0。"""
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()
    missing = tmp_path / "never_created" / "echodesk.db"
    settings = Settings(
        db_path=missing,
        storage_dir=missing.parent / "storage",
        rag_index_dir=missing.parent / "rag_index",
        skill_executor_build_dir=missing.parent / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app()
    app.dependency_overrides[admin_mod.get_settings] = lambda: settings
    c = TestClient(app)

    r = c.get("/admin/data-dir")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(missing.parent)
    assert body["exists"] is False
    assert body["size_bytes"] == 0
    assert all(v == 0 for v in body["breakdown"].values())


# ─────────────────────── /admin/meetings/{id}/export ─────────────


@pytest.mark.asyncio
async def _seed_in_meeting(
    repo: SQLiteRepository, meeting_id: str = "m-export-1"
) -> None:
    """种一个 in_meeting 状态的会议 + 几个含 speaker 的 segment。"""
    started = datetime.now(UTC)
    await repo.create_meeting(meeting_id, started_at=started, title="销售周会")
    for i, (text, sid, label) in enumerate(
        [
            ("开会了", "spk-1", "说话人1"),
            ("我同意", "spk-2", "说话人2"),
            ("收到", "spk-1", "说话人1"),
        ]
    ):
        seg = TranscriptSegment(
            text=text, start_ms=i * 1000, end_ms=(i + 1) * 1000,
            speaker_id=sid, speaker_label=label,
        )
        await repo.append_meeting_segment(meeting_id, seg, captured_at=started)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_meeting_returns_zip(
    client: TestClient, repo: SQLiteRepository
) -> None:
    """正常 meeting → 200 + application/zip + 含 meeting.json/transcript.md/segments.json。"""
    await _seed_in_meeting(repo, "m-export-1")

    r = client.post("/admin/meetings/m-export-1/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    cd = r.headers["content-disposition"]
    assert "echodesk-meeting-m-export-1-" in cd

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"meeting.json", "transcript.md", "segments.json"}.issubset(names)

    meeting = json.loads(zf.read("meeting.json"))
    assert meeting["id"] == "m-export-1"
    assert meeting["title"] == "销售周会"
    assert meeting["state"] == "in_meeting"
    # 没 finalize，不应有 minutes
    assert "minutes" not in meeting

    transcript = zf.read("transcript.md").decode("utf-8")
    assert "# 销售周会" in transcript
    assert "说话人1 · 开会了" in transcript
    assert "说话人2 · 我同意" in transcript

    segs = json.loads(zf.read("segments.json"))
    assert len(segs) == 3
    assert segs[0]["text"] == "开会了"
    assert segs[0]["speaker_label"] == "说话人1"


@pytest.mark.unit
def test_export_meeting_404_when_missing(client: TestClient) -> None:
    """meeting 不存在 → 404 with detail。"""
    r = client.post("/admin/meetings/nope-xxx/export")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_meeting_includes_minutes_when_finalized(
    client: TestClient, repo: SQLiteRepository
) -> None:
    """finalized meeting → meeting.json 里 ``minutes`` 字段是解析后的 dict。"""
    meeting_id = "m-export-final"
    await _seed_in_meeting(repo, meeting_id)
    finalized_at = datetime.now(UTC)
    minutes = MeetingMinutes(
        meeting_id=meeting_id,
        title="销售周会",
        duration_sec=180,
        speakers=["说话人1", "说话人2"],
        summary="本周 GMV ↑ 8%",
        sections=[
            MinutesSection(heading="数据", bullets=["GMV +8%", "活跃 +3%"]),
        ],
        decisions=["维持现策略"],
        action_items=["小张 跟进开通新渠道"],
    )
    await repo.update_meeting_state(
        meeting_id,
        state="finalized",
        finalized_at=finalized_at,
        minutes_json=minutes.model_dump_json(),
    )

    r = client.post(f"/admin/meetings/{meeting_id}/export")
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    meeting = json.loads(zf.read("meeting.json"))
    assert meeting["state"] == "finalized"
    assert "minutes" in meeting
    assert meeting["minutes"]["summary"] == "本周 GMV ↑ 8%"
    assert meeting["minutes"]["decisions"] == ["维持现策略"]
    assert meeting["minutes"]["sections"][0]["bullets"] == ["GMV +8%", "活跃 +3%"]
    # raw minutes_json 字段不再单独保留（已展开为 minutes）
    assert "minutes_json" not in meeting


@pytest.mark.unit
@pytest.mark.asyncio
async def test_export_meeting_picks_up_storage_artifacts(
    client: TestClient, repo: SQLiteRepository, settings: Settings
) -> None:
    """storage_dir/meetings/<id>* 文件被复制到 zip 的 artifacts/ 下。"""
    meeting_id = "m-art"
    await _seed_in_meeting(repo, meeting_id)
    art_dir = Path(settings.storage_dir) / "meetings"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / f"{meeting_id}.json").write_text('{"raw": "transcript"}', encoding="utf-8")
    (art_dir / f"{meeting_id}-audio.bin").write_bytes(b"\x00\x01\x02")
    # 不该被带走（不以 meeting_id 起头）
    (art_dir / "other-meeting.json").write_text('{"x": 1}', encoding="utf-8")

    r = client.post(f"/admin/meetings/{meeting_id}/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert f"artifacts/{meeting_id}.json" in names
    assert f"artifacts/{meeting_id}-audio.bin" in names
    assert "artifacts/other-meeting.json" not in names


# ─────────────────────── /admin/speakers/reset ───────────────────


async def _seed_speakers_and_segments(repo: SQLiteRepository) -> None:
    """populate speakers + speaker-labeled segments，让 reset 有事可清。"""
    now = datetime.now(UTC)
    await repo.upsert_speaker("spk-1", captured_at=now, label="说话人1")
    await repo.upsert_speaker("spk-2", captured_at=now, label="说话人2")
    await repo.upsert_speaker("spk-3", captured_at=now, label=None)

    await repo.create_meeting("m-rs", started_at=now, title="reset 测试")
    for i, (sid, label) in enumerate(
        [("spk-1", "说话人1"), ("spk-2", "说话人2"), (None, None)]
    ):
        seg = TranscriptSegment(
            text=f"t{i}", start_ms=i * 1000, end_ms=(i + 1) * 1000,
            speaker_id=sid, speaker_label=label,
        )
        await repo.append_meeting_segment("m-rs", seg, captured_at=now)
    await repo.upsert_meeting_speaker_label("m-rs", "spk-1", "Alice")
    await repo.upsert_meeting_speaker_label("m-rs", "spk-2", "Bob")

    for i, (sid, label) in enumerate(
        [
            ("spk-1", "说话人1"),
            ("spk-2", "说话人2"),
            (None, None),  # 这条 speaker NULL，reset 不应该被计数
            ("spk-3", None),
        ]
    ):
        await repo.append_ambient_segment(
            audio_ref=f"a{i}.wav",
            text=f"amb{i}",
            captured_at=now,
            speaker_id=sid,
            speaker_label=label,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_speakers_reset_clears_data_keeps_segments(
    client: TestClient,
    repo: SQLiteRepository,
    fake_diarizer: _FakeDiarizer,
) -> None:
    """speakers + meeting_speaker_labels 全清；段表 speaker_id/label 置 NULL；
    segment 本身的 text 保留。返回计数与实际一致。"""
    await _seed_speakers_and_segments(repo)

    r = client.post("/admin/speakers/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["speakers_deleted"] == 3
    # meeting: 2 段非 NULL + ambient: 3 段非 NULL = 5
    assert body["segments_cleared"] == 5
    assert body["diarizer_reset"] is True
    assert fake_diarizer.reset_called is True
    assert fake_diarizer._profiles == {}

    # speakers 表已空
    assert await repo.list_speakers() == []

    # meeting_speaker_labels 全清
    assert await repo.get_meeting_speaker_labels("m-rs") == {}

    # meeting_segments：text 保留、speaker 字段全 NULL
    segs = await repo.list_meeting_segments("m-rs")
    assert len(segs) == 3
    assert {s.text for s in segs} == {"t0", "t1", "t2"}
    assert all(s.speaker_id is None and s.speaker_label is None for s in segs)

    # ambient_segments：text 保留、speaker 字段全 NULL
    ambient = await repo.list_ambient_segments(limit=100)
    assert len(ambient) == 4
    assert {a.text for a in ambient} == {"amb0", "amb1", "amb2", "amb3"}
    assert all(a.speaker_id is None and a.speaker_label is None for a in ambient)


@pytest.mark.unit
def test_speakers_reset_when_already_empty_is_idempotent(
    client: TestClient,
    fake_diarizer: _FakeDiarizer,
) -> None:
    """空库 / 没种数据时再点 reset 不应该出错，0/0/diarizer 仍然 reset。"""
    r = client.post("/admin/speakers/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["speakers_deleted"] == 0
    assert body["segments_cleared"] == 0
    assert body["diarizer_reset"] is True
    assert fake_diarizer.reset_called is True


@pytest.mark.unit
def test_speakers_reset_when_db_missing_still_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_diarizer: _FakeDiarizer
) -> None:
    """db 文件根本不存在 → endpoint 仍返回 200 / 0 计数 / diarizer reset。

    防御性：用户头一次开 app（schema migration 没跑过）就点了 reset 也不能崩。
    """
    monkeypatch.setenv("ECHO_USER_DIR", str(tmp_path))
    reset_deps_for_test()
    settings = Settings(
        db_path=tmp_path / "ghost" / "nope.db",
        storage_dir=tmp_path / "storage",
        rag_index_dir=tmp_path / "rag_index",
        skill_executor_build_dir=tmp_path / "skill_build",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app()
    app.dependency_overrides[admin_mod.get_settings] = lambda: settings
    app.dependency_overrides[get_diarizer_singleton] = lambda: fake_diarizer
    c = TestClient(app)

    r = c.post("/admin/speakers/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["speakers_deleted"] == 0
    assert body["segments_cleared"] == 0
    assert body["diarizer_reset"] is True
