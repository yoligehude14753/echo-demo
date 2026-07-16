"""HTTP API: 产物生成 / 下载。

POST /artifacts/generate — body { artifact_type, brief, extra_instructions? }
  artifact_type ∈ word | docx | xlsx | excel | pptx | ppt | html
                  | markdown | md | mdown | pdf | txt | text （详见 schemas.artifact）
GET  /artifacts/{id}/download — 下载产物文件，filename 形如
  <safe_title>_<artifact_id>.<ext>（来自 build_dir/meta.json）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm import LLMError
from app.adapters.skill import SkillError, SkillExecutor
from app.api.deps import (
    get_artifact_repository,
    get_event_bus,
    get_repository,
    get_request_principal,
    get_workflow_dispatcher,
)
from app.api.deps import get_llm_singleton as get_llm
from app.artifacts.recovery import validated_artifact_file_path
from app.artifacts.repository import ArtifactRepository
from app.artifacts.staging import (
    load_workflow_artifact,
    workflow_artifact_id,
    workflow_build_lease_marker,
    write_workflow_artifact_manifest,
)
from app.config import Settings, get_settings
from app.ports.llm import LLMPort
from app.ports.repository import RepositoryPort
from app.ports.skill import SkillExecutorPort
from app.schemas.artifact import (
    ArtifactRequest,
    GeneratedArtifact,
    GeneratedArtifactDTO,
    normalize_kind,
)
from app.schemas.events import EchoEvent
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import current_principal
from app.security.errors import InternalHTTPException
from app.security.headers import PRIVATE_NO_STORE_HEADERS
from app.security.models import Principal
from app.security.public_projection import project_client_dict
from app.security.scope import scoped_directory
from app.use_cases.generate_artifact import generate_artifact
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher, WorkflowExecutionError

_log = logging.getLogger("echodesk.artifacts")

router = APIRouter(tags=["artifacts"])

_PUBLIC_OWNER_SAFE_KINDS = frozenset({"pptx", "html", "markdown", "txt"})

_skill_singleton: SkillExecutor | None = None


def get_skill(settings: Settings = Depends(get_settings)) -> SkillExecutorPort:
    global _skill_singleton  # noqa: PLW0603
    if _skill_singleton is None:
        _skill_singleton = SkillExecutor(settings)
    return _skill_singleton


def reset_skill_singleton() -> None:
    global _skill_singleton  # noqa: PLW0603
    _skill_singleton = None


def _artifact_dto(value: GeneratedArtifact | dict[str, Any]) -> GeneratedArtifactDTO:
    payload = value.model_dump(mode="json") if isinstance(value, GeneratedArtifact) else value
    return GeneratedArtifactDTO.model_validate(project_client_dict(payload, current_principal()))


def bind_artifact_workflow_handler(
    dispatcher: WorkflowDispatcher,
    *,
    settings: Settings,
    llm: LLMPort,
    runner: SkillExecutorPort,
    event_bus: InMemoryEventBus,
    artifact_repo: ArtifactRepository,
) -> None:
    principal = current_principal()
    scope = (principal.tenant_id, principal.owner_id)

    async def handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload["meeting_id"]) if payload.get("meeting_id") else None
        todo_id = str(payload["todo_id"]) if payload.get("todo_id") else None
        artifact_type = str(payload["artifact_type"])
        await event_bus.publish(
            EchoEvent(
                type="artifact.generating",
                meeting_id=meeting_id,
                payload={
                    "artifact_type": artifact_type,
                    "brief": str(payload["brief"])[:200],
                    "run_id": context.run_id,
                    "todo_id": todo_id,
                },
            )
        )
        await dispatcher.service.record_event(
            context.run_id,
            "artifact.generating",
            message="正在生成产物",
            payload={"artifact_type": artifact_type},
        )
        try:
            artifact = load_workflow_artifact(
                settings,
                run_id=context.run_id,
                artifact_type=artifact_type,
            )
            if artifact is None:
                with workflow_build_lease_marker(
                    settings,
                    run_id=context.run_id,
                    artifact_type=artifact_type,
                    fence_token=context.fence_token,
                ):
                    artifact = await generate_artifact(
                        runner=runner,
                        llm=llm,
                        artifact_type=artifact_type,
                        brief=str(payload["brief"]),
                        extra_instructions=(
                            str(payload["extra_instructions"])
                            if payload.get("extra_instructions")
                            else None
                        ),
                        artifact_id=workflow_artifact_id(context.run_id, artifact_type),
                    )
            artifact = write_workflow_artifact_manifest(
                settings,
                run_id=context.run_id,
                artifact_type=artifact_type,
                artifact=artifact,
            )
        except (SkillError, LLMError) as exc:
            error = (
                f"远程 LLM 不可达：{str(exc)[:200]}"
                if isinstance(exc, LLMError)
                else str(exc)[:500]
            )
            failed_events = [
                EchoEvent(
                    type="artifact.failed",
                    meeting_id=meeting_id,
                    payload={
                        "artifact_type": artifact_type,
                        "error": error,
                        "reason": "remote_llm" if isinstance(exc, LLMError) else "skill",
                        "run_id": context.run_id,
                        "todo_id": todo_id,
                    },
                )
            ]
            if meeting_id and todo_id:
                failed_events.append(
                    EchoEvent(
                        type="meeting.todo.updated",
                        meeting_id=meeting_id,
                        payload={
                            "todo_id": todo_id,
                            "state": "failed",
                            "run_id": context.run_id,
                            "error": error,
                        },
                    )
                )
            await dispatcher.service.fail_run_atomic(
                context.run_id,
                error=error,
                domain_events=failed_events,
                payload={"reason": "remote_llm"} if isinstance(exc, LLMError) else None,
            )
            raise RuntimeError(error) from exc

        links: list[dict[str, str | None]] = []
        domain_events: list[EchoEvent] = []

        async def write_domain(conn: aiosqlite.Connection) -> None:
            await artifact_repo.save_artifact_tx(conn, artifact, run_id=context.run_id)
            if meeting_id or todo_id:
                link = await artifact_repo.link_artifact_tx(
                    conn,
                    artifact_id=artifact.artifact_id,
                    source="todo" if todo_id else "meeting",
                    meeting_id=meeting_id,
                    todo_id=todo_id,
                    run_id=context.run_id,
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
            if meeting_id and todo_id:
                principal_now = current_principal()
                cur = await conn.execute(
                    """SELECT minutes_json FROM meetings
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                    (meeting_id, principal_now.tenant_id, principal_now.owner_id),
                )
                row = await cur.fetchone()
                await cur.close()
                if row and row["minutes_json"]:
                    minutes = json.loads(str(row["minutes_json"]))
                    now = datetime.now(UTC).isoformat()
                    for todo in minutes.get("todos") or []:
                        if isinstance(todo, dict) and todo.get("id") == todo_id:
                            todo.update(
                                status="done", done_at=now, artifact_id=artifact.artifact_id
                            )
                            await conn.execute(
                                """UPDATE meetings SET minutes_json = ?
                                   WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                                (
                                    json.dumps(minutes, ensure_ascii=False),
                                    meeting_id,
                                    principal_now.tenant_id,
                                    principal_now.owner_id,
                                ),
                            )
                            domain_events.extend(
                                [
                                    EchoEvent(
                                        type="meeting.todo.updated",
                                        meeting_id=meeting_id,
                                        payload={
                                            "todo_id": todo_id,
                                            "state": "succeeded",
                                            "run_id": context.run_id,
                                            "artifact_id": artifact.artifact_id,
                                        },
                                    ),
                                    EchoEvent(
                                        type="meeting.todo.completed",
                                        meeting_id=meeting_id,
                                        payload={
                                            "todo_id": todo_id,
                                            "artifact_id": artifact.artifact_id,
                                            "done_at": now,
                                        },
                                    ),
                                ]
                            )
                            break
            domain_events.append(
                EchoEvent(
                    type="artifact.ready",
                    meeting_id=meeting_id,
                    payload={
                        **artifact.model_dump(mode="json"),
                        "meeting_id": meeting_id,
                        "todo_id": todo_id,
                        "run_id": context.run_id,
                        "links": links,
                    },
                )
            )

        output = {
            "artifact": artifact.model_dump(mode="json"),
            "artifact_id": artifact.artifact_id,
            "links": links,
        }
        committed = await dispatcher.service.complete_run_atomic(
            context.run_id,
            output=output,
            domain_writer=write_domain,
            domain_events=domain_events,
            message="产物已生成",
        )
        if committed is None:
            raise RuntimeError("artifact workflow disappeared")
        return output

    dispatcher.registry.register(
        "artifact.generate",
        handler,
        scope=scope,
        replace=True,
    )


@router.post(
    "/artifacts/generate",
    response_model=GeneratedArtifactDTO,
)
async def generate(
    body: ArtifactRequest,
    principal: Principal = Depends(get_request_principal),
    repository: RepositoryPort = Depends(get_repository),
    llm: LLMPort = Depends(get_llm),
    runner: SkillExecutorPort = Depends(get_skill),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    dispatcher: WorkflowDispatcher = Depends(get_workflow_dispatcher),
    artifact_repo: ArtifactRepository = Depends(get_artifact_repository),
) -> GeneratedArtifactDTO:
    """生成产物。artifact_type 走 ArtifactKind 枚举校验（含 ppt/pptx/word/xlsx/excel/html 别名）。

    M_minutes_refactor：可选携带 ``meeting_id`` + ``todo_id``：
    - 生成成功后回写 ``meetings.minutes_json.todos[todo_id].status="done"``
      + ``artifact_id``，并发 ``meeting.todo.completed`` 事件给前端
    - 任何一边为空则跳过回写（普通产物生成路径不受影响）
    """
    if not body.brief.strip():
        raise HTTPException(status_code=400, detail="brief empty")
    artifact_kind = normalize_kind(body.artifact_type)
    if principal.mode == "public" and artifact_kind not in _PUBLIC_OWNER_SAFE_KINDS:
        raise HTTPException(
            status_code=403,
            detail="artifact type is not available to owner sessions",
        )
    if body.meeting_id and await repository.get_meeting(body.meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    bind_artifact_workflow_handler(
        dispatcher,
        settings=artifact_repo.settings,
        llm=llm,
        runner=runner,
        event_bus=event_bus,
        artifact_repo=artifact_repo,
    )
    input_payload = body.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(input_payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    try:
        done = await dispatcher.execute(
            WorkflowRunCreate(
                kind="artifact.generate",
                source="retry"
                if body.retry_of_run_id
                else ("todo" if body.todo_id else "artifact_api"),
                title=body.title,
                intent_text=body.brief,
                meeting_id=body.meeting_id,
                todo_id=body.todo_id,
                input=input_payload,
                timeout_s=600,
                active_key=f"artifact.generate:{digest}",
            )
        )
    except WorkflowExecutionError as exc:
        detail = str(exc)
        raise InternalHTTPException(
            status_code=502 if "远程 LLM" in detail else 400,
            detail=detail,
        ) from exc
    return _artifact_dto(done.output["artifact"])


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


def _allowed_artifact_file(
    settings: Settings,
    artifact: GeneratedArtifact,
) -> Path | None:
    principal = current_principal()
    return validated_artifact_file_path(
        settings,
        artifact_id=artifact.artifact_id,
        file_path=artifact.file_path,
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        metadata=artifact.metadata,
    )


def _download_name_from_artifact(artifact: GeneratedArtifact, path: Path) -> str:
    ext = path.suffix.lstrip(".")
    safe = _safe_title(artifact.title or artifact.artifact_id)
    return f"{safe}_{artifact.artifact_id}.{ext}" if ext else f"{safe}_{artifact.artifact_id}"


@router.get("/artifacts", response_model=list[GeneratedArtifactDTO])
async def list_artifacts(
    artifact_repo: ArtifactRepository = Depends(get_artifact_repository),
    limit: int = Query(100, ge=1, le=500),
) -> list[GeneratedArtifactDTO]:
    return [_artifact_dto(artifact) for artifact in await artifact_repo.list_artifacts(limit=limit)]


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
        f = _allowed_artifact_file(settings, artifact)
        if f is not None:
            return FileResponse(
                f,
                filename=_download_name_from_artifact(artifact, f),
                media_type=artifact.mime_type or None,
                headers=PRIVATE_NO_STORE_HEADERS,
            )

    # 旧版本允许仅凭可猜的 build 目录名下载未登记文件。local-first 继续兼容；
    # public 模式必须以 owner-scoped metadata 为授权事实源，查不到即 404。
    if settings.public_demo_mode:
        raise HTTPException(status_code=404, detail="artifact not found")

    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    scoped_build_dir = (scoped_directory(base).resolve() / artifact_id).resolve()
    legacy_build_dir = (base / artifact_id).resolve()
    build_dir = scoped_build_dir if scoped_build_dir.is_dir() else legacy_build_dir
    if build_dir == base or base not in build_dir.parents or not build_dir.is_dir():
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

    return FileResponse(f, filename=download_name, headers=PRIVATE_NO_STORE_HEADERS)
