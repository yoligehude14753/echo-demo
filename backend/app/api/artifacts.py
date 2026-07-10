"""HTTP API: 产物生成 / 下载。

POST /artifacts/generate — body { artifact_type, brief, extra_instructions? }
  artifact_type ∈ word | docx | xlsx | excel | pptx | ppt | html
                  | markdown | md | mdown | pdf | txt | text （详见 schemas.artifact）
GET  /artifacts/{id}/download — 下载产物文件，filename 形如
  <safe_title>_<artifact_id>.<ext>（来自 build_dir/meta.json）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import LLMError
from app.adapters.skill import SkillError, SkillExecutor
from app.api.deps import get_artifact_repository, get_event_bus, get_workflow_service
from app.api.deps import get_llm_singleton as get_llm
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.ports.skill import SkillExecutorPort
from app.schemas.artifact import ArtifactRequest, GeneratedArtifact
from app.schemas.events import EchoEvent
from app.schemas.workflow import WorkflowRunCreate
from app.use_cases.generate_artifact import generate_artifact
from app.workflows.service import WorkflowService

_log = logging.getLogger("echodesk.artifacts")

router = APIRouter(tags=["artifacts"])


_skill_singleton: SkillExecutor | None = None


def get_skill(settings: Settings = Depends(get_settings)) -> SkillExecutorPort:
    global _skill_singleton  # noqa: PLW0603
    if _skill_singleton is None:
        _skill_singleton = SkillExecutor(settings)
    return _skill_singleton


def reset_skill_singleton() -> None:
    global _skill_singleton  # noqa: PLW0603
    _skill_singleton = None


@router.post("/artifacts/generate", response_model=GeneratedArtifact)
async def generate(
    body: ArtifactRequest,
    llm: LLMPort = Depends(get_llm),
    runner: SkillExecutorPort = Depends(get_skill),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    workflow_service: WorkflowService = Depends(get_workflow_service),
    artifact_repo: ArtifactRepository = Depends(get_artifact_repository),
) -> GeneratedArtifact:
    """生成产物。artifact_type 走 ArtifactKind 枚举校验（含 ppt/pptx/word/xlsx/excel/html 别名）。

    M_minutes_refactor：可选携带 ``meeting_id`` + ``todo_id``：
    - 生成成功后回写 ``meetings.minutes_json.todos[todo_id].status="done"``
      + ``artifact_id``，并发 ``meeting.todo.completed`` 事件给前端
    - 任何一边为空则跳过回写（普通产物生成路径不受影响）
    """
    if not body.brief.strip():
        raise HTTPException(status_code=400, detail="brief empty")
    run = await workflow_service.create_run(
        WorkflowRunCreate(
            kind="artifact.generate",
            source="retry" if body.retry_of_run_id else ("todo" if body.todo_id else "artifact_api"),
            title=body.title,
            intent_text=body.brief,
            meeting_id=body.meeting_id,
            todo_id=body.todo_id,
            input={
                "artifact_type": body.artifact_type,
                "extra_instructions": body.extra_instructions,
                "context_refs": body.context_refs,
                "quality_first": body.quality_first,
                "retry_of": body.retry_of_run_id,
            },
        )
    )
    await workflow_service.start_run(run.run_id)
    if body.meeting_id and body.todo_id:
        await event_bus.publish(
            EchoEvent(
                type="meeting.todo.updated",
                meeting_id=body.meeting_id,
                payload={"todo_id": body.todo_id, "state": "running", "run_id": run.run_id},
            )
        )
    await event_bus.publish(
        EchoEvent(
            type="artifact.generating",
            meeting_id=body.meeting_id,
            payload={
                "artifact_type": body.artifact_type,
                "brief": body.brief[:200],
                "run_id": run.run_id,
                "todo_id": body.todo_id,
            },
        )
    )
    await workflow_service.record_event(
        run.run_id,
        "artifact.generating",
        message="正在生成产物",
        payload={"artifact_type": body.artifact_type},
    )
    try:
        artifact = await generate_artifact(
            runner=runner,
            llm=llm,
            artifact_type=body.artifact_type,
            brief=body.brief,
            extra_instructions=body.extra_instructions,
        )
    except SkillError as e:
        await workflow_service.fail_run(run.run_id, error=str(e)[:500])
        await event_bus.publish(
            EchoEvent(
                type="artifact.failed",
                meeting_id=body.meeting_id,
                payload={
                    "artifact_type": body.artifact_type,
                    "error": str(e)[:300],
                    "run_id": run.run_id,
                    "todo_id": body.todo_id,
                },
            )
        )
        if body.meeting_id and body.todo_id:
            await event_bus.publish(
                EchoEvent(
                    type="meeting.todo.updated",
                    meeting_id=body.meeting_id,
                    payload={
                        "todo_id": body.todo_id,
                        "state": "failed",
                        "run_id": run.run_id,
                        "error": str(e)[:300],
                    },
                )
            )
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LLMError as e:
        # P2.3：LLM 远程不可达（Yunwu/eight 断）也算 graceful failure，
        # 否则前端只能看到 500 静默挂。带 reason="remote_llm" 让前端区分这
        # 类失败（可引导查 StatusBar 云 pill）。
        error = f"远程 LLM 不可达：{str(e)[:200]}"
        await workflow_service.fail_run(
            run.run_id,
            error=error,
            payload={"reason": "remote_llm"},
        )
        await event_bus.publish(
            EchoEvent(
                type="artifact.failed",
                meeting_id=body.meeting_id,
                payload={
                    "artifact_type": body.artifact_type,
                    "error": error,
                    "reason": "remote_llm",
                    "run_id": run.run_id,
                    "todo_id": body.todo_id,
                },
            )
        )
        if body.meeting_id and body.todo_id:
            await event_bus.publish(
                EchoEvent(
                    type="meeting.todo.updated",
                    meeting_id=body.meeting_id,
                    payload={
                        "todo_id": body.todo_id,
                        "state": "failed",
                        "run_id": run.run_id,
                        "error": error,
                    },
                )
            )
        raise HTTPException(status_code=502, detail=str(e)) from e
    artifact = await artifact_repo.save_artifact(artifact, run_id=run.run_id)
    links: list[dict[str, str | None]] = []
    if body.meeting_id or body.todo_id:
        link = await artifact_repo.link_artifact(
            artifact_id=artifact.artifact_id,
            source="todo" if body.todo_id else "meeting",
            meeting_id=body.meeting_id,
            todo_id=body.todo_id,
            run_id=run.run_id,
        )
        links.append(
            {
                "link_id": link.link_id,
                "source": link.source,
                "meeting_id": link.meeting_id,
                "todo_id": link.todo_id,
                "run_id": link.run_id,
            }
        )
    # M_minutes_refactor：todo 回写（artifact 已经生成，回写失败只警告日志，
    # 不影响 artifact.ready 事件正常发出，否则用户看不到产物已生成）
    if body.meeting_id and body.todo_id:
        await _attach_artifact_to_todo_safe(
            meeting_id=body.meeting_id,
            todo_id=body.todo_id,
            artifact_id=artifact.artifact_id,
        )
        await event_bus.publish(
            EchoEvent(
                type="meeting.todo.updated",
                meeting_id=body.meeting_id,
                payload={
                    "todo_id": body.todo_id,
                    "state": "succeeded",
                    "run_id": run.run_id,
                    "artifact_id": artifact.artifact_id,
                },
            )
        )
    await workflow_service.complete_run(
        run.run_id,
        output={"artifact_id": artifact.artifact_id, "links": links},
        message="产物已生成",
    )
    payload = artifact.model_dump(mode="json")
    if body.meeting_id:
        payload["meeting_id"] = body.meeting_id
    if body.todo_id:
        payload["todo_id"] = body.todo_id
    payload["run_id"] = run.run_id
    payload["links"] = links
    await event_bus.publish(
        EchoEvent(
            type="artifact.ready",
            meeting_id=body.meeting_id,
            payload=payload,
        )
    )
    return artifact


async def _attach_artifact_to_todo_safe(*, meeting_id: str, todo_id: str, artifact_id: str) -> None:
    """从 meetings.py 拿 pipeline 单例并尝试回写 todo；任何异常只警告日志。

    use_cases / api 层间用 lazy import 避免循环引用（meetings.py 也 import 这里的
    schemas / 反向风险）。回写失败不抛错——artifact 自身已经成功生成，前端能
    在 ArtifactPanel 看到下载链接；只是 todo checkbox 不会自动划掉。
    """
    try:
        from app.api.meetings import _pipeline

        if _pipeline is None:
            _log.warning(
                "todo writeback skipped: meeting pipeline singleton not initialized "
                "(meeting_id=%s todo_id=%s artifact_id=%s)",
                meeting_id,
                todo_id,
                artifact_id,
            )
            return
        ok = await _pipeline.attach_artifact_to_todo(meeting_id, todo_id, artifact_id)
        if not ok:
            _log.warning(
                "todo writeback miss: meeting_id=%s todo_id=%s artifact_id=%s "
                "(meeting / minutes_json / todo not found)",
                meeting_id,
                todo_id,
                artifact_id,
            )
    except Exception as e:  # pragma: no cover - 防御性，不影响主路径
        _log.warning(
            "todo writeback failed: meeting_id=%s todo_id=%s artifact_id=%s err=%s",
            meeting_id,
            todo_id,
            artifact_id,
            e,
        )


# 跨平台不允许的文件名字符 + 控制字符
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
# 文件名总长度上限（含扩展名）；macOS HFS+ 是 255 字节，留余量给 _<id>.<ext>
_MAX_FILENAME_LEN = 120
# meta.json 缺失或 title 被全部清掉时的兜底
_FALLBACK_TITLE = "untitled"


def _safe_title(raw: str) -> str:
    """将任意 title 字符串归一为可作为文件名片段的安全形式。"""
    s = _UNSAFE_FILENAME_CHARS.sub(" ", raw).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")  # 去首尾空格/句点（Windows 上以 . 结尾会被 strip）
    if not s:
        return _FALLBACK_TITLE
    if len(s) > _MAX_FILENAME_LEN:
        s = s[:_MAX_FILENAME_LEN].rstrip(" .…") or _FALLBACK_TITLE
    return s


def _allowed_artifact_file(settings: Settings, file_path: str) -> Path | None:
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    allowed_roots = [
        Path(settings.skill_executor_build_dir).expanduser().resolve(),
        Path(settings.storage_dir).expanduser().resolve(),
    ]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        return None
    return resolved if resolved.is_file() else None


def _download_name_from_artifact(artifact: GeneratedArtifact, path: Path) -> str:
    ext = path.suffix.lstrip(".")
    safe = _safe_title(artifact.title or artifact.artifact_id)
    return f"{safe}_{artifact.artifact_id}.{ext}" if ext else f"{safe}_{artifact.artifact_id}"


@router.get("/artifacts", response_model=list[GeneratedArtifact])
async def list_artifacts(
    artifact_repo: ArtifactRepository = Depends(get_artifact_repository),
    limit: int = Query(100, ge=1, le=500),
) -> list[GeneratedArtifact]:
    return await artifact_repo.list_artifacts(limit=limit)


@router.get("/artifacts/{artifact_id}/download")
async def download(
    artifact_id: str,
    settings: Settings = Depends(get_settings),
    artifact_repo: ArtifactRepository = Depends(get_artifact_repository),
) -> FileResponse:
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        artifact = await artifact_repo.get_artifact(artifact_id)
    except aiosqlite.Error as e:
        _log.debug("artifact db lookup skipped for %s: %s", artifact_id, e)
        artifact = None
    if artifact is not None:
        f = _allowed_artifact_file(settings, artifact.file_path)
        if f is not None:
            return FileResponse(
                f,
                filename=_download_name_from_artifact(artifact, f),
                media_type=artifact.mime_type or None,
            )

    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    build_dir = (base / artifact_id).resolve()
    if build_dir == base or base not in build_dir.parents:
        raise HTTPException(status_code=404, detail="artifact not found")
    if not build_dir.exists():
        raise HTTPException(status_code=404, detail="artifact not found")
    candidates = list(build_dir.glob("output.*"))
    if not candidates:
        raise HTTPException(status_code=404, detail="output file missing")
    f = candidates[0]

    # 读 meta.json 拼友好文件名；缺失/坏掉 → 回退到 output.<ext>
    meta_path = build_dir / "meta.json"
    download_name = f.name
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_title = str(meta.get("title", "") or "")
            ext = str(meta.get("ext", "") or f.suffix.lstrip("."))
            safe = _safe_title(raw_title)
            download_name = f"{safe}_{artifact_id}.{ext}" if ext else f"{safe}_{artifact_id}"
        except (OSError, json.JSONDecodeError, ValueError):
            download_name = f.name

    return FileResponse(f, filename=download_name)
