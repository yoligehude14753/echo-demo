"""管理后台 API：数据目录信息 / 会议导出 / 说话人重置（P2.5）。

设计：
- 三个端点全部围绕 ~/.echodesk/ 用户数据目录展开
- 不引入新的依赖、不动 sqlite.py 的 query；如需 SQL 直接抓单例 conn
- export 走 FileResponse + BackgroundTask 清理 tmp file

为什么不复用 `app/tools/reset_speakers.py`：
- 那是 CLI 工具，用同步 sqlite3 直连 DB；后端运行时连接已开 WAL，不能两边
  同时写。此处复用 sqlite 单例的 lock + 连接，保证一致性
- 这里只清 speaker（保留 transcript），与 CLI 的 --include-segments 语义相反
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.adapters.repo.sqlite import SQLiteRepository
from app.api.deps import get_diarizer_singleton, get_repository
from app.config import Settings, get_settings
from app.config_io import load_user_config_json, user_config_path, write_user_config_json
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort

logger = logging.getLogger("echodesk.admin")
router = APIRouter(tags=["admin"])


# ── 工具函数 ─────────────────────────────────────────────────────────


def _dir_size_bytes(path: Path) -> int:
    """递归累加目录占用字节；不存在 → 0；权限错误 → 跳过单条不抛。

    os.scandir 比 pathlib 的 rglob 快很多（在 storage/ambient/YYYY-MM-DD 这种
    深目录上差距 5-10×）。
    """
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    stack: list[Path] = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _data_dir(settings: Settings) -> Path:
    """所有用户数据的根目录 = db_path 所在目录（与 install-backend.sh 对齐）。"""
    return Path(settings.db_path).expanduser().parent


def _segments_to_markdown(
    segments: list[dict[str, object]],
    *,
    title: str | None = None,
) -> str:
    """把 segments 拼成可读 markdown：`- speaker_label · text`。

    缺 speaker_label 用 "(未识别)" 占位；空 text 行跳过。
    """
    lines: list[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for seg in segments:
        raw_text = seg.get("text") or ""
        text = str(raw_text).strip()
        if not text:
            continue
        label = seg.get("speaker_label") or "(未识别)"
        lines.append(f"- {label} · {text}")
    if not lines or (title and len(lines) == 2):  # 只有标题没内容
        lines.append("(空转写)")
    return "\n".join(lines) + "\n"


# ── 1. GET /admin/data-dir ────────────────────────────────────────


@router.get("/data-dir")
async def get_data_dir(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """返回 ~/.echodesk/ 目录信息 + 子项 size breakdown。

    breakdown 子项：
    - db: echodesk.db 单文件大小
    - storage: storage/（会议转写 + ambient wav）
    - rag_index: rag_index/（BM25 索引）
    - logs: logs/（rotating backend log）
    - skill_build: skill_build/（生成产物 cache）
    """
    root = _data_dir(settings)
    exists = root.exists()

    db_path = Path(settings.db_path).expanduser()
    storage = Path(settings.storage_dir).expanduser()
    rag_index = Path(settings.rag_index_dir).expanduser()
    logs = root / "logs"
    skill_build = Path(settings.skill_executor_build_dir).expanduser()

    breakdown = {
        "db": _dir_size_bytes(db_path),
        "storage": _dir_size_bytes(storage),
        "rag_index": _dir_size_bytes(rag_index),
        "logs": _dir_size_bytes(logs),
        "skill_build": _dir_size_bytes(skill_build),
    }
    size_bytes = _dir_size_bytes(root) if exists else 0

    return {
        "path": str(root),
        "exists": exists,
        "size_bytes": size_bytes,
        "breakdown": breakdown,
    }


# ── 2. POST /admin/meetings/{meeting_id}/export ──────────────────


def _safe_zip_name(filename: str) -> str:
    """去掉路径分隔符避免 zip 写出 ../../etc/passwd。"""
    return os.path.basename(filename).replace("/", "_").replace("\\", "_") or "file"


def _build_meeting_zip(
    zip_path: Path,
    *,
    meeting_payload: dict[str, object],
    transcript_md: str,
    segments_payload: list[dict[str, object]],
    storage: Path,
    meeting_id: str,
    raw_transcript_ref: str | None,
) -> None:
    """实际把 zip 写出：核心三件 + 可选 audio/artifacts（best-effort）。"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "meeting.json",
            json.dumps(meeting_payload, ensure_ascii=False, indent=2),
        )
        zf.writestr("transcript.md", transcript_md)
        zf.writestr(
            "segments.json",
            json.dumps(segments_payload, ensure_ascii=False, indent=2),
        )

        # raw_transcript_ref（finalize 落盘的逐字稿 json）best-effort 带过来
        if raw_transcript_ref:
            ref_path = Path(raw_transcript_ref).expanduser()
            if ref_path.exists() and ref_path.is_file():
                try:
                    zf.write(ref_path, arcname="transcript.raw.json")
                except OSError as e:
                    logger.warning("export: 跳过 raw_transcript %s: %s", ref_path, e)

        # audio: meeting 当前没有强绑定的 audio_ref（ambient 链路写在 ambient_segments），
        # 但若 storage/ambient/ 下能找到与 meeting 同期 wav 就稍后再补；P2.5 阶段只
        # 把 storage/meetings/{id}/audio/* 当做最小约定（forward-compatible），不存在
        # 就跳过，不 fail。
        audio_dir = storage / "meetings" / meeting_id / "audio"
        if audio_dir.exists() and audio_dir.is_dir():
            for f in audio_dir.glob("*.wav"):
                try:
                    zf.write(f, arcname=f"audio/{_safe_zip_name(f.name)}")
                except OSError as e:
                    logger.warning("export: 跳过 audio %s: %s", f, e)

        # artifacts: 同上，约定 storage/meetings/{id}/artifacts/* 是该 meeting 的产物
        artifacts_dir = storage / "meetings" / meeting_id / "artifacts"
        if artifacts_dir.exists() and artifacts_dir.is_dir():
            for f in artifacts_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    zf.write(f, arcname=f"artifacts/{_safe_zip_name(f.name)}")
                except OSError as e:
                    logger.warning("export: 跳过 artifact %s: %s", f, e)


@router.post("/meetings/{meeting_id}/export")
async def export_meeting(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """把指定会议导出为 zip 返回；缺失 meeting → 404。

    zip 内容固定 3 件 + best-effort 2 件：
      meeting.json     - meeting record + 解析后的 minutes（若有）
      transcript.md    - segments 拼成的可读文本
      segments.json    - 完整 raw segments
      transcript.raw.json  - finalize 时落盘的逐字稿（若有）
      audio/*.wav      - storage/meetings/{id}/audio/ 下的 wav（若有）
      artifacts/*      - storage/meetings/{id}/artifacts/ 下的产物（若有）
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    segments = await repository.list_meeting_segments(meeting_id)
    labels = await repository.get_meeting_speaker_labels(meeting_id)

    minutes_obj: object | None = None
    if meeting.minutes_json:
        try:
            minutes_obj = json.loads(meeting.minutes_json)
        except json.JSONDecodeError:
            minutes_obj = None

    meeting_payload: dict[str, object] = {
        "id": meeting.id,
        "title": meeting.title,
        "state": meeting.state,
        "started_at": meeting.started_at.isoformat(),
        "ended_at": meeting.ended_at.isoformat() if meeting.ended_at else None,
        "finalized_at": (meeting.finalized_at.isoformat() if meeting.finalized_at else None),
        "auto_started": meeting.auto_started,
        "speaker_labels": labels,
        "minutes": minutes_obj,
        "raw_transcript_ref": meeting.raw_transcript_ref,
    }
    segments_payload = [s.model_dump() for s in segments]
    transcript_md = _segments_to_markdown(
        segments_payload, title=meeting.title or f"Meeting {meeting_id[:8]}"
    )

    storage = Path(settings.storage_dir).expanduser()

    # 手动 close 让 FileResponse + BackgroundTask 接管生命周期；ruff SIM115
    # 不识别这种"先建文件名再交给下游"的合法模式
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f"echodesk-export-{meeting_id[:8]}-",
        suffix=".zip",
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        _build_meeting_zip(
            tmp_path,
            meeting_payload=meeting_payload,
            transcript_md=transcript_md,
            segments_payload=segments_payload,
            storage=storage,
            meeting_id=meeting_id,
            raw_transcript_ref=meeting.raw_transcript_ref,
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    started_slug = meeting.started_at.strftime("%Y%m%d-%H%M%S")
    filename = f"meeting-{meeting_id[:8]}-{started_slug}.zip"

    def _cleanup() -> None:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    return FileResponse(
        path=tmp_path,
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(_cleanup),
    )


# ── 3. POST /admin/speakers/reset ────────────────────────────────


@router.post("/speakers/reset")
async def reset_speakers(
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    diarizer: Annotated[DiarizerPort, Depends(get_diarizer_singleton)],
) -> dict[str, object]:
    """清空 speaker 数据但保留 transcript（speakers 表 + label map 删行；
    segments 的 speaker_id/speaker_label 字段 UPDATE 为 NULL，**不删行**）。

    与 `app/tools/reset_speakers.py` 的区别：CLI 工具是 dry-run + 可选
    --include-segments；这里固定语义 = 只清说话人。
    """
    if not isinstance(repository, SQLiteRepository):
        raise HTTPException(
            status_code=500,
            detail="non-sqlite repository does not support speaker reset",
        )

    # 复用 sqlite 单例的 lock 避免与主链路双连接竞争；直接抓 _conn 是有意为之
    repo = repository
    async with repo._lock:
        conn = repo._require_conn()

        cur = await conn.execute("SELECT COUNT(*) FROM speakers")
        row = await cur.fetchone()
        await cur.close()
        speakers_deleted = int(row[0]) if row else 0

        cleared_count = 0
        cur = await conn.execute(
            "SELECT COUNT(*) FROM ambient_segments "
            "WHERE speaker_id IS NOT NULL OR speaker_label IS NOT NULL"
        )
        row = await cur.fetchone()
        await cur.close()
        cleared_count += int(row[0]) if row else 0
        cur = await conn.execute(
            "SELECT COUNT(*) FROM meeting_segments "
            "WHERE speaker_id IS NOT NULL OR speaker_label IS NOT NULL"
        )
        row = await cur.fetchone()
        await cur.close()
        cleared_count += int(row[0]) if row else 0

        await conn.execute("DELETE FROM speakers")
        await conn.execute("DELETE FROM meeting_speaker_labels")
        await conn.execute("UPDATE ambient_segments SET speaker_id = NULL, speaker_label = NULL")
        await conn.execute("UPDATE meeting_segments SET speaker_id = NULL, speaker_label = NULL")
        await conn.commit()

    diarizer_reset_ok = True
    try:
        await diarizer.reset()
    except Exception as e:
        logger.warning("diarizer.reset() failed: %s", e)
        diarizer_reset_ok = False

    logger.info(
        "admin: speaker reset done (speakers_deleted=%d segments_cleared=%d diarizer_reset=%s)",
        speakers_deleted,
        cleared_count,
        diarizer_reset_ok,
    )
    return {
        "speakers_deleted": speakers_deleted,
        "segments_cleared": cleared_count,
        "diarizer_reset": diarizer_reset_ok,
    }


# ── 4. 远端 endpoint 配置（P3.2） ─────────────────────────────────
#
# 让用户在 SettingsPanel 直接改 LLM/STT/TTS/Tavily 的 base_url + key，
# 不需要再编辑 ~/.echodesk/config.json 文件。
#
# 安全策略：
# - 只允许白名单内字段可读 / 可写，永远不暴露 db_path / workspace_dirs
#   等本地路径（即便 GET 也不返回）
# - GET 返回的 key 字段（标记 sensitive=True）做脱敏；不脱敏的 base_url
#   等明文返回，让用户能看到改了没改
# - 写入只调 write_user_config_json（pydantic-settings 启动期 load），
#   响应里 restart_required=true 提示用户重启 backend 才能让单例生效
# - 不在这里调 BackendSupervisor 重启；用户走 StatusBar「重启 backend」按钮
#   触发，避免本 endpoint 自身被切断


# 字段元信息：name → (settings_attr, sensitive)
# 顺序与 SettingsPanel UI 一致
_REMOTE_FIELDS: list[tuple[str, str, bool]] = [
    # (config key in ~/.echodesk/config.json, Settings 属性, 是否 sensitive)
    # 网关模式（推荐）：填这两项即开启，所有上游走 echo-gateway，无需配下面的直连项。
    # echo_gateway_token 是发给用户的「访问 key」——拿到 key 的人填进来就能用。
    ("echo_gateway_url", "echo_gateway_url", False),
    ("echo_gateway_token", "echo_gateway_token", True),
    ("llm_main_base_url", "llm_main_base_url", False),
    ("yunwu_open_key", "yunwu_open_key", True),
    ("llm_fast_base_url", "llm_fast_base_url", False),
    ("stt_firered_url", "stt_firered_url", False),
    ("tts_qwen3_url", "tts_qwen3_url", False),
    ("tts_qwen3_voice", "tts_qwen3_voice", False),
    ("tavily_api_key", "tavily_api_key", True),
]

_ALLOWED_KEYS = {f[0] for f in _REMOTE_FIELDS}


def _mask_secret(value: str) -> str:
    """key 脱敏：留首 4 / 末 4，中间 ***；短 key（≤8）保留首末各 1。空字符串直接空。"""
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[0]}***{value[-1]}"
    return f"{value[:4]}***{value[-4:]}"


class RemoteFieldDTO(BaseModel):
    key: str
    value: str = Field(description="脱敏后的明文（sensitive=False 时即原值）")
    sensitive: bool
    source: str = Field(description="default | user（来自 ~/.echodesk/config.json）")


class RemoteSettingsDTO(BaseModel):
    config_path: str = Field(description="~/.echodesk/config.json 的绝对路径")
    fields: list[RemoteFieldDTO]


class RemoteSettingsPatch(BaseModel):
    """PATCH body：dict 形式，只接受白名单字段；其它键直接 422。"""

    updates: dict[str, str] = Field(description="key → new value 映射；空字符串 = 清空该字段")


@router.get("/settings/remote", response_model=RemoteSettingsDTO)
def get_remote_settings(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RemoteSettingsDTO:
    """读当前生效的远端 endpoint + key（脱敏）。

    生效值 = pydantic-settings 三层合并结果（env > user.json > default）。
    `source = "user"` 当且仅当 user.json 出现了该 key（不管值是不是和 default 一样）。
    """
    user_overrides = load_user_config_json()
    items: list[RemoteFieldDTO] = []
    for key, attr, sensitive in _REMOTE_FIELDS:
        raw_value = getattr(settings, attr, "")
        display = _mask_secret(raw_value) if sensitive else str(raw_value)
        items.append(
            RemoteFieldDTO(
                key=key,
                value=display,
                sensitive=sensitive,
                source="user" if key in user_overrides else "default",
            )
        )
    return RemoteSettingsDTO(
        config_path=str(user_config_path()),
        fields=items,
    )


@router.patch("/settings/remote")
def patch_remote_settings(body: RemoteSettingsPatch) -> dict[str, object]:
    """合并写入到 ~/.echodesk/config.json；非白名单 key 整体 422。

    返回：{written_keys: [...], skipped_keys: [...], restart_required: True}
    """
    unknown = set(body.updates.keys()) - _ALLOWED_KEYS
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"未知配置项：{sorted(unknown)}（允许：{sorted(_ALLOWED_KEYS)}）",
        )

    # 空字符串保留为空字符串而非删除：用户想清掉 key 时显式置空即可
    sanitized = {k: v for k, v in body.updates.items() if k in _ALLOWED_KEYS}
    if not sanitized:
        return {
            "written_keys": [],
            "skipped_keys": [],
            "restart_required": False,
            "config_path": str(user_config_path()),
        }

    write_user_config_json(sanitized)
    logger.info("admin: remote settings updated keys=%s", sorted(sanitized.keys()))

    return {
        "written_keys": sorted(sanitized.keys()),
        "skipped_keys": [],
        "restart_required": True,
        "config_path": str(user_config_path()),
    }


__all__ = ["router"]
