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
from app.artifacts.recovery import (
    artifact_file_cleanup_target,
    replay_artifact_file_cleanup_target,
    validated_artifact_file_path,
)
from app.artifacts.repository import ArtifactRepository
from app.config import Settings, get_settings
from app.ports.diarizer import DiarizerPort
from app.ports.rag import RagPort
from app.ports.repository import RepositoryPort
from app.schemas.artifact import GeneratedArtifact, GeneratedArtifactDTO
from app.schemas.events import EchoEvent
from app.schemas.meeting import MeetingMinutes, MeetingSummary, TranscriptSegment
from app.schemas.workflow import WorkflowRunCreate, WorkflowState
from app.security.context import current_principal
from app.security.governor import PrincipalGovernor, QuotaReservation
from app.security.headers import PRIVATE_NO_STORE_HEADERS, apply_private_no_store
from app.security.public_projection import project_client_dict
from app.security.scope import scoped_directory
from app.security.sessions import SessionStore
from app.upload import UploadTooLarge, read_limited_upload
from app.use_cases.meeting_pipeline import MeetingPipeline, MeetingPipelineError
from app.use_cases.meeting_state import MeetingState
from app.workflows.kernel import WorkflowContext, WorkflowDispatcher, WorkflowExecutionError
from app.workflows.service import (
    WorkflowConflictError,
    WorkflowRunRecord,
    new_workflow_run_id,
)

router = APIRouter(prefix="/meetings", tags=["meetings"])

_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_SHARE_TICKET_TTL = timedelta(minutes=10)


def _scope_key() -> tuple[str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.owner_id


def _minutes_dto(minutes: MeetingMinutes) -> MeetingMinutes:
    return MeetingMinutes.model_validate(
        project_client_dict(minutes.model_dump(mode="json"), current_principal())
    )


def _artifact_dto(artifact: GeneratedArtifact) -> GeneratedArtifactDTO:
    return GeneratedArtifactDTO.model_validate(
        project_client_dict(artifact.model_dump(mode="json"), current_principal())
    )


def _artifact_metadata(raw: object) -> dict[str, str]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


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


def bind_meeting_workflow_handlers(  # noqa: PLR0915 - explicit durable finalize lifecycle
    dispatcher: WorkflowDispatcher,
    pipeline: MeetingPipeline,
) -> None:
    scope = _scope_key()

    async def write_terminal_projection(
        conn: aiosqlite.Connection,
        run_id: str,
        meeting_id: str | None,
        state: WorkflowState,
        error: str,
    ) -> bool:
        if not meeting_id:
            return False
        principal = current_principal()
        cancelled_at = datetime.now(UTC).isoformat() if state == "cancelled" else None
        changed = await conn.execute(
            """UPDATE meetings
               SET state = 'ended', ended_at = COALESCE(ended_at, ?),
                   minutes_status = 'generation_failed', minutes_error = ?,
                   minutes_generation_run_id = NULL,
                   minutes_generation_cancelled_at = ?
               WHERE id = ? AND tenant_id = ? AND owner_id = ?
                 AND (
                       minutes_generation_run_id = ?
                       OR (
                           minutes_generation_run_id IS NULL
                           AND NULLIF(minutes_json, '') IS NULL
                           AND COALESCE(minutes_status, '') <> 'ok'
                           AND minutes_cleared_at IS NULL
                           AND EXISTS (
                               SELECT 1
                               FROM workflow_runs AS legacy_run
                               WHERE legacy_run.run_id = ?
                                 AND legacy_run.tenant_id = meetings.tenant_id
                                 AND legacy_run.owner_id = meetings.owner_id
                                 AND legacy_run.kind = 'meeting.finalize'
                                 AND legacy_run.meeting_id = meetings.id
                                 AND legacy_run.state IN (
                                     'pending', 'running', 'cancel_requested'
                                 )
                           )
                           AND NOT EXISTS (
                               SELECT 1
                               FROM workflow_runs AS other_run
                               WHERE other_run.tenant_id = meetings.tenant_id
                                 AND other_run.owner_id = meetings.owner_id
                                 AND other_run.kind = 'meeting.finalize'
                                 AND other_run.meeting_id = meetings.id
                                 AND other_run.run_id <> ?
                                 AND other_run.state IN (
                                     'pending', 'running', 'cancel_requested'
                                 )
                           )
                       )
                 )""",
            (
                datetime.now(UTC).isoformat(),
                error[:500],
                cancelled_at,
                meeting_id,
                principal.tenant_id,
                principal.owner_id,
                run_id,
                run_id,
                run_id,
            ),
        )
        applied = changed.rowcount == 1
        await changed.close()
        return applied

    async def terminal_projector(
        conn: aiosqlite.Connection,
        run: WorkflowRunRecord,
        state: WorkflowState,
    ) -> None:
        error = {
            "cancelled": "会议纪要生成已取消",
            "timeout": "会议纪要生成超时",
            "failed": "会议纪要生成失败",
            "cancel_failed": "会议纪要取消失败",
        }.get(state, f"会议纪要 workflow 终止：{state}")
        applied = await write_terminal_projection(
            conn,
            run.run_id,
            run.meeting_id,
            state,
            error,
        )
        if not applied or not run.meeting_id:
            return
        await dispatcher.service.append_domain_event_in_transaction(
            conn,
            EchoEvent(
                type="minutes.failed",
                meeting_id=run.meeting_id,
                payload={"error": error},
            ),
            aggregate_id=run.run_id,
        )

    async def attach_generation_owner(
        conn: aiosqlite.Connection,
        run: WorkflowRunRecord,
    ) -> None:
        if not run.meeting_id:
            raise MeetingPipelineError("meeting finalize run has no meeting")
        principal = current_principal()
        cur = await conn.execute(
            """SELECT rag_projection_generation, minutes_generation_run_id,
                      minutes_status, minutes_json, minutes_cleared_at
               FROM meetings
               WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
            (run.meeting_id, principal.tenant_id, principal.owner_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise MeetingPipelineError("meeting finalize target disappeared")
        current_owner = str(row[1]) if row[1] is not None else None
        if current_owner not in {None, run.run_id}:
            raise WorkflowConflictError("meeting finalize generation is owned by another run")
        if str(row[2] or "") == "ok" and row[3] is not None:
            raise WorkflowConflictError("active meeting finalize already has committed minutes")
        if row[4] is not None:
            raise WorkflowConflictError("meeting minutes were explicitly cleared")
        generation = int(row[0] or 0)
        changed = await conn.execute(
            """UPDATE meetings
               SET state = 'ended', ended_at = COALESCE(ended_at, ?),
                   minutes_status = 'generating', minutes_error = '',
                   minutes_cleared_at = NULL, minutes_generation_run_id = ?,
                   minutes_generation_cancelled_at = NULL
               WHERE id = ? AND tenant_id = ? AND owner_id = ?
                 AND rag_projection_generation = ?
                 AND minutes_cleared_at IS NULL
                 AND (minutes_generation_run_id IS NULL OR minutes_generation_run_id = ?)""",
            (
                datetime.now(UTC).isoformat(),
                run.run_id,
                run.meeting_id,
                principal.tenant_id,
                principal.owner_id,
                generation,
                run.run_id,
            ),
        )
        changed_count = changed.rowcount
        await changed.close()
        if changed_count != 1:
            raise WorkflowConflictError("meeting finalize generation attach lost its CAS")

    async def finalize_handler(context: WorkflowContext, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(payload["meeting_id"])
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        active_run = await dispatcher.service.get_run(context.run_id)
        if active_run is None:
            raise MeetingPipelineError("meeting workflow disappeared")
        output = dict(active_run.output)
        domain_commit = output.get("domain_commit")
        if isinstance(domain_commit, dict):
            if domain_commit.get("kind") != "meeting.finalize":
                raise MeetingPipelineError("invalid meeting finalize recovery marker")
            minutes = MeetingMinutes.model_validate(output.get("minutes"))
            committed_generation = int(domain_commit["rag_projection_generation"])
        else:
            attached = await dispatcher.service.project_active_run(
                context.run_id,
                domain_writer=attach_generation_owner,
            )
            if attached is None or attached.is_terminal:
                raise MeetingPipelineError("meeting finalize run disappeared before generation")
            if not pipeline.get_segments(meeting_id):
                loaded = await pipeline.load_meeting_for_retry(meeting_id)
                if not loaded:
                    raise MeetingPipelineError(f"meeting {meeting_id} has no segments to summarize")
            title = str(payload["title"])
            minutes = await pipeline.finalize_meeting(meeting_id, title=title, commit=False)
            now = datetime.now(UTC).isoformat()
            minutes_json = minutes.model_dump_json()
            output = {
                "meeting_id": meeting_id,
                "minutes": minutes.model_dump(mode="json"),
                "domain_commit": {"kind": "meeting.finalize"},
            }

            async def write_success(conn: aiosqlite.Connection) -> None:
                principal = current_principal()
                cur = await conn.execute(
                    """UPDATE meetings
                           SET state = 'finalized', title = ?, display_title = ?,
                           ended_at = COALESCE(ended_at, ?), finalized_at = ?,
                           minutes_json = ?, raw_transcript_ref = ?,
                           minutes_status = 'ok', minutes_error = '', minutes_cleared_at = NULL,
                           rag_projection_state = 'index_pending', rag_projection_error = NULL,
                           rag_projected_at = NULL, rag_projection_attempts = 0,
                           rag_projection_next_retry_at = NULL,
                           rag_projection_generation = rag_projection_generation + 1,
                           minutes_generation_run_id = NULL,
                           minutes_generation_cancelled_at = NULL
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?
                         AND minutes_generation_run_id = ?
                       RETURNING rag_projection_generation""",
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
                        context.run_id,
                    ),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise MeetingPipelineError("stale meeting finalize generation")
                output["domain_commit"]["rag_projection_generation"] = int(row[0])

            committed = await dispatcher.service.commit_run_progress_atomic(
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
                            "text": (f"会议{minutes.title}已结束，纪要已生成。{minutes.summary}")[
                                :400
                            ],
                            "kind": "minutes",
                        },
                    ),
                ],
                event_type="workflow.domain_committed",
                message="会议纪要领域状态已提交",
            )
            if committed is None or committed.is_terminal:
                raise MeetingPipelineError("meeting workflow disappeared before projection")
            committed_generation = int(output["domain_commit"]["rag_projection_generation"])

        if bool(output.get("post_commit_complete")):
            return output
        await pipeline.after_finalize_committed(
            meeting_id,
            minutes,
            expected_generation=committed_generation,
        )
        output["post_commit_complete"] = True
        projected = await dispatcher.service.merge_output(
            context.run_id,
            {"post_commit_complete": True},
            event_type="workflow.rag_projection_attempted",
            message="会议纪要检索投影已处理",
        )
        if projected is None or projected.is_terminal:
            raise MeetingPipelineError("meeting workflow disappeared after projection")
        return output

    dispatcher.registry.register(
        "meeting.finalize",
        finalize_handler,
        scope=scope,
        replace=True,
    )
    dispatcher.registry.register_terminal_projector(
        "meeting.finalize",
        terminal_projector,
        scope=scope,
        replace=True,
    )


def bind_share_workflow_handler(
    dispatcher: WorkflowDispatcher,
    _sessions: SessionStore,
) -> None:
    async def share_handler(_context: WorkflowContext, _payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("share.prepare requires caller-bound inline issuance")

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

    await dispatcher.service.complete_new_run_atomic(
        WorkflowRunCreate(
            kind="share.prepare",
            source=source,
            intent_text=f"Prepare read-only share for {resource_type} {resource_id}",
            input={"resource_type": resource_type, "resource_id": resource_id},
            timeout_s=30,
        ),
        output=output,
        domain_writer=write_ticket,
        message="分享票据已签发",
    )
    token = token_box.get("token")
    if token is None:
        raise RuntimeError("share workflow did not return its one-time token")
    return token


_FILE_CLEANUP_ERROR_IO = "file cleanup failed"
_FILE_CLEANUP_ERROR_PROTECTED = "cleanup target is protected"
_FILE_CLEANUP_ERROR_UNSAFE = "cleanup target is unsafe"
_FILE_CLEANUP_STABLE_ERRORS = {
    _FILE_CLEANUP_ERROR_IO,
    _FILE_CLEANUP_ERROR_PROTECTED,
    _FILE_CLEANUP_ERROR_UNSAFE,
}


def _file_cleanup_errors(output: dict[str, Any]) -> dict[str, str]:
    raw = output.get("file_cleanup_errors")
    if not isinstance(raw, dict):
        return {}
    return {
        str(artifact_id): (
            str(error) if str(error) in _FILE_CLEANUP_STABLE_ERRORS else _FILE_CLEANUP_ERROR_IO
        )
        for artifact_id, error in raw.items()
        if isinstance(artifact_id, str) and _ARTIFACT_ID_RE.fullmatch(artifact_id)
    }


def _cleanup_artifact_id_set(output: dict[str, Any], key: str) -> set[str]:
    raw = output.get(key)
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str) and _ARTIFACT_ID_RE.fullmatch(item)}


def _merge_file_cleanup_output(
    current: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Union concurrent cleanup results against the latest durable receipt."""

    merged = {**current, **patch}
    target_ids = _cleanup_artifact_id_set(merged, "file_cleanup_artifact_ids")
    deleted_ids = _cleanup_artifact_id_set(current, "file_cleanup_deleted_ids")
    deleted_ids.update(_cleanup_artifact_id_set(patch, "file_cleanup_deleted_ids"))
    missing_ids = _cleanup_artifact_id_set(current, "missing_artifact_ids")
    missing_ids.update(_cleanup_artifact_id_set(patch, "missing_artifact_ids"))
    if target_ids:
        deleted_ids.intersection_update(target_ids)
        missing_ids.intersection_update(target_ids)
    missing_ids.difference_update(deleted_ids)
    completed_ids = deleted_ids | missing_ids

    errors = _file_cleanup_errors(current)
    errors.update(_file_cleanup_errors(patch))
    for artifact_id in completed_ids:
        errors.pop(artifact_id, None)

    merged["file_cleanup_deleted_ids"] = sorted(deleted_ids)
    merged["missing_artifact_ids"] = sorted(missing_ids)
    merged["artifacts_deleted"] = max(
        int(current.get("artifacts_deleted") or 0),
        int(patch.get("artifacts_deleted") or 0),
        len(deleted_ids),
    )
    merged["file_cleanup_errors"] = errors
    merged["post_commit_complete"] = not errors
    return merged


async def _replay_cleanup_receipt_files(
    dispatcher: WorkflowDispatcher,
    settings: Settings,
    receipt: WorkflowRunRecord,
) -> WorkflowRunRecord:
    """Replay durable cleanup targets for the current owner and update one receipt."""

    principal = current_principal()
    errors = _file_cleanup_errors(receipt.output)
    if not errors:
        return receipt
    deleted = int(receipt.output.get("artifacts_deleted") or 0)
    deleted_ids = _cleanup_artifact_id_set(receipt.output, "file_cleanup_deleted_ids")
    missing_ids = _cleanup_artifact_id_set(receipt.output, "missing_artifact_ids")
    raw_targets = receipt.output.get("file_cleanup_targets")
    targets = raw_targets if isinstance(raw_targets, list) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        artifact_id = target.get("artifact_id")
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            continue
        if artifact_id not in errors:
            continue
        try:
            outcome = await replay_artifact_file_cleanup_target(
                settings,
                target,
                tenant_id=principal.tenant_id,
                owner_id=principal.owner_id,
            )
            if outcome in {"deleted", "absent"}:
                errors.pop(artifact_id, None)
            elif outcome == "protected":
                errors[artifact_id] = _FILE_CLEANUP_ERROR_PROTECTED
            else:
                errors[artifact_id] = _FILE_CLEANUP_ERROR_UNSAFE
            if outcome == "deleted":
                deleted_ids.add(artifact_id)
                missing_ids.discard(artifact_id)
                deleted = max(deleted, len(deleted_ids))
            elif outcome == "absent" and artifact_id not in deleted_ids:
                missing_ids.add(artifact_id)
        except OSError:
            errors[artifact_id] = _FILE_CLEANUP_ERROR_IO
    updated = await dispatcher.service.merge_output(
        receipt.run_id,
        {
            "artifacts_deleted": deleted,
            "file_cleanup_deleted_ids": sorted(deleted_ids),
            "missing_artifact_ids": sorted(missing_ids),
            "file_cleanup_errors": errors,
            "post_commit_complete": not errors,
        },
        event_type="workflow.file_cleanup_retried",
        message="产物文件清理已重试",
        merge_strategy=_merge_file_cleanup_output,
    )
    if updated is None:
        raise RuntimeError("meeting output cleanup receipt disappeared")
    return updated


def _raise_if_file_cleanup_incomplete(output: dict[str, Any]) -> None:
    if _file_cleanup_errors(output):
        raise HTTPException(
            status_code=503,
            detail="artifact file cleanup incomplete; retry the request",
        )


def bind_output_cleanup_workflow_handler(  # noqa: PLR0915 - one atomic cleanup UoW
    dispatcher: WorkflowDispatcher,
    _repository: RepositoryPort,
    settings: Settings,
    _artifact_repo: ArtifactRepository,
    pipeline: MeetingPipeline,
) -> None:
    principal = current_principal()
    scope = (principal.tenant_id, principal.owner_id)

    async def handler(  # noqa: PLR0912, PLR0915 - durable cleanup plus replayable projections
        context: WorkflowContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        meeting_id = str(payload["meeting_id"])
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        clear_minutes = bool(payload.get("clear_minutes", True))
        cleanup_artifact_ids: list[str] = []
        cleanup_targets: dict[str, dict[str, str]] = {}
        cleanup_generation: int | None = None
        active_run = await dispatcher.service.get_run(context.run_id)
        if active_run is None:
            raise RuntimeError("meeting output cleanup workflow disappeared")
        output = dict(active_run.output)
        domain_commit = output.get("domain_commit")
        if isinstance(domain_commit, dict):
            if domain_commit.get("kind") != "meeting.outputs.clear":
                raise RuntimeError("invalid meeting output cleanup recovery marker")
            raw_generation = domain_commit.get("rag_projection_generation")
            cleanup_generation = int(raw_generation) if raw_generation is not None else None
            cleanup_artifact_ids = [
                str(item) for item in output.get("file_cleanup_artifact_ids", [])
            ]
            cleanup_targets = {
                str(item["artifact_id"]): dict(item)
                for item in output.get("file_cleanup_targets", [])
                if isinstance(item, dict) and isinstance(item.get("artifact_id"), str)
            }
        else:
            output = {
                "meeting_id": meeting_id,
                "minutes_cleared": clear_minutes,
                "artifact_ids": [],
                "artifacts_deleted": 0,
                "missing_artifact_ids": [],
                "file_cleanup_deleted_ids": [],
                # Durable post-commit file cleanup intent. A replacement
                # dispatcher replays this exact owner-scoped target list.
                "file_cleanup_artifact_ids": [],
                "file_cleanup_targets": [],
                "domain_commit": {"kind": "meeting.outputs.clear"},
            }

        async def write_cleanup(conn: aiosqlite.Connection) -> None:
            nonlocal cleanup_generation
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
                artifact = GeneratedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=str(row["artifact_type"]),
                    title=str(row["title"] or ""),
                    file_path=str(row["file_path"]),
                    mime_type=str(row["mime_type"]),
                    size_bytes=int(row["size_bytes"] or 0),
                    generation_latency_ms=float(row["generation_latency_ms"] or 0),
                    model=str(row["model"] or ""),
                    metadata=_artifact_metadata(row["metadata_json"]),
                )
                target = artifact_file_cleanup_target(
                    settings,
                    artifact_id=artifact.artifact_id,
                    file_path=artifact.file_path,
                    tenant_id=active.tenant_id,
                    owner_id=active.owner_id,
                    metadata=artifact.metadata,
                )
                if target is not None:
                    output["file_cleanup_targets"].append(target)
                    cleanup_targets[artifact.artifact_id] = target
                cleanup_artifact_ids.append(artifact.artifact_id)
            output["file_cleanup_artifact_ids"] = list(cleanup_artifact_ids)
            if clear_minutes:
                meeting_cur = await conn.execute(
                    """SELECT minutes_json, minutes_status, minutes_error, display_title,
                              finalized_at, minutes_cleared_at, minutes_generation_run_id,
                              rag_projection_state, rag_projection_generation
                       FROM meetings
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                    (meeting_id, active.tenant_id, active.owner_id),
                )
                meeting_row = await meeting_cur.fetchone()
                await meeting_cur.close()
                if meeting_row is None:
                    raise RuntimeError("meeting output cleanup target disappeared")
                has_uncleared_state = (
                    meeting_row[5] is None
                    or any(meeting_row[index] not in {None, ""} for index in range(5))
                    or meeting_row[6] is not None
                )
                if has_uncleared_state:
                    clear_cur = await conn.execute(
                        """UPDATE meetings SET
                               state = CASE WHEN state = 'finalized' THEN 'ended' ELSE state END,
                               minutes_json = NULL, minutes_status = NULL, minutes_error = NULL,
                               display_title = NULL, finalized_at = NULL,
                               minutes_cleared_at = CURRENT_TIMESTAMP,
                               rag_projection_state = 'delete_pending',
                               rag_projection_error = NULL, rag_projected_at = NULL,
                               rag_projection_attempts = 0,
                               rag_projection_next_retry_at = NULL,
                               rag_projection_generation = rag_projection_generation + 1,
                               minutes_generation_run_id = NULL,
                               minutes_generation_cancelled_at = NULL
                           WHERE id = ? AND tenant_id = ? AND owner_id = ?
                           RETURNING rag_projection_generation""",
                        (meeting_id, active.tenant_id, active.owner_id),
                    )
                    generation_row = await clear_cur.fetchone()
                    await clear_cur.close()
                    if generation_row is None:
                        raise RuntimeError("meeting output cleanup target disappeared")
                    cleanup_generation = int(generation_row[0])
                elif str(meeting_row[7]) in {"delete_pending", "delete_failed"}:
                    cleanup_generation = int(meeting_row[8])
            output["domain_commit"]["rag_projection_generation"] = cleanup_generation

        if not isinstance(domain_commit, dict):
            committed = await dispatcher.service.commit_run_progress_atomic(
                context.run_id,
                output=output,
                domain_writer=write_cleanup,
                domain_events=[],
                event_type="workflow.domain_committed",
                message="会议纪要与产物领域状态已清理",
            )
            if committed is None or committed.is_terminal:
                raise RuntimeError("meeting output cleanup workflow disappeared")

        if bool(output.get("post_commit_complete")):
            return output

        rag_projection_deleted = True
        if clear_minutes and cleanup_generation is not None:
            rag_projection_deleted = await pipeline.delete_meeting_projection(
                meeting_id,
                expected_generation=cleanup_generation,
            )

        deleted = 0
        deleted_ids: set[str] = set()
        missing: list[str] = []
        cleanup_errors: dict[str, str] = {}
        active = current_principal()
        for artifact_id in cleanup_artifact_ids:
            try:
                outcome = await replay_artifact_file_cleanup_target(
                    settings,
                    cleanup_targets.get(artifact_id),
                    tenant_id=active.tenant_id,
                    owner_id=active.owner_id,
                )
                if outcome == "deleted":
                    deleted += 1
                    deleted_ids.add(artifact_id)
                elif outcome == "absent":
                    missing.append(artifact_id)
                elif outcome == "protected":
                    cleanup_errors[artifact_id] = _FILE_CLEANUP_ERROR_PROTECTED
                else:
                    cleanup_errors[artifact_id] = _FILE_CLEANUP_ERROR_UNSAFE
            except OSError:
                cleanup_errors[artifact_id] = _FILE_CLEANUP_ERROR_IO
        output.update(
            artifacts_deleted=deleted,
            file_cleanup_deleted_ids=sorted(deleted_ids),
            missing_artifact_ids=missing,
            file_cleanup_errors=cleanup_errors,
            rag_projection_deleted=rag_projection_deleted,
            post_commit_complete=not cleanup_errors,
        )
        projected = await dispatcher.service.merge_output(
            context.run_id,
            {
                "artifacts_deleted": deleted,
                "file_cleanup_deleted_ids": sorted(deleted_ids),
                "missing_artifact_ids": missing,
                "file_cleanup_errors": cleanup_errors,
                "rag_projection_deleted": rag_projection_deleted,
                "post_commit_complete": not cleanup_errors,
            },
            event_type="workflow.file_cleanup_projected",
            message="产物文件清理投影已更新",
            merge_strategy=_merge_file_cleanup_output,
        )
        if projected is None or projected.is_terminal:
            raise RuntimeError("meeting output cleanup workflow disappeared after projection")
        return dict(projected.output)

    dispatcher.registry.register(
        "meeting.outputs.clear",
        handler,
        scope=scope,
        replace=True,
    )


async def dispatch_meeting_finalize(  # noqa: PLR0915 - explicit winner adoption lifecycle
    dispatcher: WorkflowDispatcher,
    pipeline: MeetingPipeline,
    repository: RepositoryPort,
    *,
    meeting_id: str,
    title: str,
    source: str,
) -> MeetingMinutes:
    bind_meeting_workflow_handlers(dispatcher, pipeline)
    active_key = f"meeting.finalize:{meeting_id}"

    async def wait_for_active_finalize() -> bool:
        active = await dispatcher.service.get_active_by_active_key(active_key)
        if active is None:
            return False
        if active.kind != "meeting.finalize" or active.meeting_id != meeting_id:
            raise WorkflowConflictError("active workflow cannot own this meeting finalize")
        try:
            await dispatcher.wait_succeeded(active.run_id)
        except WorkflowExecutionError as exc:
            raise MeetingPipelineError(str(exc)) from exc
        return True

    while True:
        if await wait_for_active_finalize():
            continue
        meeting = await repository.get_meeting(meeting_id)
        # Close the read race where another instance creates the active run
        # after our first lookup and commits minutes before this meeting read.
        if await wait_for_active_finalize():
            continue
        break
    if meeting is not None and meeting.minutes_json and meeting.minutes_status == "ok":
        return MeetingMinutes.model_validate_json(meeting.minutes_json)

    finalize_runs = [
        item
        for item in await dispatcher.service.list_runs(meeting_id=meeting_id, limit=200)
        if item.kind == "meeting.finalize"
    ]
    latest = finalize_runs[0] if finalize_runs else None
    expected_generation = meeting.rag_projection_generation if meeting is not None else 0
    generation_started_at = datetime.now(UTC).isoformat()

    async def write_generation_marker(
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> None:
        principal = current_principal()
        changed = await conn.execute(
            """UPDATE meetings
               SET state = 'ended', ended_at = COALESCE(ended_at, ?),
                   minutes_status = 'generating', minutes_error = '',
                   minutes_cleared_at = NULL, minutes_generation_run_id = ?,
                   minutes_generation_cancelled_at = NULL
               WHERE id = ? AND tenant_id = ? AND owner_id = ?
                 AND rag_projection_generation = ?
                 AND (minutes_generation_run_id IS NULL OR minutes_generation_run_id = ?)""",
            (
                generation_started_at,
                run_id,
                meeting_id,
                principal.tenant_id,
                principal.owner_id,
                expected_generation,
                run_id,
            ),
        )
        updated = changed.rowcount == 1
        await changed.close()
        if not updated:
            raise WorkflowConflictError("meeting finalize generation was superseded")

    async def adopt_authoritative_run(candidate: WorkflowRunRecord) -> WorkflowRunRecord:
        if (
            candidate.kind != "meeting.finalize"
            or candidate.meeting_id != meeting_id
            or str(candidate.input.get("meeting_id") or "") != meeting_id
            or not isinstance(candidate.input.get("title"), str)
        ):
            raise WorkflowConflictError("active workflow cannot own this meeting finalize")
        if candidate.is_terminal:
            if candidate.state == "succeeded":
                return candidate
            raise WorkflowConflictError("meeting finalize winner is already terminal")
        current = await repository.get_meeting(meeting_id)
        if current is None:
            raise WorkflowConflictError("meeting finalize target disappeared")
        projected = candidate
        if current.minutes_generation_run_id != candidate.run_id:
            attached = await dispatcher.service.project_active_run(
                candidate.run_id,
                domain_writer=lambda conn, run: write_generation_marker(conn, run.run_id),
            )
            if attached is None or (attached.is_terminal and attached.state != "succeeded"):
                raise WorkflowConflictError("active meeting workflow cannot be adopted")
            projected = attached
            if projected.is_terminal:
                return projected
        scheduled = await dispatcher.dispatch(
            WorkflowRunCreate(
                kind="meeting.finalize",
                source=projected.source,
                title=projected.title,
                intent_text=projected.intent_text,
                meeting_id=meeting_id,
                input=dict(projected.input),
                timeout_s=projected.timeout_s,
                idempotency_key=projected.idempotency_key,
                active_key=projected.active_key or active_key,
            )
        )
        if scheduled.run_id != projected.run_id:
            raise WorkflowConflictError("another meeting finalize became authoritative")
        return scheduled

    try:
        if latest is not None and not latest.is_terminal:
            run = latest
        elif latest is not None and latest.state != "succeeded":
            retried = await dispatcher.retry(
                latest.run_id,
                reason="meeting minutes retry",
                domain_writer=lambda conn, child: write_generation_marker(conn, child.run_id),
            )
            if retried is None:
                raise WorkflowConflictError("meeting workflow retry was not created")
            run = retried
        else:
            run_id = new_workflow_run_id()

            run = await dispatcher.dispatch_atomic(
                WorkflowRunCreate(
                    kind="meeting.finalize",
                    source=source,
                    title=title,
                    intent_text=f"Finalize meeting {meeting_id}",
                    meeting_id=meeting_id,
                    input={"meeting_id": meeting_id, "title": title},
                    timeout_s=300,
                    idempotency_key=f"meeting.finalize:{meeting_id}:run:{run_id}",
                    active_key=active_key,
                ),
                domain_writer=lambda conn: write_generation_marker(conn, run_id),
                run_id=run_id,
            )
        run = await adopt_authoritative_run(run)
    except WorkflowConflictError:
        winner = await dispatcher.service.get_active_by_active_key(active_key)
        if winner is None:
            winner = next(
                (
                    item
                    for item in await dispatcher.service.list_runs(
                        meeting_id=meeting_id,
                        limit=20,
                    )
                    if item.kind == "meeting.finalize" and item.active_key == active_key
                ),
                None,
            )
        if winner is None:
            raise
        run = await adopt_authoritative_run(winner)
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
    principal = current_principal()
    return validated_artifact_file_path(
        settings,
        artifact_id=artifact.artifact_id,
        file_path=artifact.file_path,
        tenant_id=principal.tenant_id,
        owner_id=principal.owner_id,
        metadata=artifact.metadata,
    )


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
        return project_client_dict(
            {
                "mode": "in_meeting",
                "meeting_id": cur.meeting_id,
                "started_at": cur.started_at.isoformat(),
                "started_by": cur.started_by,
                "minutes_status": None,
                "minutes_error": None,
            },
            current_principal(),
        )
    # idle：探一下最近一条 meeting，把它的 minutes_status 透传出来
    latest = await repository.list_meetings(limit=1)
    latest_rec = latest[0] if latest else None
    return project_client_dict(
        {
            "mode": "idle",
            "meeting_id": None,
            "started_at": None,
            "started_by": None,
            "minutes_status": latest_rec.minutes_status if latest_rec else None,
            "minutes_error": latest_rec.minutes_error if latest_rec else None,
        },
        current_principal(),
    )


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
    return _minutes_dto(MeetingMinutes(**data))


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


@router.get("/{meeting_id}/artifacts", response_model=list[GeneratedArtifactDTO])
async def get_meeting_artifacts(
    meeting_id: str,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
) -> list[GeneratedArtifactDTO]:
    """单会议产物（右下 outputs 面板用），以 artifact_links 为事实源。"""
    meeting = await repository.get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    return [
        _artifact_dto(artifact)
        for artifact in await artifact_repo.list_meeting_artifacts(meeting_id)
    ]


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
async def clear_meeting_outputs(  # noqa: PLR0912 - active receipt arbitration is explicit
    meeting_id: str,
    body: ClearMeetingOutputsRequest,
    repository: Annotated[RepositoryPort, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    artifact_repo: Annotated[ArtifactRepository, Depends(get_artifact_repository)],
    dispatcher: Annotated[WorkflowDispatcher, Depends(get_workflow_dispatcher)],
    pipeline: Annotated[MeetingPipeline, Depends(get_meeting_pipeline)],
) -> ClearMeetingOutputsResponse:
    """清理会议纪要与产物文件。

    0.3 起 artifact_links 是唯一事实源。请求体里的 ``artifact_ids`` 保留为
    旧客户端兼容字段，但不会被用于扩大删除范围。
    """
    _ = body.artifact_ids
    bind_output_cleanup_workflow_handler(
        dispatcher,
        repository,
        settings,
        artifact_repo,
        pipeline,
    )
    active_key = f"meeting.outputs.clear:{meeting_id}"
    for _attempt in range(8):
        active = await dispatcher.service.get_active_by_active_key(active_key)
        if active is not None:
            try:
                await dispatcher.wait_succeeded(active.run_id)
            except WorkflowExecutionError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            continue
        meeting = await repository.get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="meeting not found")
        current_artifacts = await artifact_repo.list_meeting_artifacts(meeting_id)
        # Close the same read race as finalize: never reuse a succeeded receipt
        # while another instance still owns an active post-domain tail.
        active = await dispatcher.service.get_active_by_active_key(active_key)
        if active is not None:
            try:
                await dispatcher.wait_succeeded(active.run_id)
            except WorkflowExecutionError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            continue
        request_satisfied = not current_artifacts and (
            not body.clear_minutes
            or (
                meeting.minutes_cleared_at is not None
                and meeting.minutes_json is None
                and meeting.minutes_status is None
                and meeting.finalized_at is None
            )
        )
        if request_satisfied:
            receipts = await dispatcher.service.list_runs(meeting_id=meeting_id, limit=200)
            receipt = next(
                (
                    item
                    for item in receipts
                    if item.kind == "meeting.outputs.clear"
                    and item.state == "succeeded"
                    and bool(item.input.get("clear_minutes", True)) is body.clear_minutes
                ),
                None,
            )
            if receipt is not None:
                receipt = await _replay_cleanup_receipt_files(
                    dispatcher,
                    settings,
                    receipt,
                )
                if body.clear_minutes and meeting.rag_projection_state in {
                    "delete_pending",
                    "delete_failed",
                }:
                    projection_deleted = await pipeline.delete_meeting_projection(
                        meeting_id,
                        expected_generation=meeting.rag_projection_generation,
                    )
                    updated_receipt = await dispatcher.service.merge_output(
                        receipt.run_id,
                        {"rag_projection_deleted": projection_deleted},
                        event_type="workflow.rag_projection_retried",
                        message="会议清理检索投影已重试",
                    )
                    if updated_receipt is not None:
                        receipt = updated_receipt
                _raise_if_file_cleanup_incomplete(receipt.output)
                return ClearMeetingOutputsResponse.model_validate(receipt.output)
        fingerprint: dict[str, object] = {
            "artifact_ids": sorted(item.artifact_id for item in current_artifacts),
            "clear_minutes": body.clear_minutes,
        }
        if body.clear_minutes:
            fingerprint["minutes_generation"] = {
                "finalized_at": meeting.finalized_at.isoformat() if meeting.finalized_at else None,
                "minutes_sha256": hashlib.sha256((meeting.minutes_json or "").encode()).hexdigest(),
                "minutes_status": meeting.minutes_status,
                "rag_projection_generation": meeting.rag_projection_generation,
            }
        digest = hashlib.sha256(
            json.dumps(
                fingerprint,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        idempotency_key = f"meeting.outputs.clear:{meeting_id}:{body.clear_minutes}:{digest}"
        try:
            done = await dispatcher.execute(
                WorkflowRunCreate(
                    kind="meeting.outputs.clear",
                    source="meeting_outputs_api",
                    intent_text=f"Clear outputs for meeting {meeting_id}",
                    meeting_id=meeting_id,
                    input={"meeting_id": meeting_id, "clear_minutes": body.clear_minutes},
                    timeout_s=120,
                    idempotency_key=idempotency_key,
                    active_key=active_key,
                )
            )
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if done.idempotency_key == idempotency_key:
            _raise_if_file_cleanup_incomplete(done.output)
            return ClearMeetingOutputsResponse.model_validate(done.output)
        # We joined a different request that already owned the meeting lane.
        # Its handler is terminal now; refresh domain state and submit the
        # caller's request against the new authoritative fingerprint.
    raise HTTPException(status_code=409, detail="meeting output cleanup is busy")


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
        minutes = await dispatch_meeting_finalize(
            dispatcher,
            pipeline,
            repository,
            meeting_id=meeting_id,
            title=title,
            source="meeting_api",
        )
        return _minutes_dto(minutes)
    except MeetingPipelineError as exc:
        status_code = 400 if "no segments" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except WorkflowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
