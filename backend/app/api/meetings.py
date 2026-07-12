"""会议 API：开始/喂 chunk/结束。

设计上音频上传走 multipart（会议端实时切片 30s/段），纪要落地后通过
``/meetings/{id}/minutes`` 拉取，前端清单式展示。

P4-M_meeting_history 新增（2026-05-28）：
- ``GET /meetings``                       前端启动期 hydrate 历史会议列表
- ``GET /meetings/{id}/transcript``       拉指定会议的转写段（``/segments`` 别名）
- ``GET /meetings/{id}/minutes``          反序列化 ``meetings.minutes_json``
- ``GET /meetings/{id}/artifacts``        per-meeting 产物

0.3 使用 ``artifact_links`` 将 ``artifacts`` 与会议关联。启动时还会扫描早期
``skill_build`` 中已有的 output 文件和 meta.json，把历史文件补录到这两个表；
因此前端切换到历史会议时也能获得持久化的 outputs。
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.stt import make_stt
from app.api.deps import (
    get_artifact_repository,
    get_diarizer_singleton,
    get_event_bus,
    get_llm_singleton,
    get_meeting_state,
    get_quota_governor,
    get_repository,
    get_scope_runtime,
    get_session_store,
    get_workflow_dispatcher,
    peek_scope_runtime,
    reset_scope_runtime_component_for_test,
)
from app.api.retrieval import get_rag
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.rag import RagPort
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact
from app.schemas.events import EchoEvent
from app.schemas.meeting import MeetingMinutes, MeetingSummary, TranscriptSegment
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import current_principal
from app.security.governor import PrincipalGovernor, QuotaReservation
from app.security.headers import PRIVATE_NO_STORE_HEADERS, apply_private_no_store
from app.security.scope import scoped_directory
from app.security.sessions import SessionStore
from app.upload import UploadTooLarge, read_limited_upload
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher, WorkflowExecutionError

router = APIRouter(prefix="/meetings", tags=["meetings"])

_share_ticket_tokens: dict[str, str] = {}
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_SHARE_TICKET_TTL = timedelta(minutes=10)


def _scope_key() -> tuple[str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.owner_id


class ClearMeetingOutputsRequest(BaseModel):
    artifact_ids: list[str] = Field(default_factory=list)
    clear_minutes: bool = True


class ClearMeetingOutputsResponse(BaseModel):
    meeting_id: str
    minutes_cleared: bool
    artifact_ids: list[str]
    artifacts_deleted: int
    missing_artifact_ids: list[str]


class MeetingShareTicketResponse(BaseModel):
    path: str
    expires_in_s: int | None


def get_meeting_pipeline(
    settings: Settings = Depends(get_settings),
    llm: OpenAICompatibleLLM = Depends(get_llm_singleton),
    event_bus: InMemoryEventBus = Depends(get_event_bus),
    repository: RepositoryPort = Depends(get_repository),
    diarizer: DiarizerPort = Depends(get_diarizer_singleton),
    rag: RagPort = Depends(get_rag),
) -> MeetingPipeline:
    runtime = get_scope_runtime(settings)
    return runtime.get_or_create(
        "meeting_pipeline",
        lambda: MeetingPipeline(
            settings=settings,
            stt=make_stt(settings),
            diarizer=diarizer,
            rag=rag,
            llm=llm,
            event_bus=event_bus,
            repository=repository,
        ),
    )


def get_meeting_pipeline_for_lifespan(
    settings: Settings,
    repository: RepositoryPort,
) -> MeetingPipeline:
    """lifespan 用：不通过 Depends 注入，直接拿单例（无 LLM/STT/RAG 也能 hydrate）。"""
    from app.api.deps import (
        get_diarizer_singleton as _get_diar,
    )
    from app.api.deps import (
        get_event_bus as _get_bus,
    )
    from app.api.deps import (
        get_llm_singleton as _get_llm,
    )

    runtime = get_scope_runtime(settings)

    def make_pipeline() -> MeetingPipeline:
        bus = _get_bus()
        llm = _get_llm(settings)
        diar = _get_diar(settings)
        return MeetingPipeline(
            settings=settings,
            stt=make_stt(settings),
            diarizer=diar,
            rag=get_rag(settings),
            llm=llm,
            event_bus=bus,
            repository=repository,
        )

    return runtime.get_or_create("meeting_pipeline", make_pipeline)


def reset_meeting_pipeline() -> None:
    """测试用：清掉缓存的单例。"""
    reset_scope_runtime_component_for_test("meeting_pipeline")


def get_initialized_meeting_pipeline() -> MeetingPipeline | None:
    """Return only the current principal's initialized pipeline, without creating one."""

    runtime = peek_scope_runtime()
    pipeline = runtime.get("meeting_pipeline") if runtime is not None else None
    return pipeline if isinstance(pipeline, MeetingPipeline) else None


def bind_meeting_workflow_handlers(
    dispatcher: WorkflowDispatcher,
    pipeline: MeetingPipeline,
) -> None:
    scope = _scope_key()

    async def finalize_handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload["meeting_id"])
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        if not pipeline.get_segments(meeting_id):
            loaded = await pipeline.load_meeting_for_retry(meeting_id)
            if not loaded:
                raise MeetingPipelineError(f"meeting {meeting_id} has no segments to summarize")
        title = str(payload["title"])
        try:
            minutes = await pipeline.finalize_meeting(meeting_id, title=title, commit=False)
        except Exception as exc:
            error = str(exc)[:500] or "unknown error"

            async def write_failure(conn: aiosqlite.Connection) -> None:
                principal = current_principal()
                await conn.execute(
                    """UPDATE meetings
                       SET state = 'ended', ended_at = COALESCE(ended_at, ?),
                           minutes_status = 'generation_failed', minutes_error = ?,
                           minutes_cleared_at = NULL
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                    (
                        datetime.now(UTC).isoformat(),
                        error,
                        meeting_id,
                        principal.tenant_id,
                        principal.owner_id,
                    ),
                )

            await dispatcher.service.fail_run_atomic(
                context.run_id,
                error=error,
                domain_writer=write_failure,
                domain_events=[
                    EchoEvent(
                        type="minutes.failed", meeting_id=meeting_id, payload={"error": error}
                    )
                ],
            )
            raise

        now = datetime.now(UTC).isoformat()
        minutes_json = minutes.model_dump_json()

        async def write_success(conn: aiosqlite.Connection) -> None:
            principal = current_principal()
            await conn.execute(
                """UPDATE meetings
                       SET state = 'finalized', title = ?, display_title = ?,
                       ended_at = COALESCE(ended_at, ?), finalized_at = ?,
                       minutes_json = ?, raw_transcript_ref = ?,
                       minutes_status = 'ok', minutes_error = '', minutes_cleared_at = NULL,
                       rag_projection_state = 'index_pending', rag_projection_error = NULL,
                       rag_projected_at = NULL
                   WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                (
                    title,
                    minutes.title,
                    now,
                    now,
                    minutes_json,
                    minutes.raw_transcript_ref,
                    meeting_id,
                    principal.tenant_id,
                    principal.owner_id,
                ),
            )

        output = {"meeting_id": meeting_id, "minutes": minutes.model_dump(mode="json")}
        committed = await dispatcher.service.complete_run_atomic(
            context.run_id,
            output=output,
            domain_writer=write_success,
            domain_events=[
                EchoEvent(
                    type="meeting.ended",
                    meeting_id=meeting_id,
                    payload={"duration_sec": minutes.duration_sec},
                ),
                EchoEvent(
                    type="minutes.ready",
                    meeting_id=meeting_id,
                    payload=minutes.model_dump(mode="json"),
                ),
                EchoEvent(
                    type="tts.suggested",
                    meeting_id=meeting_id,
                    payload={
                        "text": (f"会议{minutes.title}已结束，纪要已生成。{minutes.summary}")[:400],
                        "kind": "minutes",
                    },
                ),
            ],
            message="会议纪要已生成",
        )
        if committed is None:
            raise MeetingPipelineError("meeting workflow disappeared")
        await pipeline.after_finalize_committed(meeting_id, minutes)
        return output

    dispatcher.registry.register(
        "meeting.finalize",
        finalize_handler,
        scope=scope,
        replace=True,
    )


def bind_share_workflow_handler(
    dispatcher: WorkflowDispatcher,
    sessions: SessionStore,
) -> None:
    async def share_handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        resource_type = str(payload["resource_type"])
        resource_id = str(payload["resource_id"])
        output: dict[str, Any] = {
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        token_box: dict[str, str] = {}

        async def write_ticket(conn: aiosqlite.Connection) -> None:
            token, ticket_id, expires_at = await sessions.issue_resource_ticket_tx(
                conn,
                current_principal(),
                resource_type=resource_type,
                resource_id=resource_id,
                ttl=_SHARE_TICKET_TTL,
            )
            token_box["token"] = token
            output.update(ticket_id=ticket_id, expires_at=expires_at)

        committed = await dispatcher.service.complete_run_atomic(
            context.run_id,
            output=output,
            domain_writer=write_ticket,
            domain_events=[],
            message="分享票据已签发",
        )
        if committed is None or "token" not in token_box:
            raise RuntimeError("share ticket workflow disappeared")
        _share_ticket_tokens[context.run_id] = token_box["token"]
        asyncio.get_running_loop().call_later(
            30,
            _share_ticket_tokens.pop,
            context.run_id,
            None,
        )
        return output

    if dispatcher.registry.resolve("share.prepare") is None:
        dispatcher.registry.register("share.prepare", share_handler)


async def dispatch_resource_share_ticket(
    dispatcher: WorkflowDispatcher,
    sessions: SessionStore,
    *,
    resource_type: str,
    resource_id: str,
    source: str,
) -> str:
    bind_share_workflow_handler(dispatcher, sessions)
    done = await dispatcher.execute(
        WorkflowRunCreate(
            kind="share.prepare",
            source=source,
            intent_text=f"Prepare read-only share for {resource_type} {resource_id}",
            input={"resource_type": resource_type, "resource_id": resource_id},
            timeout_s=30,
        )
    )
    token = _share_ticket_tokens.pop(done.run_id, None)
    if token is None:
        raise RuntimeError("share workflow did not return its one-time token")
    return token


def bind_output_cleanup_workflow_handler(
    dispatcher: WorkflowDispatcher,
    _repository: RepositoryPort,
    settings: Settings,
    _artifact_repo: ArtifactRepository,
) -> None:
    principal = current_principal()
    scope = (principal.tenant_id, principal.owner_id)

    async def handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload["meeting_id"])
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        clear_minutes = bool(payload.get("clear_minutes", True))
        cleanup_artifacts: list[GeneratedArtifact] = []
        output: dict[str, Any] = {
            "meeting_id": meeting_id,
            "minutes_cleared": clear_minutes,
            "artifact_ids": [],
            "artifacts_deleted": 0,
            "missing_artifact_ids": [],
            # Durable post-commit file cleanup intent. Startup recovery replays
            # this list after a crash between SQLite commit and filesystem IO.
            "file_cleanup_artifact_ids": [],
        }

        async def write_cleanup(conn: aiosqlite.Connection) -> None:
            active = current_principal()
            cur = await conn.execute(
                """SELECT DISTINCT a.*
                   FROM artifacts a
                   JOIN artifact_links l
                     ON l.artifact_id = a.artifact_id
                    AND l.tenant_id = a.tenant_id
                    AND l.owner_id = a.owner_id
                   WHERE l.meeting_id = ?
                     AND l.tenant_id = ? AND l.owner_id = ?
                   ORDER BY a.artifact_id""",
                (meeting_id, active.tenant_id, active.owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
            output["artifact_ids"] = [str(row["artifact_id"]) for row in rows]

            await conn.execute(
                "DELETE FROM artifact_links WHERE meeting_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (meeting_id, active.tenant_id, active.owner_id),
            )
            for row in rows:
                artifact_id = str(row["artifact_id"])
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM artifact_links "
                    "WHERE artifact_id = ? AND tenant_id = ? AND owner_id = ?",
                    (artifact_id, active.tenant_id, active.owner_id),
                )
                count_row = await cur.fetchone()
                await cur.close()
                if int(count_row[0] if count_row else 0) > 0:
                    continue
                await conn.execute(
                    "DELETE FROM artifacts WHERE artifact_id = ? "
                    "AND tenant_id = ? AND owner_id = ?",
                    (artifact_id, active.tenant_id, active.owner_id),
                )
                cleanup_artifacts.append(
                    GeneratedArtifact(
                        artifact_id=artifact_id,
                        artifact_type=str(row["artifact_type"]),
                        title=str(row["title"] or ""),
                        file_path=str(row["file_path"]),
                        mime_type=str(row["mime_type"]),
                        size_bytes=int(row["size_bytes"] or 0),
                        generation_latency_ms=float(row["generation_latency_ms"] or 0),
                        model=str(row["model"] or ""),
                    )
                )
            output["file_cleanup_artifact_ids"] = [
                artifact.artifact_id for artifact in cleanup_artifacts
            ]
            if clear_minutes:
                await conn.execute(
                    """UPDATE meetings SET
                           state = CASE WHEN state = 'finalized' THEN 'ended' ELSE state END,
                           minutes_json = NULL, minutes_status = NULL, minutes_error = NULL,
                           display_title = NULL, finalized_at = NULL,
                           minutes_cleared_at = CURRENT_TIMESTAMP,
                           rag_projection_state = 'delete_pending',
                           rag_projection_error = NULL, rag_projected_at = NULL
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                    (meeting_id, active.tenant_id, active.owner_id),
                )

        committed = await dispatcher.service.complete_run_atomic(
            context.run_id,
            output=output,
            domain_writer=write_cleanup,
            domain_events=[],
            message="会议纪要与产物已清理",
        )
        if committed is None:
            raise RuntimeError("meeting output cleanup workflow disappeared")

        deleted = 0
        missing: list[str] = []
        cleanup_errors: dict[str, str] = {}
        for artifact in cleanup_artifacts:
            try:
                if _delete_artifact_file(settings, artifact):
                    deleted += 1
                else:
                    missing.append(artifact.artifact_id)
            except OSError as exc:
                cleanup_errors[artifact.artifact_id] = str(exc)[:300]
        output.update(
            artifacts_deleted=deleted,
            missing_artifact_ids=missing,
            file_cleanup_errors=cleanup_errors,
        )
        await dispatcher.service.merge_output(
            context.run_id,
            {
                "artifacts_deleted": deleted,
                "missing_artifact_ids": missing,
                "file_cleanup_errors": cleanup_errors,
            },
            event_type="workflow.file_cleanup_projected",
            message="产物文件清理投影已更新",
        )
        return output

    dispatcher.registry.register(
        "meeting.outputs.clear",
        handler,
        scope=scope,
        replace=True,
    )


async def dispatch_meeting_finalize(
    dispatcher: WorkflowDispatcher,
    pipeline: MeetingPipeline,
    repository: RepositoryPort,
    *,
    meeting_id: str,
    title: str,
    source: str,
) -> MeetingMinutes:
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    meeting = await repository.get_meeting(meeting_id)
    if meeting is not None and meeting.minutes_json and meeting.minutes_status == "ok":
        return MeetingMinutes.model_validate_json(meeting.minutes_json)

    finalize_runs = [
        item
        for item in await dispatcher.service.list_runs(meeting_id=meeting_id, limit=200)
        if item.kind == "meeting.finalize"
    ]
    latest = finalize_runs[0] if finalize_runs else None
    active_key = f"meeting.finalize:{meeting_id}"
    if latest is not None and not latest.is_terminal:
        # Re-dispatching an active run through its permanent request key makes
        # sure a request racing startup restore schedules the same run.
        run = await dispatcher.dispatch(
            WorkflowRunCreate(
                kind="meeting.finalize",
                source=latest.source,
                title=latest.title,
                intent_text=latest.intent_text,
                meeting_id=meeting_id,
                input=dict(latest.input),
                timeout_s=latest.timeout_s,
                idempotency_key=latest.idempotency_key,
                active_key=latest.active_key or active_key,
            )
        )
    elif latest is not None and latest.state != "succeeded":
        retried = await dispatcher.retry(latest.run_id, reason="meeting minutes retry")
        if retried is None:
            raise MeetingPipelineError("meeting workflow retry was not created")
        run = retried
    else:
        generation = len(finalize_runs) + 1
        run = await dispatcher.dispatch(
            WorkflowRunCreate(
                kind="meeting.finalize",
                source=source,
                title=title,
                intent_text=f"Finalize meeting {meeting_id}",
                meeting_id=meeting_id,
                input={"meeting_id": meeting_id, "title": title},
                timeout_s=300,
                idempotency_key=f"meeting.finalize:{meeting_id}:generation:{generation}",
                active_key=active_key,
            )
        )
    try:
        done = await dispatcher.wait_succeeded(run.run_id)
    except WorkflowExecutionError as exc:
        raise MeetingPipelineError(str(exc)) from exc
    return MeetingMinutes.model_validate(done.output["minutes"])


def _split_artifact_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for item in raw.replace("\n", ",").split(","):
        v = item.strip()
        if v and _ARTIFACT_ID_RE.fullmatch(v) and v not in out:
            out.append(v)
    return out


def _artifact_ids_from_minutes(minutes_json: str | None) -> list[str]:
    if not minutes_json:
        return []
    try:
        data = json.loads(minutes_json)
    except json.JSONDecodeError:
        return []
    ids: list[str] = []
    for todo in data.get("todos", []) or []:
        if not isinstance(todo, dict):
            continue
        artifact_id = todo.get("artifact_id")
        if (
            isinstance(artifact_id, str)
            and _ARTIFACT_ID_RE.fullmatch(artifact_id)
            and artifact_id not in ids
        ):
            ids.append(artifact_id)
    return ids


def _artifact_build_dir(settings: Settings, artifact_id: str) -> Path | None:
    if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        return None
    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    scoped_candidate = (scoped_directory(base).resolve() / artifact_id).resolve()
    legacy_candidate = (base / artifact_id).resolve()
    for candidate in (scoped_candidate, legacy_candidate):
        if candidate != base and base in candidate.parents and candidate.exists():
            return candidate
    return scoped_candidate if base in scoped_candidate.parents else None


def _artifact_download_info(settings: Settings, artifact_id: str) -> dict[str, object] | None:
    build_dir = _artifact_build_dir(settings, artifact_id)
    if build_dir is None or not build_dir.exists():
        return None
    candidates = sorted(build_dir.glob("output.*"))
    if not candidates:
        return None
    output = candidates[0]
    title = artifact_id
    artifact_type = output.suffix.lstrip(".") or "file"
    size_bytes = output.stat().st_size
    meta_path = build_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = str(meta.get("title") or title)
            artifact_type = str(meta.get("artifact_type") or meta.get("ext") or artifact_type)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "artifact_id": artifact_id,
        "title": title,
        "artifact_type": artifact_type,
        "size_bytes": size_bytes,
        "download_url": f"/artifacts/{quote(artifact_id)}/download",
    }


def _artifact_file_path(settings: Settings, artifact: GeneratedArtifact) -> Path | None:
    path = Path(artifact.file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    allowed_roots = [
        Path(settings.skill_executor_build_dir).expanduser().resolve(),
        Path(settings.storage_dir).expanduser().resolve(),
    ]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        return None
    return resolved


def _artifact_download_info_from_record(
    settings: Settings,
    artifact: GeneratedArtifact,
) -> dict[str, object] | None:
    path = _artifact_file_path(settings, artifact)
    if path is None or not path.exists():
        return None
    return {
        "artifact_id": artifact.artifact_id,
        "title": artifact.title or artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "size_bytes": artifact.size_bytes,
        "download_url": f"/artifacts/{quote(artifact.artifact_id)}/download",
    }


def _delete_artifact_file(settings: Settings, artifact: GeneratedArtifact) -> bool:
    path = _artifact_file_path(settings, artifact)
    if path is None or not path.exists():
        return False
    base = Path(settings.skill_executor_build_dir).expanduser().resolve()
    build_dirs = (
        (scoped_directory(base).resolve() / artifact.artifact_id).resolve(),
        (base / artifact.artifact_id).resolve(),
    )
    for build_dir in build_dirs:
        if (
            build_dir != base
            and base in build_dir.parents
            and (path == build_dir or build_dir in path.parents)
        ):
            shutil.rmtree(build_dir)
            return True
    path.unlink()
    return True


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _safe_download_name(raw: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", raw).strip()
    return name or "echodesk-minutes"


def _minutes_markdown(
    *,
    meeting_id: str,
    title: str,
    data: dict[str, object],
) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 会议 ID：{meeting_id}",
    ]
    duration = data.get("duration_sec")
    if isinstance(duration, int | float):
        lines.append(f"- 时长：{round(duration)} 秒")
    created_at = data.get("created_at")
    if created_at:
        lines.append(f"- 生成时间：{created_at}")
    summary = str(data.get("summary") or "").strip()
    lines.extend(["", "## 摘要", "", summary or "会议纪要尚未生成或已被删除。", ""])

    raw_sections = data.get("sections")
    sections = raw_sections if isinstance(raw_sections, list) else []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or "议题")
        lines.extend([f"## {heading}", ""])
        raw_bullets = sec.get("bullets")
        bullets = raw_bullets if isinstance(raw_bullets, list) else []
        for bullet in bullets:
            lines.append(f"- {bullet}")
        lines.append("")

    raw_decisions = data.get("decisions")
    decisions = raw_decisions if isinstance(raw_decisions, list) else []
    if decisions:
        lines.extend(["## 决议", ""])
        for decision in decisions:
            lines.append(f"- {decision}")
        lines.append("")

    raw_todos = data.get("todos")
    todos = raw_todos if isinstance(raw_todos, list) else []
    if todos:
        lines.extend(["## 待办", ""])
        for item in todos:
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                assignee = str(item.get("assignee") or "").strip()
                status = "已完成" if item.get("status") == "done" else "待处理"
                suffix = f"（{assignee}）" if assignee else ""
                if text:
                    lines.append(f"- [{status}] {text}{suffix}")
        lines.append("")

    body = "\n".join(lines).strip()
    return f"{body}\n"


def _share_html(
    *,
    meeting_id: str,
    title: str,
    summary: str | None,
    sections: list[dict[str, object]],
    decisions: list[str],
    artifacts: list[dict[str, object]],
    minutes_download_url: str | None,
) -> str:
    safe_title = html.escape(title)
    safe_meeting = html.escape(meeting_id)
    summary_html = (
        f'<p class="summary">{html.escape(summary)}</p>'
        if summary
        else '<p class="empty">会议纪要尚未生成或已被删除。</p>'
    )
    section_html = ""
    for sec in sections:
        heading = html.escape(str(sec.get("heading") or "议题"))
        raw_bullets = sec.get("bullets")
        bullets: list[object] = raw_bullets if isinstance(raw_bullets, list) else []
        bullet_html = "".join(f"<li>{html.escape(str(b))}</li>" for b in bullets)
        section_html += f"<section><h2>{heading}</h2><ul>{bullet_html}</ul></section>"
    decisions_html = ""
    if decisions:
        decisions_html = (
            "<section><h2>决议</h2><ul>"
            + "".join(f"<li>{html.escape(str(d))}</li>" for d in decisions)
            + "</ul></section>"
        )
    artifact_html = ""
    if artifacts:
        rows = []
        for item in artifacts:
            size_bytes = item.get("size_bytes")
            size = int(size_bytes) if isinstance(size_bytes, int | str) else 0
            rows.append(
                '<a class="artifact" href="{url}">'
                "<span><strong>{title}</strong><em>{kind} · {size}</em></span>"
                "<b>下载</b>"
                "</a>".format(
                    url=html.escape(str(item["download_url"])),
                    title=html.escape(str(item["title"])),
                    kind=html.escape(str(item["artifact_type"])),
                    size=html.escape(_format_bytes(size)),
                )
            )
        artifact_html = (
            '<section><h2>会议产物</h2><div class="artifacts">' + "".join(rows) + "</div></section>"
        )
    else:
        artifact_html = '<section><h2>会议产物</h2><p class="empty">暂无可下载产物。</p></section>'
    action_html = (
        '<div class="actions">'
        f'<a class="primary" href="{html.escape(minutes_download_url)}">保存纪要.md</a>'
        '<a href="#artifacts">查看产物</a>'
        "</div>"
        if minutes_download_url
        else ""
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>{safe_title} · EchoDesk</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f7f5f2; color: #26211d; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 28px 18px 44px; }}
    header {{ margin-bottom: 24px; }}
    .brand {{ color: #10a37f; font-size: 13px; font-weight: 700; letter-spacing: .02em; }}
    h1 {{ margin: 8px 0 6px; font-size: clamp(24px, 7vw, 38px); line-height: 1.12; }}
    .mid {{ color: #8b8178; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }}
    .summary {{ font-size: 16px; line-height: 1.8; background: white; border: 1px solid #e4ded7; border-radius: 14px; padding: 16px; }}
    section {{ margin-top: 22px; }}
    h2 {{ font-size: 16px; margin: 0 0 10px; }}
    ul {{ margin: 0; padding-left: 20px; line-height: 1.8; }}
    .empty {{ color: #8b8178; background: #fff; border: 1px dashed #ded7cf; border-radius: 12px; padding: 14px; }}
    .artifacts {{ display: grid; gap: 10px; }}
    .artifact {{ display: flex; justify-content: space-between; gap: 14px; align-items: center; padding: 14px; border: 1px solid #e4ded7; border-radius: 14px; background: white; color: inherit; text-decoration: none; }}
    .artifact strong {{ display: block; font-size: 15px; line-height: 1.3; }}
    .artifact em {{ display: block; margin-top: 4px; color: #8b8178; font-size: 12px; font-style: normal; }}
    .artifact b {{ flex: 0 0 auto; color: #0b7f64; font-size: 13px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
    .actions a {{ display: inline-flex; align-items: center; justify-content: center; min-height: 40px; padding: 0 14px; border-radius: 10px; border: 1px solid #d8d0c8; background: white; color: #26211d; text-decoration: none; font-size: 14px; font-weight: 650; }}
    .actions a.primary {{ background: #10a37f; border-color: #10a37f; color: white; }}
    footer {{ margin-top: 32px; color: #9a9188; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="brand">EchoDesk 会议资料</div>
      <h1>{safe_title}</h1>
      <div class="mid">{safe_meeting}</div>
      {action_html}
    </header>
    {summary_html}
    {section_html}
    {decisions_html}
    <div id="artifacts">{artifact_html}</div>
    <footer>扫码页面仅用于保存会议纪要与下载产物；删除请回到会议室大屏 EchoDesk 操作。</footer>
  </main>
</body>
</html>"""


@router.get("/current")
async def get_current_meeting(
    response: Response,
    state: Annotated[MeetingState, Depends(get_meeting_state)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> dict[str, object]:
    """全局会议状态机当前状态：idle 或 in_meeting。

    返回中带 ``minutes_status`` 让前端 MinutesView 能区分「会议中 / 生成中 / 失败 / 已生成」。
    in_meeting → minutes_status=null（会议没结束没纪要可言）
    idle      → 返回最新 meeting 的 minutes_status（若用户刚结束一个会议，UI 据此决定显示什么）
    """
    response.headers["Cache-Control"] = "no-store"
    await state.hydrate()
    state.start_watchdog()
    cur = state.current
    if cur is not None:
        return {
            "mode": "in_meeting",
            "meeting_id": cur.meeting_id,
            "started_at": cur.started_at.isoformat(),
            "started_by": cur.started_by,
            "minutes_status": None,
            "minutes_error": None,
        }
    # idle：探一下最近一条 meeting，把它的 minutes_status 透传出来
    latest = await repository.list_meetings(limit=1)
    latest_rec = latest[0] if latest else None
    return {
        "mode": "idle",
        "meeting_id": None,
        "started_at": None,
        "started_by": None,
        "minutes_status": latest_rec.minutes_status if latest_rec else None,
        "minutes_error": latest_rec.minutes_error if latest_rec else None,
    }


@router.post("/manual_start")
async def manual_start_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
    title: str | None = Form(None),
) -> dict[str, object]:
    """用户点击状态栏：手动开始会议。已在会议中则原样返回。"""
    state.start_watchdog()
    cur = await state.manual_start(title=title)
    return {
        "mode": "in_meeting",
        "meeting_id": cur.meeting_id,
        "started_at": cur.started_at.isoformat(),
        "started_by": cur.started_by,
    }


@router.post("/manual_end")
async def manual_end_meeting(
    state: Annotated[MeetingState, Depends(get_meeting_state)],
) -> dict[str, object]:
    """用户点击状态栏：手动结束会议（含 finalize 纪要）。"""
    state.start_watchdog()
    ended = await state.manual_end()
    return {"mode": "idle", "meeting_id": ended}


@router.get("", response_model=list[MeetingSummary])
async def list_meetings(
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    limit: int = 50,
) -> list[MeetingSummary]:
    """会议列表（左侧面板用）。

    按 started_at DESC 倒序，每条带 segments / speakers 计数 + minutes 是否就绪，
    避免前端再发 N 次 detail 请求。

    这是前端启动期 hydrate 的核心入口：早于任何 ws 事件，让用户能马上看到历史
    会议；ws 事件随后只负责维护 in-progress 会议的实时增量。
    """
    rows = await repository.list_meetings(limit=limit)
    out: list[MeetingSummary] = []
    for r in rows:
        n_seg = await repository.count_meeting_segments(r.id)
        n_spk = await repository.count_meeting_speakers(r.id)
        out.append(
            MeetingSummary(
                meeting_id=r.id,
                title=r.title,
                display_title=r.display_title,  # M_minutes_refactor：语义化标题
                state=r.state,
                started_at=r.started_at,
                ended_at=r.ended_at,
                finalized_at=r.finalized_at,
                n_segments=n_seg,
                n_speakers=n_spk,
                has_minutes=bool(r.minutes_json),
            )
        )
    return out


@router.get("/{meeting_id}/transcript", response_model=list[TranscriptSegment])
async def get_transcript(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> list[TranscriptSegment]:
    """单会议转写流（中间面板用）。

    与 ``/segments`` 等价但语义更显式 + 直接走 repository（不依赖 pipeline 内
    存状态，可拉历史会议）。404 当会议不存在；空列表表示没有 segment 但会议
    本身存在（合法的"刚 start 还没说话"状态）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return await repository.list_meeting_segments(meeting_id)


@router.get("/{meeting_id}/minutes", response_model=MeetingMinutes)
async def get_minutes(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> MeetingMinutes:
    """单会议纪要（右上面板用）。

    从 ``meetings.minutes_json`` 反序列化；finalize 之前会议没纪要时返回 404。
    JSON 解析失败抛 502（落库的纪要损坏属于运维问题，不应该让前端默默无展示）。
    """
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not meeting.minutes_json:
        raise HTTPException(status_code=404, detail="minutes not generated yet")
    try:
        data = json.loads(meeting.minutes_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"minutes_json corrupted: {e!s}") from e
    # 早期落库的 minutes 可能没带 meeting_id；补上保持 schema 完整
    data.setdefault("meeting_id", meeting_id)
    return MeetingMinutes(**data)


@router.get("/{meeting_id}/minutes.md")
async def download_minutes_markdown(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> Response:
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    if not meeting.minutes_json:
        raise HTTPException(status_code=404, detail="minutes not generated")
    try:
        data = json.loads(meeting.minutes_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail="minutes json corrupted") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="minutes json corrupted")

    title = str(data.get("title") or meeting.display_title or meeting.title or meeting_id)
    markdown = _minutes_markdown(meeting_id=meeting_id, title=title, data=data)
    filename = quote(f"{_safe_download_name(title)}.md")
    return Response(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            **PRIVATE_NO_STORE_HEADERS,
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        },
    )


@router.get("/{meeting_id}/artifacts", response_model=list[GeneratedArtifact])
async def get_meeting_artifacts(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> list[GeneratedArtifact]:
    """单会议产物（右下 outputs 面板用），以 artifact_links 为事实源。"""
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return await artifact_repo.list_meeting_artifacts(meeting_id)


@router.get("/{meeting_id}/share", response_class=HTMLResponse)
async def share_meeting(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
    sessions: Annotated[SessionStore, Depends(get_session_store)],
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
    artifact_ids: str | None = Query(None),
    share: str | None = Query(None),
) -> HTMLResponse:
    """手机扫码保存会议资料页。

    产物来源以后端 artifact_links 为准。``artifact_ids`` query 只保留兼容旧链接，
    不再参与新事实源判断。
    """
    _ = artifact_ids
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    title = meeting.display_title or meeting.title or meeting_id
    summary: str | None = None
    sections: list[dict[str, object]] = []
    decisions: list[str] = []
    if meeting.minutes_json:
        try:
            data = json.loads(meeting.minutes_json)
            title = str(data.get("title") or title)
            summary = str(data.get("summary") or "") or None
            raw_sections = data.get("sections") if isinstance(data.get("sections"), list) else []
            sections = [s for s in raw_sections if isinstance(s, dict)]
            raw_decisions = data.get("decisions") if isinstance(data.get("decisions"), list) else []
            decisions = [str(d) for d in raw_decisions]
        except json.JSONDecodeError:
            summary = "会议纪要数据损坏，请回到 EchoDesk 重新生成。"

    artifacts: list[dict[str, object]] = []
    for artifact in await artifact_repo.list_meeting_artifacts(meeting_id):
        info = _artifact_download_info_from_record(settings, artifact)
        if info is None:
            continue
        if settings.public_demo_mode:
            artifact_ticket = await dispatch_resource_share_ticket(
                dispatcher,
                sessions,
                resource_type="artifact",
                resource_id=artifact.artifact_id,
                source="meeting_share_page",
            )
            info["download_url"] = f"{info['download_url']}?share={quote(artifact_ticket)}"
        artifacts.append(info)
    minutes_url = f"/meetings/{quote(meeting_id)}/minutes.md" if meeting.minutes_json else None
    if minutes_url and settings.public_demo_mode:
        if not share:
            raise HTTPException(status_code=401, detail="share ticket required")
        minutes_url = f"{minutes_url}?share={quote(share)}"
    return HTMLResponse(
        _share_html(
            meeting_id=meeting_id,
            title=title,
            summary=summary,
            sections=sections,
            decisions=decisions,
            artifacts=artifacts,
            minutes_download_url=minutes_url,
        ),
        headers={
            **PRIVATE_NO_STORE_HEADERS,
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Frame-Options": "DENY",
        },
    )


@router.post("/{meeting_id}/share-ticket", response_model=MeetingShareTicketResponse)
async def create_meeting_share_ticket(
    meeting_id: str,
    response: Response,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    sessions: Annotated[SessionStore, Depends(get_session_store)],
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
) -> MeetingShareTicketResponse:
    """Create a narrow URL token; never put the full device session in a QR code."""

    apply_private_no_store(response.headers)
    if await repository.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    path = f"/meetings/{quote(meeting_id)}/share"
    if not settings.public_demo_mode:
        return MeetingShareTicketResponse(path=path, expires_in_s=None)
    token = await dispatch_resource_share_ticket(
        dispatcher,
        sessions,
        resource_type="meeting",
        resource_id=meeting_id,
        source="meeting_share_api",
    )
    return MeetingShareTicketResponse(
        path=f"{path}?share={quote(token)}",
        expires_in_s=int(_SHARE_TICKET_TTL.total_seconds()),
    )


@router.delete("/{meeting_id}/outputs", response_model=ClearMeetingOutputsResponse)
async def clear_meeting_outputs(
    meeting_id: str,
    body: ClearMeetingOutputsRequest,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
) -> ClearMeetingOutputsResponse:
    """清理会议纪要与产物文件。

    0.3 起 artifact_links 是唯一事实源。请求体里的 ``artifact_ids`` 保留为
    旧客户端兼容字段，但不会被用于扩大删除范围。
    """
    _ = body.artifact_ids
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    current_artifacts = await artifact_repo.list_meeting_artifacts(meeting_id)
    digest = hashlib.sha256(
        ("\0".join(sorted(item.artifact_id for item in current_artifacts))).encode()
    ).hexdigest()
    bind_output_cleanup_workflow_handler(dispatcher, repository, settings, artifact_repo)
    try:
        done = await dispatcher.execute(
            WorkflowRunCreate(
                kind="meeting.outputs.clear",
                source="meeting_outputs_api",
                intent_text=f"Clear outputs for meeting {meeting_id}",
                meeting_id=meeting_id,
                input={"meeting_id": meeting_id, "clear_minutes": body.clear_minutes},
                timeout_s=120,
                idempotency_key=(
                    f"meeting.outputs.clear:{meeting_id}:{body.clear_minutes}:{digest}"
                ),
            )
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ClearMeetingOutputsResponse.model_validate(done.output)


@router.post("/{meeting_id}/start")
async def start_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> dict[str, str]:
    """启动会议（low-level；建议走 /meetings/manual_start）。"""
    active = await pipeline.start_meeting(meeting_id)
    return {
        "meeting_id": active.id,
        "status": "started" if active.id == meeting_id else "active_reused",
    }


@router.post("/{meeting_id}/chunk", response_model=list[TranscriptSegment])
async def add_chunk(
    request: Request,
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    audio: UploadFile = File(...),
    sample_rate: int = Form(16_000),
    settings: Settings = Depends(get_settings),
    governor: PrincipalGovernor = Depends(get_quota_governor),
) -> list[TranscriptSegment]:
    if await repository.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    try:
        upload = await read_limited_upload(
            audio,
            max_bytes=int(settings.upload_max_file_mb * 1024 * 1024),
            chunk_bytes=settings.upload_read_chunk_bytes,
            governor=governor,
            principal=current_principal(),
            upload_reservation=getattr(request.state, "upload_quota_reservation", None),
        )
    except UploadTooLarge as exc:
        raise HTTPException(status_code=413, detail="audio upload too large") from exc
    audio_bytes = upload.data
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        return await pipeline.add_audio_chunk(meeting_id, audio_bytes, sample_rate=sample_rate)
    except MeetingPipelineError as e:
        status_code = 409 if "not active" in str(e) or "already ended" in str(e) else 502
        raise HTTPException(status_code=status_code, detail=str(e)) from e


@router.post("/{meeting_id}/finalize", response_model=MeetingMinutes)
async def finalize(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
    title: str = Form(...),
) -> MeetingMinutes:
    """生成或重试生成会议纪要（幂等）。

    幂等语义（2026-05-28 修）：
    - 第一次调用：用 segments + LLM 生成 minutes，写 ``state="finalized"`` + ``minutes_status="ok"``
    - 重试调用（前次失败 → ``state="ended"`` 且 ``minutes_status="generation_failed"``）：
      pipeline 重新装载 repo segments 并重新跑 LLM；成功覆盖原 minutes_json，
      失败再次写 ``generation_failed`` + 新的 ``minutes_error``。
    - 用户视角：「重试生成纪要」按钮就是再 POST 一次 ``/meetings/{id}/finalize``。
    """
    rec = await repository.get_meeting(meeting_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")
    try:
        return await dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repository,
            meeting_id=meeting_id,
            title=title,
            source="meeting_api",
        )
    except MeetingPipelineError as exc:
        status_code = 400 if "no segments" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/{meeting_id}/end")
async def end_meeting(
    meeting_id: str,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> dict[str, str]:
    """结束会议叠加层（不生成纪要）；ambient 主链路继续。"""
    if await repository.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    await pipeline.end_meeting(meeting_id)
    return {"meeting_id": meeting_id, "status": "ended"}


@router.get("/{meeting_id}/segments", response_model=list[TranscriptSegment])
async def list_segments(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
) -> list[TranscriptSegment]:
    if await repository.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return await repository.list_meeting_segments(meeting_id)


@router.post("/{meeting_id}/inject_segment", response_model=TranscriptSegment)
async def inject_segment(
    request: Request,
    meeting_id: str,
    seg: TranscriptSegment,
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Settings = Depends(get_settings),
    governor: PrincipalGovernor = Depends(get_quota_governor),
) -> TranscriptSegment:
    """演示/兜底入口：当 STT 服务不可用时直接注入已知转写片段。

    用途：
    - demo 录制：把预先准备的逐字稿喂进 pipeline，避开 STT 依赖
    - 离线回放：从 raw_transcript_ref 文件重放
    """
    if await repository.get_meeting(meeting_id) is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    principal = current_principal()
    stored_bytes = len(seg.model_dump_json().encode("utf-8"))
    if stored_bytes > settings.meeting_inject_segment_max_bytes:
        raise HTTPException(status_code=413, detail="transcript segment too large")

    observed_body_bytes = int(getattr(request.state, "upload_body_bytes", stored_bytes))
    upload_reservation = getattr(request.state, "upload_quota_reservation", None)
    if isinstance(upload_reservation, QuotaReservation):
        await upload_reservation.settle(observed_body_bytes)
    else:
        await governor.charge_upload_bytes(principal, observed_body_bytes)

    storage_reservation = await governor.reserve_storage(principal, stored_bytes)
    try:
        return await pipeline.append_segment(meeting_id, seg)
    except BaseException:
        # The durable lifetime charge belongs only to a segment that reached
        # the repository.  Network ingress remains charged even on failure.
        await storage_reservation.release()
        raise
