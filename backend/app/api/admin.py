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
import zipfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.adapters.repo.sqlite import SQLiteRepository
from app.api.deps import (
    get_artifact_repository,
    get_diarizer_singleton,
    get_repository,
    get_workflow_dispatcher,
)
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.config_io import load_user_config_json, user_config_path, write_user_config_json
from app.ports.diarizer import DiarizerPort
from app.ports.repository import RepositoryPort
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import current_principal
from app.security.headers import PRIVATE_NO_STORE_HEADERS
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher, WorkflowExecutionError

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
    raw_transcript_path: Path | None,
    artifact_files: list[tuple[Path, str]],
    artifact_manifest: list[dict[str, object]],
) -> None:
    """Write authoritative meeting data and registered artifact files."""
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
        if raw_transcript_path is not None:
            try:
                zf.write(raw_transcript_path, arcname="transcript.raw.json")
            except OSError as e:
                logger.warning("export: skip registered raw transcript: %s", e)

        for artifact_path, archive_name in artifact_files:
            try:
                zf.write(artifact_path, arcname=f"artifacts/{archive_name}")
            except OSError as e:
                logger.warning("export: skip registered artifact %s: %s", archive_name, e)

        zf.writestr(
            "export-manifest.json",
            json.dumps(
                {
                    "artifacts": artifact_manifest,
                    "audio": {
                        "included": False,
                        "reason": "meeting audio is not linked by the current storage schema",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


def _allowed_export_file(settings: Settings, raw_path: str) -> Path | None:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    allowed_roots = (
        Path(settings.storage_dir).expanduser().resolve(),
        Path(settings.skill_executor_build_dir).expanduser().resolve(),
    )
    if not resolved.is_file():
        return None
    is_allowed = any(root == resolved or root in resolved.parents for root in allowed_roots)
    return resolved if is_allowed else None


async def _prepare_meeting_export(
    repository: RepositoryPort,
    settings: Settings,
    artifact_repo: ArtifactRepository,
    meeting_id: str,
    target: Path,
) -> str:
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise KeyError(meeting_id)
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
        "finalized_at": meeting.finalized_at.isoformat() if meeting.finalized_at else None,
        "auto_started": meeting.auto_started,
        "speaker_labels": labels,
        "minutes": minutes_obj,
        "raw_transcript_available": bool(meeting.raw_transcript_ref),
    }
    segments_payload = [segment.model_dump() for segment in segments]
    transcript_md = _segments_to_markdown(
        segments_payload,
        title=meeting.title or f"Meeting {meeting_id[:8]}",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    artifact_files: list[tuple[Path, str]] = []
    artifact_manifest: list[dict[str, object]] = []
    for artifact in await artifact_repo.list_meeting_artifacts(meeting_id):
        artifact_path = _allowed_export_file(settings, artifact.file_path)
        included = artifact_path is not None
        archive_name = (
            f"{_safe_zip_name(artifact.artifact_id)}-{_safe_zip_name(artifact_path.name)}"
            if artifact_path is not None
            else None
        )
        if artifact_path is not None and archive_name is not None:
            artifact_files.append((artifact_path, archive_name))
        artifact_manifest.append(
            {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "size_bytes": artifact.size_bytes,
                "included": included,
                "archive_name": archive_name,
            }
        )
    raw_transcript_path = (
        _allowed_export_file(settings, meeting.raw_transcript_ref)
        if meeting.raw_transcript_ref
        else None
    )
    temp = target.with_suffix(target.suffix + ".tmp")
    try:
        _build_meeting_zip(
            temp,
            meeting_payload=meeting_payload,
            transcript_md=transcript_md,
            segments_payload=segments_payload,
            raw_transcript_path=raw_transcript_path,
            artifact_files=artifact_files,
            artifact_manifest=artifact_manifest,
        )
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    started_slug = meeting.started_at.strftime("%Y%m%d-%H%M%S")
    return f"meeting-{meeting_id[:8]}-{started_slug}.zip"


def bind_meeting_export_workflow_handler(
    dispatcher: WorkflowDispatcher,
    repository: RepositoryPort,
    settings: Settings,
    artifact_repo: ArtifactRepository,
) -> None:
    principal = current_principal()
    scope = (principal.tenant_id, principal.owner_id)

    async def handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload["meeting_id"])
        target = (
            Path(settings.storage_dir).expanduser() / "exports" / f"meeting-{context.run_id}.zip"
        )
        filename = await _prepare_meeting_export(
            repository,
            settings,
            artifact_repo,
            meeting_id,
            target,
        )
        return {
            "meeting_id": meeting_id,
            "path": str(target),
            "filename": filename,
            "size_bytes": target.stat().st_size,
        }

    dispatcher.registry.register(
        "meeting.export",
        handler,
        scope=scope,
        replace=True,
    )


@router.post("/meetings/{meeting_id}/export")
async def export_meeting(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> FileResponse:
    """把指定会议导出为 zip 返回；缺失 meeting → 404。

    zip 内容固定 4 件 + best-effort 2 类：
      meeting.json     - meeting record + 解析后的 minutes（若有）
      transcript.md    - segments 拼成的可读文本
      segments.json    - 完整 raw segments
      export-manifest.json - 导出的产物清单与音频可用性说明
      transcript.raw.json  - finalize 时落盘的逐字稿（若有）
      artifacts/*      - 通过 artifact_links 明确关联且位于允许目录的产物

    当前 schema 未建立会议音频关联，因此不猜测或扫描音频目录。
    """
    if await repository.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    bind_meeting_export_workflow_handler(dispatcher, repository, settings, artifact_repo)
    try:
        done = await dispatcher.execute(
            WorkflowRunCreate(
                kind="meeting.export",
                source="admin_export_api",
                intent_text=f"Export meeting {meeting_id}",
                meeting_id=meeting_id,
                input={"meeting_id": meeting_id},
                timeout_s=120,
                active_key=f"meeting.export:{meeting_id}",
            )
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    tmp_path = Path(str(done.output["path"]))
    filename = str(done.output["filename"])

    def _cleanup() -> None:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    return FileResponse(
        path=tmp_path,
        filename=filename,
        media_type="application/zip",
        headers=PRIVATE_NO_STORE_HEADERS,
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
    ("llm_main_base_url", "llm_main_base_url", False),
    ("llm_main_api_key", "resolved_llm_main_api_key", True),
    ("llm_fast_base_url", "llm_fast_base_url", False),
    ("stt_firered_url", "stt_firered_url", False),
    ("tts_qwen3_url", "tts_qwen3_url", False),
    ("tts_qwen3_voice", "tts_qwen3_voice", False),
    ("tavily_api_key", "tavily_api_key", True),
]

_ALLOWED_KEYS = {f[0] for f in _REMOTE_FIELDS}


def _mask_secret(value: str) -> str:
    """Never return credential fragments to the renderer or remote clients."""

    return "[REDACTED]" if value else ""


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
