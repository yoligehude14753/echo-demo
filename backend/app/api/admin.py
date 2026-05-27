"""管理 endpoint：数据目录概况 / 单会议导出 / 重置说话人。

P2.5（独立产品 Phase 2）：UI 设置页 + 客服排障入口。

路由统一以 ``/admin`` 前缀挂载（``main.py`` 里 ``app.include_router(admin_router,
prefix='/admin')``），因此本文件内路由声明**不**再带 ``/admin`` 前缀。

设计取舍：
- 写库路径（``/speakers/reset``）走独立 ``sqlite3`` 连接（参考
  ``app/tools/reset_speakers.py``），不与现有 async repo 抢 ``_lock`` —— 隔离影响面、
  沿用 WAL 多写者协调；亦不需要为此扩展 ``RepositoryPort``。
- ``/meetings/{id}/export`` 复用 ``RepositoryPort.get_meeting`` /
  ``list_meeting_segments`` 只读 API，不破坏 repo 现状。
- 不做鉴权（EchoDesk 是本地桌面 app，仅监听 127.0.0.1，鉴权交给 OS 帐号）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.api.deps import get_diarizer_singleton, get_repository
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort

logger = logging.getLogger("echodesk.admin")

router = APIRouter(tags=["admin"])


def _safe_unlink(path: Path) -> None:
    """zip tmp 文件清理；删除失败不抛（OS 偶尔会延迟释放句柄）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("admin: failed to remove %s: %s", path, e)


def _dir_size_bytes(p: Path) -> int:
    """递归累加目录下所有文件 size_bytes；不存在 / 不可读 → 0。

    单文件直接 ``stat().st_size``；遇到无权限 / 损坏链接 silently skip
    （诊断、UI 展示不应被一只损坏链接绊住）。
    """
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


@router.get("/data-dir", summary="用户数据目录概况")
async def get_data_dir(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """返回 ``~/.echodesk`` 当前规模与各子目录拆分。

    breakdown 字段：``db``（``echodesk.db`` 单文件）/ ``storage`` /
    ``rag_index`` / ``logs`` / ``skill_build``。UI 设置页用来给用户
    "我现在的数据占了多少"的可见反馈。

    返回 ``exists=False`` 时其它字段保持 0，前端可以稳定渲染（不需要二次
    判空）。
    """
    db_path = Path(settings.db_path).expanduser()
    root = db_path.parent
    storage_dir = Path(settings.storage_dir).expanduser()
    rag_dir = Path(settings.rag_index_dir).expanduser()
    logs_dir = root / "logs"
    skill_dir = Path(settings.skill_executor_build_dir).expanduser()

    breakdown = {
        "db": _dir_size_bytes(db_path),
        "storage": _dir_size_bytes(storage_dir),
        "rag_index": _dir_size_bytes(rag_dir),
        "logs": _dir_size_bytes(logs_dir),
        "skill_build": _dir_size_bytes(skill_dir),
    }
    return {
        "path": str(root),
        "exists": root.exists(),
        "size_bytes": _dir_size_bytes(root) if root.exists() else 0,
        "breakdown": breakdown,
    }


@router.post("/meetings/{meeting_id}/export", summary="导出单个会议（zip）")
async def export_meeting(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    """打包 meeting metadata + transcript + segments + artifacts。

    zip 结构（无目录前缀，直接在 zip 根）：

        meeting.json           完整 MeetingRecord JSON（含 ``minutes`` 解析后字典；
                               若 ``minutes_json`` 不可解析则带 ``_parse_error`` 标记）
        transcript.md          ``[mm:ss] speaker_label · text`` 每行一段；
                               首行是 ``# <title>``
        segments.json          所有 ``TranscriptSegment`` 原始数组
        artifacts/<filename>   从 ``storage_dir/meetings/`` 复制的所有以 meeting_id
                               开头的产物文件（例如 ``<id>.json`` 完整转写）

    404 if meeting 不存在；下载后 BackgroundTask 删除 tmp。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")

    segs = await repository.list_meeting_segments(meeting_id)

    # 拼会议元信息（含 minutes 如已 finalized）
    meeting_dict: dict[str, Any] = meeting.model_dump(mode="json")
    minutes_json = meeting_dict.pop("minutes_json", None)
    if minutes_json:
        try:
            meeting_dict["minutes"] = json.loads(minutes_json)
        except json.JSONDecodeError:
            meeting_dict["minutes"] = {"_parse_error": True, "raw": minutes_json[:1000]}

    transcript_lines: list[str] = []
    for s in segs:
        ts = f"[{s.start_ms // 1000:02d}:{(s.start_ms // 1000) % 60:02d}]"
        label = s.speaker_label or s.speaker_id or "未知"
        transcript_lines.append(f"{ts} {label} · {s.text}")
    transcript_md = f"# {meeting.title or meeting_id}\n\n" + "\n".join(transcript_lines)

    segments_arr = [s.model_dump() for s in segs]

    storage_meetings = Path(settings.storage_dir).expanduser() / "meetings"

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    with tempfile.NamedTemporaryFile(
        prefix=f"echodesk-meeting-{meeting_id}-", suffix=".zip", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)

    def _write_zip() -> None:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "meeting.json",
                json.dumps(meeting_dict, indent=2, ensure_ascii=False),
            )
            zf.writestr("transcript.md", transcript_md)
            zf.writestr(
                "segments.json",
                json.dumps(segments_arr, indent=2, ensure_ascii=False),
            )
            if storage_meetings.exists():
                for f in storage_meetings.iterdir():
                    try:
                        if not f.is_file() or not f.name.startswith(meeting_id):
                            continue
                        zf.write(f, arcname=f"artifacts/{f.name}")
                    except OSError as e:
                        logger.warning(
                            "admin: skip artifact %s for meeting %s: %s",
                            f,
                            meeting_id,
                            e,
                        )

    await asyncio.to_thread(_write_zip)

    fname = f"echodesk-meeting-{meeting_id}-{ts}.zip"
    return FileResponse(
        path=str(tmp_path),
        media_type="application/zip",
        filename=fname,
        background=BackgroundTask(_safe_unlink, tmp_path),
    )


@router.post("/speakers/reset", summary="清空 speakers + 段表 speaker 字段")
async def reset_speakers(
    diarizer: Annotated[DiarizerPort, Depends(get_diarizer_singleton)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """清说话人注册表 + 段表 speaker_id/label，但**保留 segment 本身**。

    具体操作：
    - ``DELETE FROM speakers``
    - ``DELETE FROM meeting_speaker_labels``
    - ``UPDATE ambient_segments SET speaker_id=NULL, speaker_label=NULL``
    - ``UPDATE meeting_segments  SET speaker_id=NULL, speaker_label=NULL``
    - ``diarizer.reset()`` 清内存 ``_profiles`` + ``_counter``

    不删 ambient_segments / meeting_segments 的 ``text`` / ``audio_ref`` —— 用户
    依然能在历史里看到 STT 文本，只是没了 speaker 归属。

    与 ``app/tools/reset_speakers.py`` CLI 的差异：CLI 可选 ``--include-segments``
    直接 DELETE 段；本 endpoint 不允许删段（产品决策：UI 触发的 reset 不丢历史）。

    返回：
      ``speakers_deleted``: ``speakers`` 表删除前的行数
      ``segments_cleared``: 受影响段总数 = ambient + meeting（被改成 NULL 的行）
      ``diarizer_reset``:   diarizer 内存 ``_profiles`` 是否成功清空
    """
    # 直接读 settings.db_path 而不是经 RepositoryPort：
    # 1) 写操作需 DELETE/UPDATE，RepositoryPort 当前不暴露这些，
    #    不想为 admin 单点扩 port。
    # 2) 用独立 sqlite3 连接 + asyncio.to_thread，沿用 WAL 多写者协调，
    #    不抢 SQLiteRepository._lock。
    db_path = Path(settings.db_path).expanduser()
    if not db_path.exists():
        # idempotent：空库 / 还没启动过 → 0/0/diarizer reset 仍执行
        speakers_deleted = 0
        segments_cleared = 0
    else:
        def _reset_db() -> tuple[int, int]:
            con = sqlite3.connect(str(db_path))
            try:
                speakers_before = int(
                    con.execute("SELECT COUNT(*) FROM speakers").fetchone()[0]
                )
                ambient_affected = int(
                    con.execute(
                        "SELECT COUNT(*) FROM ambient_segments "
                        "WHERE speaker_id IS NOT NULL OR speaker_label IS NOT NULL"
                    ).fetchone()[0]
                )
                meeting_affected = int(
                    con.execute(
                        "SELECT COUNT(*) FROM meeting_segments "
                        "WHERE speaker_id IS NOT NULL OR speaker_label IS NOT NULL"
                    ).fetchone()[0]
                )
                con.execute("DELETE FROM speakers")
                con.execute("DELETE FROM meeting_speaker_labels")
                con.execute(
                    "UPDATE ambient_segments "
                    "SET speaker_id = NULL, speaker_label = NULL"
                )
                con.execute(
                    "UPDATE meeting_segments "
                    "SET speaker_id = NULL, speaker_label = NULL"
                )
                con.commit()
                return speakers_before, ambient_affected + meeting_affected
            finally:
                con.close()

        speakers_deleted, segments_cleared = await asyncio.to_thread(_reset_db)

    diarizer_reset = False
    reset_fn = getattr(diarizer, "reset", None)
    if reset_fn is not None:
        try:
            await reset_fn()
            diarizer_reset = True
        except Exception as e:
            logger.warning("admin: diarizer reset failed: %s", e)

    logger.info(
        "admin speakers/reset: speakers_deleted=%d segments_cleared=%d diarizer_reset=%s",
        speakers_deleted,
        segments_cleared,
        diarizer_reset,
    )
    return {
        "speakers_deleted": speakers_deleted,
        "segments_cleared": segments_cleared,
        "diarizer_reset": diarizer_reset,
    }


__all__ = ["router"]
