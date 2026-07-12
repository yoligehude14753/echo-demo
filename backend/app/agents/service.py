"""Agent task service：持久化、授权、AgentOS bridge 与 EchoEvent 广播。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import aiosqlite
import httpx

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.agents.agentos import (
    AGENTOS_SUBMIT_MAX_WALL_S,
    AgentOSBackend,
    submit_operation_key,
)
from app.agents.artifact_transfer import (
    ArtifactContentLengthError,
    ArtifactDownloadResult,
    ArtifactSizeLimitError,
    download_artifact_to_path,
)
from app.agents.base import AgentIntent, AgentSubmitResult, AgentTaskState, new_echo_task_id
from app.agents.command_outbox import (
    AgentCommandOutbox,
    AgentCommandOutcome,
    AgentCommandRecord,
)
from app.agents.events import (
    EchoTaskEvent,
    default_snapshot,
    reduce_snapshot,
    utc_now_iso,
)
from app.agents.stream_bridge import EchoTaskStreamBridge
from app.artifacts.repository import ArtifactRepository
from app.config import Settings
from app.runtime.execution_lease import ExecutionLeaseStore, LeaseOwnershipError, LeaseToken
from app.schemas.artifact import GeneratedArtifact
from app.schemas.events import EchoEvent
from app.schemas.workflow import WorkflowRunCreate
from app.security.context import bind_principal, current_principal, reset_principal
from app.security.models import LEGACY_OWNER_ID, Principal
from app.security.scope import physical_resource_id, scoped_directory
from app.workflows.service import (
    WorkflowService,
    get_workflow_service,
)

_log = logging.getLogger("echodesk.agents")

RUNNER_CLAUDE_CODE = "claude_code"
PROFILE_FULL_ACCESS = "claude_code_full_access"
PERMISSION_MODE_BYPASS = "bypassPermissions"


def _scope() -> tuple[str, str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.device_id, principal.owner_id


def _effective_device_id(requested: str) -> str:
    principal = current_principal()
    return principal.device_id if principal.mode == "public" else requested


@dataclass(slots=True)
class AgentTaskRecord:
    task_id: str
    tenant_id: str
    owner_id: str
    device_id: str
    title: str
    intent_text: str
    state: AgentTaskState
    runner_task_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    route: str = RUNNER_CLAUDE_CODE
    task_kind: str | None = None
    progress_text: str = ""
    final_text: str | None = None
    error: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    envelope: dict[str, Any] = field(default_factory=dict)
    grant_id: str | None = None
    permission_profile: str | None = None
    workflow_run_id: str | None = None
    last_seq: int = 0
    submitted_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    bridge_completed_at: str | None = None
    timeout_s: float = 1800.0


@dataclass(slots=True)
class AgentRunnerGrant:
    grant_id: str
    device_id: str
    runner: str
    permission_profile: str
    permission_mode: str
    workspace_ids: list[str]
    granted_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None


def _title_from_text(text: str) -> str:
    title = " ".join((text or "").strip().split())
    return title[:42] if title else "EchoDesk 正在执行"


def _json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _state(raw: str | None) -> AgentTaskState:
    try:
        return AgentTaskState(raw or AgentTaskState.PENDING.value)
    except ValueError:
        return AgentTaskState.PENDING


def _context_str(context: dict[str, Any], key: str) -> str | None:
    value = context.get(key)
    return str(value) if value not in (None, "") else None


def _workflow_source(intent: AgentIntent) -> str:
    origin = _context_str(intent.context, "origin")
    if origin in {"command", "todo", "retry"}:
        return origin
    return "todo" if _context_str(intent.context, "todo_id") else "command"


def _encode_agentos_artifact_path(relpath: str) -> str:
    path = PurePosixPath(relpath)
    if path.is_absolute():
        return ""
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(quote(part, safe="") for part in parts)


def _cache_relpath(task_id: str, relpath: str) -> Path | None:
    path = PurePosixPath(relpath)
    if path.is_absolute():
        return None
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    task_dir = physical_resource_id(task_id, kind="agent-task")
    return scoped_directory(Path("agent_artifacts")) / task_dir / Path(*parts)


def _agent_artifact_id(task_id: str, relpath: str) -> str:
    digest = hashlib.sha1(f"{task_id}:{relpath}".encode()).hexdigest()[:24]
    return f"agent-{digest}"


def _row_to_record(row: aiosqlite.Row) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        owner_id=row["owner_id"],
        runner_task_id=row["runner_task_id"],
        device_id=row["device_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        title=row["title"],
        intent_text=row["intent_text"],
        route=row["route"],
        task_kind=row["task_kind"],
        state=_state(row["state"]),
        progress_text=row["progress_text"] or "",
        final_text=row["final_text"],
        error=row["error"],
        artifacts=_json(row["artifacts_json"], []),
        snapshot=_json(row["snapshot_json"], {}),
        envelope=_json(row["envelope_json"], {}),
        grant_id=row["grant_id"],
        permission_profile=row["permission_profile"],
        workflow_run_id=row["workflow_run_id"],
        last_seq=int(row["last_seq"] or 0),
        submitted_at=row["submitted_at"],
        finished_at=row["finished_at"],
        bridge_completed_at=row["bridge_completed_at"],
        timeout_s=float(row["timeout_s"] or 1800.0),
    )


def _row_to_grant(row: aiosqlite.Row) -> AgentRunnerGrant:
    return AgentRunnerGrant(
        grant_id=row["grant_id"],
        device_id=row["device_id"],
        runner=row["runner"],
        permission_profile=row["permission_profile"],
        permission_mode=row["permission_mode"],
        workspace_ids=[str(x) for x in _json(row["workspace_ids_json"], [])],
        granted_at=row["granted_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
    )


class AgentTaskService:
    """Manage authoritative Agent commands and their workflow projection.

    ``agent_tasks`` is authoritative for external runner state. ``workflow_runs``
    is the user-visible lifecycle projection; initial rows are committed in one
    SQLite Unit of Work and terminal drift is repaired during startup restore.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: InMemoryEventBus,
        *,
        workflow: WorkflowService | None = None,
        holder_id: str | None = None,
        bridge_lease_ttl_seconds: float | None = None,
        bridge_heartbeat_seconds: float | None = None,
        bridge_recovery_interval_seconds: float | None = None,
        bridge_retry_base_seconds: float | None = None,
        bridge_retry_max_seconds: float | None = None,
        submit_lease_ttl_seconds: float | None = None,
        submit_heartbeat_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.backend = AgentOSBackend(settings)
        self.workflow = workflow or WorkflowService(settings, event_bus)
        self.artifact_repo = ArtifactRepository(settings)
        lease_ttl = (
            bridge_lease_ttl_seconds
            if bridge_lease_ttl_seconds is not None
            else settings.agent_bridge_lease_ttl_s
        )
        heartbeat = (
            bridge_heartbeat_seconds
            if bridge_heartbeat_seconds is not None
            else settings.agent_bridge_heartbeat_s
        )
        recovery_interval = (
            bridge_recovery_interval_seconds
            if bridge_recovery_interval_seconds is not None
            else settings.agent_bridge_recovery_interval_s
        )
        retry_base = (
            bridge_retry_base_seconds
            if bridge_retry_base_seconds is not None
            else settings.agent_bridge_retry_base_s
        )
        retry_max = (
            bridge_retry_max_seconds
            if bridge_retry_max_seconds is not None
            else settings.agent_bridge_retry_max_s
        )
        submit_lease_ttl = (
            submit_lease_ttl_seconds
            if submit_lease_ttl_seconds is not None
            else settings.agent_submit_lease_ttl_s
        )
        submit_heartbeat = (
            submit_heartbeat_seconds
            if submit_heartbeat_seconds is not None
            else settings.agent_submit_heartbeat_s
        )
        if lease_ttl <= 0 or heartbeat <= 0 or heartbeat >= lease_ttl:
            raise ValueError("bridge heartbeat must be positive and shorter than lease TTL")
        if recovery_interval <= 0 or retry_base <= 0 or retry_max < retry_base:
            raise ValueError("bridge recovery intervals must be positive and bounded")
        if submit_lease_ttl <= 0 or submit_heartbeat <= 0 or submit_heartbeat >= submit_lease_ttl:
            raise ValueError("submit heartbeat must be positive and shorter than lease TTL")
        if submit_lease_ttl_seconds is None and submit_lease_ttl <= AGENTOS_SUBMIT_MAX_WALL_S:
            raise ValueError("submit lease TTL must exceed the AgentOS retry window")
        self._holder_id = holder_id or f"agent:{os.getpid()}:{uuid.uuid4().hex}"
        self._lease_store = ExecutionLeaseStore(settings.db_path)
        self._command_outbox = AgentCommandOutbox(settings.db_path, self._lease_store)
        self._bridge_lease_ttl_seconds = lease_ttl
        self._bridge_heartbeat_seconds = heartbeat
        self._bridge_recovery_interval_seconds = recovery_interval
        self._bridge_retry_base_seconds = retry_base
        self._bridge_retry_max_seconds = retry_max
        self._submit_lease_ttl_seconds = submit_lease_ttl
        self._submit_heartbeat_seconds = submit_heartbeat
        self._cancel_command_lease_ttl_seconds = settings.agent_cancel_command_lease_ttl_s
        self._cancel_command_retry_base_seconds = settings.agent_cancel_command_retry_base_s
        self._cancel_command_retry_max_seconds = settings.agent_cancel_command_retry_max_s
        self._cancel_command_max_attempts = settings.agent_cancel_command_max_attempts
        if self._cancel_command_retry_max_seconds < self._cancel_command_retry_base_seconds:
            raise ValueError("cancel command retry maximum must cover its base delay")
        self._bridge_tasks: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._bridge_retry_attempts: dict[tuple[str, str, str], int] = {}
        self._bridge_retry_at: dict[tuple[str, str, str], float] = {}
        self._recovery_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with open_aiosqlite_connection(self.settings.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await configure_aiosqlite_connection(conn)
            yield conn

    async def get_active_grant(
        self,
        *,
        device_id: str,
        runner: str = RUNNER_CLAUDE_CODE,
    ) -> AgentRunnerGrant | None:
        tenant_id, _principal_device_id, owner_id = _scope()
        device_id = _effective_device_id(device_id)
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM agent_runner_grants
                   WHERE device_id = ? AND runner = ? AND revoked_at IS NULL
                     AND tenant_id = ? AND owner_id = ?
                   ORDER BY granted_at DESC LIMIT 1""",
                (device_id, runner, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_grant(row) if row else None

    async def create_grant(
        self,
        *,
        device_id: str,
        workspace_ids: list[str] | None = None,
        permission_profile: str = PROFILE_FULL_ACCESS,
    ) -> AgentRunnerGrant:
        principal = current_principal()
        tenant_id, _principal_device_id, owner_id = _scope()
        device_id = _effective_device_id(device_id)
        existing = await self.get_active_grant(device_id=device_id)
        if existing and existing.permission_profile == permission_profile:
            return existing
        grant_id = f"grant_{hashlib.sha1(f'{device_id}:{utc_now_iso()}'.encode()).hexdigest()[:24]}"
        now = utc_now_iso()
        async with self._conn() as conn:
            if principal.mode == "local":
                await conn.execute(
                    """INSERT OR IGNORE INTO devices
                       (tenant_id, user_id, device_id, created_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (tenant_id, owner_id, device_id, now, now),
                )
            await conn.execute(
                """INSERT INTO agent_runner_grants
                   (grant_id, device_id, runner, permission_profile, permission_mode,
                    workspace_ids_json, granted_at, revoked_at, last_used_at,
                    tenant_id, owner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                (
                    grant_id,
                    device_id,
                    RUNNER_CLAUDE_CODE,
                    permission_profile,
                    PERMISSION_MODE_BYPASS,
                    json.dumps(workspace_ids or [], ensure_ascii=False),
                    now,
                    tenant_id,
                    owner_id,
                ),
            )
            await conn.commit()
        return AgentRunnerGrant(
            grant_id=grant_id,
            device_id=device_id,
            runner=RUNNER_CLAUDE_CODE,
            permission_profile=permission_profile,
            permission_mode=PERMISSION_MODE_BYPASS,
            workspace_ids=workspace_ids or [],
            granted_at=now,
        )

    async def touch_grant(self, grant_id: str) -> None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE agent_runner_grants SET last_used_at = ? WHERE grant_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (utc_now_iso(), grant_id, tenant_id, owner_id),
            )
            await conn.commit()

    async def revoke_grant(self, grant_id: str) -> bool:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """UPDATE agent_runner_grants
                   SET revoked_at = ?
                   WHERE grant_id = ? AND revoked_at IS NULL
                     AND tenant_id = ? AND owner_id = ?""",
                (utc_now_iso(), grant_id, tenant_id, owner_id),
            )
            await conn.commit()
            return bool(cur.rowcount)

    async def submit_task(self, intent: AgentIntent) -> AgentTaskRecord:
        intent.device_id = _effective_device_id(intent.device_id)
        intent.echo_task_id = intent.echo_task_id or new_echo_task_id()
        intent.title = intent.title or _title_from_text(intent.text)
        grant = await self.get_active_grant(device_id=intent.device_id)
        if grant is None:
            rec, created = await self._create_task_with_run(
                intent, state=AgentTaskState.WAITING_PERMISSION
            )
            if not created:
                if rec.state == AgentTaskState.WAITING_PERMISSION and rec.last_seq == 0:
                    return await self._record_permission_required_event(rec)
                return rec
            return await self._record_permission_required_event(rec)
        intent.grant_id = grant.grant_id
        intent.permission_profile = grant.permission_profile
        # Persist the authoritative local command before calling AgentOS.  If
        # the process dies around the remote call, startup resubmits the same
        # echo_task_id, which is the provider idempotency key.
        rec, _created = await self._create_task_with_run(intent, state=AgentTaskState.PENDING)
        return await self.resume_with_grant(rec.task_id, grant)

    def _workflow_create(self, intent: AgentIntent) -> WorkflowRunCreate:
        task_id = intent.echo_task_id or new_echo_task_id()
        return WorkflowRunCreate(
            kind="agent_task",
            source=_workflow_source(intent),
            title=intent.title,
            intent_text=intent.text,
            meeting_id=_context_str(intent.context, "meeting_id"),
            todo_id=_context_str(intent.context, "todo_id"),
            agent_task_id=task_id,
            input={
                "device_id": intent.device_id,
                "conversation_id": intent.conversation_id,
                "message_id": intent.message_id,
                "task_kind": intent.task_kind,
                "context": intent.context,
                "output_contract": intent.output_contract,
                "runner": RUNNER_CLAUDE_CODE,
            },
            timeout_s=intent.timeout_s,
            idempotency_key=f"agent-task:{task_id}",
        )

    async def _create_task_with_run(
        self,
        intent: AgentIntent,
        *,
        state: AgentTaskState,
    ) -> tuple[AgentTaskRecord, bool]:
        """Atomically create the authoritative task and workflow projection."""

        tenant_id, _device_id, owner_id = _scope()
        task_id = intent.echo_task_id or new_echo_task_id()
        intent.echo_task_id = task_id
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND tenant_id = ? AND owner_id = ?",
                (task_id, tenant_id, owner_id),
            )
            existing = await cur.fetchone()
            await cur.close()
            if existing is not None:
                rec = _row_to_record(existing)
                created = False
            else:
                run, _created = await self.workflow.create_run_in_transaction(
                    conn,
                    self._workflow_create(intent),
                )
                rec = await self._insert_task_tx(
                    conn,
                    intent=intent,
                    result=AgentSubmitResult(
                        task_id=task_id,
                        accepted=True,
                        provider=RUNNER_CLAUDE_CODE,
                    ),
                    state=state,
                    workflow_run_id=run.run_id,
                )
                await conn.commit()
                created = True
        await self.workflow.flush_outbox()
        return rec, created

    async def record_permission_required(
        self,
        intent: AgentIntent,
        *,
        workflow_run_id: str | None,
    ) -> AgentTaskRecord:
        rec = await self._insert_task(
            intent=intent,
            result=AgentSubmitResult(
                task_id=intent.echo_task_id or new_echo_task_id(),
                accepted=True,
                provider=RUNNER_CLAUDE_CODE,
            ),
            state=AgentTaskState.WAITING_PERMISSION,
            workflow_run_id=workflow_run_id,
        )
        return await self._record_permission_required_event(rec)

    async def _record_permission_required_event(
        self,
        rec: AgentTaskRecord,
    ) -> AgentTaskRecord:
        await self.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                conversation_id=rec.conversation_id,
                message_id=rec.message_id,
                title=rec.title,
                event="task.permission_required",
                state="waiting_permission",
                message="需要授权后才能开始执行",
                actions=[
                    {"id": "grant_and_start", "label": "允许并开始"},
                    {"id": "cancel", "label": "取消"},
                ],
                permission={
                    "permission_profile": PROFILE_FULL_ACCESS,
                    "permission_mode": PERMISSION_MODE_BYPASS,
                    "title": "启用 EchoDesk Agent 执行能力",
                    "message": "允许 EchoDesk 在授权工作区内运行后台任务、访问网络并生成文件。",
                },
            )
        )
        return await self.get_task(rec.task_id) or rec

    async def resume_with_grant(  # noqa: PLR0911, PLR0912, PLR0915 - fenced submit lifecycle stays linear
        self,
        task_id: str,
        grant: AgentRunnerGrant,
    ) -> AgentTaskRecord:
        rec = await self.get_task(task_id)
        if rec is None:
            raise KeyError(task_id)
        if rec.state not in {AgentTaskState.WAITING_PERMISSION, AgentTaskState.PENDING}:
            return rec
        if rec.state == AgentTaskState.PENDING and rec.runner_task_id:
            return rec
        if grant.device_id != rec.device_id:
            raise PermissionError("grant device does not match task device")
        tenant_id, _device_id, owner_id = _scope()
        lease = await self._lease_store.acquire(
            tenant_id=tenant_id,
            owner_id=owner_id,
            resource_kind="agent_submit",
            resource_id=rec.task_id,
            holder_id=self._holder_id,
            ttl_seconds=self._submit_lease_ttl_seconds,
        )
        if lease is None:
            return await self.get_task(rec.task_id) or rec

        heartbeat = asyncio.create_task(
            self._submit_lease_heartbeat(lease),
            name=f"agent-submit-heartbeat:{rec.task_id}",
        )
        submitted = False
        requeued_cancel = False
        try:
            rec = await self.get_task(rec.task_id) or rec
            if rec.state not in {
                AgentTaskState.WAITING_PERMISSION,
                AgentTaskState.PENDING,
            }:
                return rec
            if rec.runner_task_id:
                return rec

            raw_context = rec.envelope.get("context")
            context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
            raw_output_contract = rec.envelope.get("output_contract")
            output_contract: dict[str, Any] = (
                raw_output_contract if isinstance(raw_output_contract, dict) else {}
            )
            intent = AgentIntent(
                text=rec.intent_text,
                device_id=rec.device_id,
                echo_task_id=rec.task_id,
                conversation_id=rec.conversation_id,
                message_id=rec.message_id,
                title=rec.title,
                task_kind=rec.task_kind,
                context=context,
                output_contract=output_contract,
                grant_id=grant.grant_id,
                permission_profile=grant.permission_profile,
                timeout_s=rec.timeout_s,
                runner_operation_key=submit_operation_key(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    task_id=rec.task_id,
                ),
            )
            result = await self.backend.submit(intent)
            if not result.accepted or not result.runner_task_id:
                latest = await self.get_task(rec.task_id) or rec
                if latest.state.is_terminal:
                    return latest
                try:
                    await self.record_task_event(
                        EchoTaskEvent(
                            task_id=rec.task_id,
                            title=rec.title,
                            event="task.failed",
                            state="failed",
                            message=result.error or "任务暂时无法启动",
                        ),
                        submit_lease_token=lease,
                    )
                except LeaseOwnershipError:
                    return await self.get_task(rec.task_id) or rec
                return await self.get_task(rec.task_id) or rec

            await self.touch_grant(grant.grant_id)
            async with self._conn() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await self._lease_store.assert_owned(lease, conn=conn)
                    changed = await conn.execute(
                        """UPDATE agent_tasks
                           SET runner_task_id = ?, state = 'pending', grant_id = ?,
                               permission_profile = ?, progress_text = ?
                           WHERE task_id = ? AND tenant_id = ? AND owner_id = ?
                             AND state IN ('waiting_permission', 'pending')
                             AND runner_task_id IS NULL""",
                        (
                            result.runner_task_id,
                            grant.grant_id,
                            grant.permission_profile,
                            "任务已提交，等待执行",
                            rec.task_id,
                            tenant_id,
                            owner_id,
                        ),
                    )
                    submitted = changed.rowcount == 1
                    await changed.close()
                    if not submitted:
                        await conn.execute(
                            """UPDATE agent_tasks
                               SET runner_task_id = COALESCE(runner_task_id, ?),
                                   grant_id = COALESCE(grant_id, ?),
                                   permission_profile = COALESCE(permission_profile, ?)
                               WHERE task_id = ? AND tenant_id = ? AND owner_id = ?""",
                            (
                                result.runner_task_id,
                                grant.grant_id,
                                grant.permission_profile,
                                rec.task_id,
                                tenant_id,
                                owner_id,
                            ),
                        )
                        requeued_cancel = await self._command_outbox.attach_runner_and_requeue_cancel_in_transaction(
                            conn,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                            task_id=rec.task_id,
                            runner_task_id=result.runner_task_id,
                        )
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._lease_store.release(lease)

        if not submitted:
            if requeued_cancel:
                await self.recover_cancel_commands_once(task_id=rec.task_id, limit=1)
            latest = await self.get_task(rec.task_id) or rec
            if latest.runner_task_id and not latest.state.is_terminal:
                self.start_bridge_for_task(latest)
            return latest

        rec = await self.get_task(rec.task_id) or rec
        await self.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                conversation_id=rec.conversation_id,
                message_id=rec.message_id,
                title=rec.title,
                event="task.queued",
                state="pending",
                message="任务已提交，等待执行",
            )
        )
        self.start_bridge_for_task(rec)
        return await self.get_task(rec.task_id) or rec

    async def _submit_lease_heartbeat(self, lease: LeaseToken) -> None:
        while True:
            await asyncio.sleep(self._submit_heartbeat_seconds)
            renewed = await self._lease_store.renew(
                lease,
                ttl_seconds=self._submit_lease_ttl_seconds,
            )
            if renewed is None:
                return

    async def _insert_task(
        self,
        *,
        intent: AgentIntent,
        result: AgentSubmitResult,
        state: AgentTaskState,
        workflow_run_id: str | None,
    ) -> AgentTaskRecord:
        async with self._conn() as conn:
            rec = await self._insert_task_tx(
                conn,
                intent=intent,
                result=result,
                state=state,
                workflow_run_id=workflow_run_id,
            )
            await conn.commit()
        return rec

    async def _insert_task_tx(
        self,
        conn: aiosqlite.Connection,
        *,
        intent: AgentIntent,
        result: AgentSubmitResult,
        state: AgentTaskState,
        workflow_run_id: str | None,
    ) -> AgentTaskRecord:
        now = utc_now_iso()
        tenant_id, principal_device_id, owner_id = _scope()
        task_id = intent.echo_task_id or result.task_id or new_echo_task_id()
        title = intent.title or _title_from_text(intent.text)
        snapshot = default_snapshot(title=title, status=state.value)
        if state == AgentTaskState.WAITING_PERMISSION:
            snapshot["progress_text"] = "等待授权"
        elif state == AgentTaskState.PENDING:
            snapshot["progress_text"] = "任务已提交，等待执行"
        envelope = {
            "source": "echodesk",
            "echo_task_id": task_id,
            "runner_task_id": result.runner_task_id,
            "device_id": intent.device_id,
            "user_text": intent.text,
            "user_visible_title": title,
            "task_kind": intent.task_kind,
            "context": intent.context,
            "output_contract": intent.output_contract,
            "runner": {
                "type": RUNNER_CLAUDE_CODE,
                "model": intent.runner_model or self.settings.llm_main_model,
                "base_url": intent.runner_base_url or self.settings.llm_main_base_url,
            },
        }
        cur = await conn.execute(
            """INSERT INTO agent_tasks
                   (task_id, runner_task_id, device_id, conversation_id, message_id,
                    title, intent_text, route, task_kind, state, progress_text,
                    final_text, error, artifacts_json, snapshot_json, envelope_json,
                   grant_id, permission_profile, workflow_run_id, last_seq, submitted_at,
                   finished_at, timeout_s, tenant_id, owner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '[]', ?, ?,
                           ?, ?, ?, 0, ?, NULL, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner_id, task_id) DO UPDATE SET
                       runner_task_id = excluded.runner_task_id,
                       device_id = excluded.device_id,
                       conversation_id = excluded.conversation_id,
                       message_id = excluded.message_id,
                       title = excluded.title,
                       intent_text = excluded.intent_text,
                       route = excluded.route,
                       task_kind = excluded.task_kind,
                       state = excluded.state,
                       progress_text = excluded.progress_text,
                       snapshot_json = excluded.snapshot_json,
                       envelope_json = excluded.envelope_json,
                       grant_id = excluded.grant_id,
                       permission_profile = excluded.permission_profile,
                       workflow_run_id = excluded.workflow_run_id,
                       submitted_at = excluded.submitted_at,
                       timeout_s = excluded.timeout_s
                   WHERE agent_tasks.tenant_id = excluded.tenant_id
                     AND agent_tasks.owner_id = excluded.owner_id""",
            (
                task_id,
                result.runner_task_id,
                principal_device_id if current_principal().mode == "public" else intent.device_id,
                intent.conversation_id,
                intent.message_id,
                title,
                intent.text,
                RUNNER_CLAUDE_CODE,
                intent.task_kind,
                state.value,
                snapshot.get("progress_text"),
                json.dumps(snapshot, ensure_ascii=False),
                json.dumps(envelope, ensure_ascii=False),
                intent.grant_id,
                intent.permission_profile,
                workflow_run_id,
                now,
                intent.timeout_s,
                tenant_id,
                owner_id,
            ),
        )
        if cur.rowcount != 1:
            await cur.close()
            raise PermissionError("agent task id is unavailable in this principal scope")
        await cur.close()
        cur = await conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ? AND tenant_id = ? AND owner_id = ?",
            (task_id, tenant_id, owner_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise RuntimeError("agent task insert did not produce an authoritative row")
        return _row_to_record(row)

    async def _persist_cancel_intent_tx(
        self,
        conn: aiosqlite.Connection,
        rec: AgentTaskRecord,
        event: EchoTaskEvent,
        *,
        tenant_id: str,
        device_id: str,
        owner_id: str,
    ) -> None:
        if event.event != "task.cancel_requested":
            return
        if rec.workflow_run_id:
            workflow = await self.workflow.request_cancel_in_transaction(
                conn,
                rec.workflow_run_id,
                reason="用户请求取消 Agent 任务",
            )
            if workflow is None:
                raise RuntimeError("Agent cancel target is missing its Workflow run")
        await self._command_outbox.enqueue_cancel_in_transaction(
            conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            device_id=device_id,
            task_id=rec.task_id,
            runner_task_id=event.runner_task_id or rec.runner_task_id,
        )

    async def record_task_event(  # noqa: PLR0912, PLR0915 - terminal UoW stays explicit
        self,
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
        raw_kind: str | None = None,
        lease_token: LeaseToken | None = None,
        submit_lease_token: LeaseToken | None = None,
        cancel_command: AgentCommandRecord | None = None,
        cancel_command_lease: LeaseToken | None = None,
    ) -> EchoTaskEvent | None:
        """Durably append one event before running its replayable projections."""

        if (cancel_command is None) != (cancel_command_lease is None):
            raise ValueError("cancel command and lease must be supplied together")
        tenant_id, device_id, owner_id = _scope()
        stored: EchoTaskEvent | None = None
        projection_seq: int | None = None
        duplicate = False
        workflow_arbitrated = False
        async with self._lock, self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if lease_token is not None:
                    await self._assert_task_lease(
                        lease_token,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        task_id=event.task_id,
                        conn=conn,
                    )
                if submit_lease_token is not None:
                    await self._assert_submit_lease(
                        submit_lease_token,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        task_id=event.task_id,
                        conn=conn,
                    )
                if cancel_command is not None and cancel_command_lease is not None:
                    await self._assert_cancel_command_lease(
                        cancel_command,
                        cancel_command_lease,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        task_id=event.task_id,
                        conn=conn,
                    )
                cur = await conn.execute(
                    "SELECT * FROM agent_tasks "
                    "WHERE task_id = ? AND tenant_id = ? AND owner_id = ?",
                    (event.task_id, tenant_id, owner_id),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    await conn.rollback()
                    return None
                rec = _row_to_record(row)
                if raw_hash:
                    cur = await conn.execute(
                        """SELECT seq, projected_at FROM agent_task_events
                           WHERE task_id = ? AND raw_event_hash = ?
                             AND tenant_id = ? AND owner_id = ?""",
                        (event.task_id, raw_hash, tenant_id, owner_id),
                    )
                    dup = await cur.fetchone()
                    await cur.close()
                    if dup is not None:
                        projection_seq = int(dup["seq"])
                        duplicate = True
                        if dup["projected_at"] is not None:
                            projection_seq = None
                if not duplicate:
                    seq = rec.last_seq + 1
                    stored = event.model_copy(update={"seq": seq})
                    incoming_state = _state(stored.state)
                    if rec.state.is_terminal and incoming_state != rec.state:
                        if stored.event != "task.artifact_updated":
                            # A remote completion and a user cancellation can cross in
                            # flight, and reconnects can deliver stale progress frames.
                            # The first durable terminal state is authoritative; retain
                            # late raw events for audit/dedupe without allowing them to
                            # rewrite either the task snapshot or Workflow.  Artifact
                            # scans are the sole post-terminal payload we still accept.
                            stored = stored.model_copy(
                                update={
                                    "runner_task_id": rec.runner_task_id,
                                    "conversation_id": rec.conversation_id,
                                    "message_id": rec.message_id,
                                    "title": rec.title,
                                    "event": "task.terminal_ignored",
                                    "state": rec.state.value,
                                    "visibility": "debug",
                                    "text_delta": None,
                                    "message": (
                                        "忽略迟到状态 "
                                        f"{incoming_state.value}；权威终态为 {rec.state.value}"
                                    ),
                                    "step": None,
                                    "artifacts": [],
                                    "actions": [],
                                    "permission": None,
                                    "snapshot": {},
                                }
                            )
                        else:
                            stored = stored.model_copy(update={"state": rec.state.value})
                    settled_state = _state(stored.state)
                    if (
                        settled_state.is_terminal
                        and stored.event != "task.artifact_updated"
                        and rec.workflow_run_id
                    ):
                        workflow = await self.workflow.settle_agent_terminal_in_transaction(
                            conn,
                            rec.workflow_run_id,
                            state=settled_state.value,  # type: ignore[arg-type]
                            message=stored.message,
                            output={
                                "agent_task_id": rec.task_id,
                                "runner_task_id": stored.runner_task_id or rec.runner_task_id,
                                "artifacts": stored.artifacts or rec.artifacts,
                            }
                            if settled_state == AgentTaskState.SUCCEEDED
                            else None,
                            payload={"agent_task_id": rec.task_id},
                        )
                        if workflow is None:
                            raise RuntimeError("Agent terminal target is missing its Workflow run")
                        workflow_arbitrated = True
                        if workflow.state != settled_state.value:
                            stored = stored.model_copy(
                                update={
                                    "runner_task_id": stored.runner_task_id or rec.runner_task_id,
                                    "conversation_id": rec.conversation_id,
                                    "message_id": rec.message_id,
                                    "title": rec.title,
                                    "event": "task.terminal_ignored",
                                    "state": workflow.state,
                                    "visibility": "debug",
                                    "text_delta": None,
                                    "message": (
                                        "忽略迟到状态 "
                                        f"{settled_state.value}；Workflow 权威终态为 "
                                        f"{workflow.state}"
                                    ),
                                    "step": None,
                                    "artifacts": [],
                                    "actions": [],
                                    "permission": None,
                                    "snapshot": {},
                                }
                            )
                    await self._persist_cancel_intent_tx(
                        conn,
                        rec,
                        stored,
                        tenant_id=tenant_id,
                        device_id=device_id,
                        owner_id=owner_id,
                    )
                    snapshot = reduce_snapshot(rec.snapshot, stored)
                    stored = stored.model_copy(update={"snapshot": snapshot})
                    state = _state(stored.state)
                    finished_at = (
                        rec.finished_at or utc_now_iso() if state.is_terminal else rec.finished_at
                    )
                    await conn.execute(
                        """INSERT INTO agent_task_events
                           (task_id, seq, event, state, visibility, payload_json,
                            raw_event_hash, raw_kind, projected_at, created_at,
                            tenant_id, device_id, owner_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                        (
                            stored.task_id,
                            seq,
                            stored.event,
                            stored.state,
                            stored.visibility,
                            stored.model_dump_json(),
                            raw_hash,
                            raw_kind,
                            stored.ts,
                            tenant_id,
                            device_id,
                            owner_id,
                        ),
                    )
                    await conn.execute(
                        """UPDATE agent_tasks
                           SET state = ?, progress_text = ?, final_text = ?, error = ?,
                               artifacts_json = ?, snapshot_json = ?, last_seq = ?,
                               runner_task_id = COALESCE(?, runner_task_id),
                               finished_at = ?
                           WHERE task_id = ? AND tenant_id = ? AND owner_id = ?""",
                        (
                            stored.state,
                            snapshot.get("progress_text"),
                            snapshot.get("final_text"),
                            snapshot.get("error"),
                            json.dumps(snapshot.get("artifacts") or [], ensure_ascii=False),
                            json.dumps(snapshot, ensure_ascii=False),
                            seq,
                            stored.runner_task_id,
                            finished_at,
                            stored.task_id,
                            tenant_id,
                            owner_id,
                        ),
                    )
                    if cancel_command is not None:
                        if not state.is_terminal:
                            raise RuntimeError(
                                "cancel command completion requires a terminal Agent event"
                            )
                        outcome: AgentCommandOutcome = (
                            "cancelled"
                            if state == AgentTaskState.CANCELLED
                            else "cancel_failed"
                            if state == AgentTaskState.CANCEL_FAILED
                            else "terminal_won"
                        )
                        completed = await self._command_outbox.mark_completed_in_transaction(
                            conn,
                            cancel_command,
                            outcome=outcome,
                        )
                        if not completed:
                            raise LeaseOwnershipError(
                                "cancel command was already completed by another fenced worker"
                            )
                    projection_seq = seq
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        if workflow_arbitrated:
            await self.workflow.flush_outbox()
        if projection_seq is not None:
            await self._project_event(event.task_id, projection_seq, lease_token=lease_token)
        return None if duplicate else stored

    async def _project_event(
        self,
        task_id: str,
        seq: int,
        *,
        lease_token: LeaseToken | None,
    ) -> None:
        tenant_id, _device_id, owner_id = _scope()
        if lease_token is not None:
            await self._assert_task_lease(
                lease_token,
                tenant_id=tenant_id,
                owner_id=owner_id,
                task_id=task_id,
            )
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT payload_json, projected_at FROM agent_task_events
                   WHERE task_id = ? AND seq = ? AND tenant_id = ? AND owner_id = ?""",
                (task_id, seq, tenant_id, owner_id),
            )
            event_row = await cur.fetchone()
            await cur.close()
            cur = await conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND tenant_id = ? AND owner_id = ?",
                (task_id, tenant_id, owner_id),
            )
            task_row = await cur.fetchone()
            await cur.close()
        if event_row is None or task_row is None or event_row["projected_at"] is not None:
            return
        stored = EchoTaskEvent.model_validate_json(event_row["payload_json"])
        rec = _row_to_record(task_row)
        # Keep the user-visible agent stream behind the durable workflow
        # projection.  Otherwise a renderer can observe a terminal Agent state
        # and immediately read the linked workflow while it is still in
        # cancel_requested/running (the packaged E2E exposed this race).
        await self._project_workflow_event(rec, stored)
        if stored.event != "task.terminal_ignored":
            await self.event_bus.publish(
                EchoEvent(type="agent.task.event", payload=stored.model_dump(mode="json"))
            )
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if lease_token is not None:
                    await self._assert_task_lease(
                        lease_token,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        task_id=task_id,
                        conn=conn,
                    )
                await conn.execute(
                    """UPDATE agent_task_events SET projected_at = ?
                       WHERE task_id = ? AND seq = ? AND tenant_id = ? AND owner_id = ?
                         AND projected_at IS NULL""",
                    (utc_now_iso(), task_id, seq, tenant_id, owner_id),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def _assert_task_lease(
        self,
        token: LeaseToken,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        conn: aiosqlite.Connection | None = None,
    ) -> None:
        if (
            token.tenant_id != tenant_id
            or token.owner_id != owner_id
            or token.resource_kind != "agent_task"
            or token.resource_id != task_id
        ):
            raise LeaseOwnershipError("agent task event lease scope does not match task")
        await self._lease_store.assert_owned(token, conn=conn)

    async def _assert_cancel_command_lease(
        self,
        command: AgentCommandRecord,
        token: LeaseToken,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        conn: aiosqlite.Connection,
    ) -> None:
        if (
            command.tenant_id != tenant_id
            or command.owner_id != owner_id
            or command.task_id != task_id
            or token.tenant_id != tenant_id
            or token.owner_id != owner_id
            or token.resource_kind != "agent_command"
            or token.resource_id != command.command_id
        ):
            raise LeaseOwnershipError("cancel command lease scope does not match task")
        await self._lease_store.assert_owned(token, conn=conn)

    async def _assert_submit_lease(
        self,
        token: LeaseToken,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        conn: aiosqlite.Connection,
    ) -> None:
        if (
            token.tenant_id != tenant_id
            or token.owner_id != owner_id
            or token.resource_kind != "agent_submit"
            or token.resource_id != task_id
        ):
            raise LeaseOwnershipError("submit lease scope does not match task")
        await self._lease_store.assert_owned(token, conn=conn)

    async def _project_workflow_event(
        self,
        rec: AgentTaskRecord,
        event: EchoTaskEvent,
    ) -> None:
        if not rec.workflow_run_id:
            return
        payload = event.model_dump(mode="json")
        visibility = (
            event.visibility if event.visibility in {"user", "debug", "hidden"} else "debug"
        )
        if event.event == "task.terminal_ignored":
            await self.workflow.record_event(
                rec.workflow_run_id,
                "agent.task.terminal_ignored",
                message=event.message,
                payload=payload,
                visibility="debug",
            )
            return
        state = event.state
        run = await self.workflow.get_run(rec.workflow_run_id)
        if (
            run is not None
            and run.state == "pending"
            and (state == AgentTaskState.RUNNING.value or _state(state).is_terminal)
        ):
            # Runner may collapse started+completed into one terminal event. Preserve
            # the strict workflow state machine by materializing pending -> running first.
            await self.workflow.start_run(rec.workflow_run_id)
        await self.workflow.record_event(
            rec.workflow_run_id,
            f"agent.{event.event}",
            message=event.message,
            payload=payload,
            visibility=visibility,
        )
        if event.event == "task.artifact_updated" and event.artifacts:
            await self._import_agent_artifacts(rec, event.artifacts)
            await self.workflow.merge_output(
                rec.workflow_run_id,
                {
                    "agent_task_id": rec.task_id,
                    "runner_task_id": event.runner_task_id or rec.runner_task_id,
                    "artifacts": event.artifacts,
                },
                event_type="agent.artifacts_projected",
                message="Agent 产物已写入统一投影",
            )
        if state == AgentTaskState.SUCCEEDED.value:
            await self.workflow.complete_run(
                rec.workflow_run_id,
                output={
                    "agent_task_id": rec.task_id,
                    "runner_task_id": event.runner_task_id or rec.runner_task_id,
                    "artifacts": event.artifacts or rec.artifacts,
                },
                message=event.message or "任务完成",
            )
        elif state == AgentTaskState.FAILED.value:
            await self.workflow.fail_run(
                rec.workflow_run_id,
                error=event.message or "任务失败",
                payload={"agent_task_id": rec.task_id},
            )
        elif state == AgentTaskState.TIMEOUT.value:
            await self.workflow.timeout_run(
                rec.workflow_run_id,
                error=event.message or "任务超时",
            )
        elif state in {
            AgentTaskState.CANCELLED.value,
            AgentTaskState.CANCEL_FAILED.value,
        }:
            await self._project_workflow_cancel_terminal(
                rec.workflow_run_id,
                cancel_state=state,
                message=event.message,
            )

    async def _project_workflow_cancel_terminal(
        self,
        run_id: str,
        *,
        cancel_state: str,
        message: str | None,
    ) -> None:
        current = await self.workflow.get_run(run_id)
        if current is not None and current.state == cancel_state:
            return
        if current is not None and current.state != "cancel_requested":
            reason = (
                "Agent Runner 已取消任务"
                if cancel_state == AgentTaskState.CANCELLED.value
                else "Agent Runner 取消失败"
            )
            await self.workflow.request_cancel(run_id, reason=reason)
        if cancel_state == AgentTaskState.CANCELLED.value:
            await self.workflow.mark_cancelled(run_id, message=message or "任务已取消")
        else:
            await self.workflow.mark_cancel_failed(run_id, error=message or "取消失败")

    async def _import_agent_artifacts(
        self,
        rec: AgentTaskRecord,
        artifacts: list[dict[str, Any]],
    ) -> None:
        if not rec.workflow_run_id or not rec.runner_task_id:
            return
        run = await self.workflow.get_run(rec.workflow_run_id)
        if run is None:
            return
        for item in artifacts:
            relpath = str(item.get("relpath") or item.get("name") or "").strip()
            encoded = _encode_agentos_artifact_path(relpath)
            cache_rel = _cache_relpath(rec.task_id, relpath)
            if not encoded or cache_rel is None:
                await self._record_artifact_import_failure(
                    rec.workflow_run_id,
                    relpath=relpath,
                    reason="invalid_path",
                    message="Agent 产物路径不合法",
                )
                continue
            artifact_id = _agent_artifact_id(rec.task_id, relpath)
            cache_path = (self.settings.storage_dir / cache_rel).expanduser().resolve()
            upstream_url = (
                f"{self.backend.base_url}/api/v1/tasks/"
                f"{quote(rec.runner_task_id, safe='')}/artifacts/{encoded}"
            )
            download = await self._download_agent_artifact(
                rec,
                relpath=relpath,
                upstream_url=upstream_url,
                cache_path=cache_path,
            )
            if download is None:
                continue

            mime = download.content_type or mimetypes.guess_type(cache_path.name)[0]
            try:
                artifact = GeneratedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=str(item.get("kind") or "agent"),
                    title=str(item.get("name") or relpath or artifact_id),
                    file_path=str(cache_path),
                    mime_type=mime or "application/octet-stream",
                    size_bytes=download.size_bytes,
                    generation_latency_ms=0,
                    model=RUNNER_CLAUDE_CODE,
                    metadata={
                        "source": "agent",
                        "agent_task_id": rec.task_id,
                        "runner_task_id": rec.runner_task_id,
                        "relpath": relpath,
                        "legacy_url": str(item.get("url") or ""),
                    },
                )
                async with self._conn() as conn:
                    await conn.execute("BEGIN IMMEDIATE")
                    try:
                        await self.artifact_repo.save_artifact_tx(
                            conn,
                            artifact,
                            run_id=rec.workflow_run_id,
                        )
                        link = await self.artifact_repo.link_artifact_tx(
                            conn,
                            artifact_id=artifact.artifact_id,
                            source="agent",
                            meeting_id=run.meeting_id,
                            todo_id=run.todo_id,
                            run_id=rec.workflow_run_id,
                        )
                        await conn.commit()
                    except BaseException:
                        await conn.rollback()
                        raise
            except Exception as exc:
                with suppress(OSError):
                    cache_path.unlink(missing_ok=True)
                _log.warning(
                    "agent artifact registration failed task=%s error_type=%s",
                    rec.task_id,
                    type(exc).__name__,
                )
                await self._record_artifact_import_failure(
                    rec.workflow_run_id,
                    relpath=relpath,
                    reason="registration_failed",
                )
                continue

            saved = artifact
            await self.workflow.record_event(
                rec.workflow_run_id,
                "agent.artifact_imported",
                message="Agent 产物已归档",
                payload={
                    "artifact_id": saved.artifact_id,
                    "relpath": relpath,
                    "link_id": link.link_id,
                },
                visibility="debug",
            )
            await self.event_bus.publish(
                EchoEvent(
                    type="artifact.ready",
                    meeting_id=run.meeting_id,
                    payload={
                        **saved.model_dump(mode="json"),
                        "run_id": rec.workflow_run_id,
                        "agent_task_id": rec.task_id,
                        "links": [
                            {
                                "link_id": link.link_id,
                                "source": link.source,
                                "meeting_id": link.meeting_id,
                                "todo_id": link.todo_id,
                                "run_id": link.run_id,
                            }
                        ],
                    },
                )
            )

    async def _download_agent_artifact(
        self,
        rec: AgentTaskRecord,
        *,
        relpath: str,
        upstream_url: str,
        cache_path: Path,
    ) -> ArtifactDownloadResult | None:
        assert rec.workflow_run_id is not None
        reason = "transfer_failed"
        try:
            return await download_artifact_to_path(
                upstream_url,
                cache_path,
                max_bytes=self.settings.agent_artifact_proxy_max_bytes,
                chunk_bytes=self.settings.upload_read_chunk_bytes,
            )
        except ArtifactSizeLimitError:
            reason = "size_limit_exceeded"
        except ArtifactContentLengthError:
            reason = "invalid_content_length"
        except httpx.HTTPError:
            reason = "upstream_unavailable"
        except OSError:
            reason = "storage_error"
        except Exception as exc:
            _log.warning(
                "agent artifact transfer failed task=%s error_type=%s",
                rec.task_id,
                type(exc).__name__,
            )
        await self._record_artifact_import_failure(
            rec.workflow_run_id,
            relpath=relpath,
            reason=reason,
        )
        return None

    async def _record_artifact_import_failure(
        self,
        workflow_run_id: str,
        *,
        relpath: str,
        reason: str,
        message: str = "Agent 产物导入失败",
    ) -> None:
        await self.workflow.record_event(
            workflow_run_id,
            "agent.artifact_import_failed",
            message=message,
            payload={"relpath": relpath, "reason": reason},
            visibility="debug",
        )

    async def _read_task(self, task_id: str) -> AgentTaskRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ? AND tenant_id = ? AND owner_id = ?",
                (task_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_record(row) if row else None

    async def get_task(self, task_id: str) -> AgentTaskRecord | None:
        """Return a terminal Agent record only after its Workflow agrees."""

        rec = await self._read_task(task_id)
        if rec is not None and rec.state.is_terminal:
            return await self._reconcile_terminal_task(rec)
        return rec

    async def list_tasks(
        self, *, device_id: str | None = None, limit: int = 50
    ) -> list[AgentTaskRecord]:
        tenant_id, _principal_device_id, owner_id = _scope()
        sql = "SELECT * FROM agent_tasks WHERE tenant_id = ? AND owner_id = ?"
        args: list[Any] = [tenant_id, owner_id]
        if device_id:
            sql += " AND device_id = ?"
            args.append(_effective_device_id(device_id))
        sql += " ORDER BY submitted_at DESC LIMIT ?"
        args.append(limit)
        async with self._conn() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        records = [_row_to_record(row) for row in rows]
        return [
            await self._reconcile_terminal_task(rec) if rec.state.is_terminal else rec
            for rec in records
        ]

    async def list_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
    ) -> tuple[list[EchoTaskEvent], dict[str, Any], int]:
        rec = await self.get_task(task_id)
        if rec is None:
            return [], {}, 0
        tenant_id, _device_id, owner_id = _scope()
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT payload_json FROM agent_task_events
                   WHERE task_id = ? AND seq > ? AND tenant_id = ? AND owner_id = ?
                   ORDER BY seq ASC""",
                (task_id, after_seq, tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        events = [EchoTaskEvent.model_validate_json(r["payload_json"]) for r in rows]
        return events, rec.snapshot, rec.last_seq

    async def cancel_task(self, task_id: str) -> AgentTaskRecord | None:
        rec = await self.get_task(task_id)
        if rec is None:
            return None
        if rec.state.is_terminal:
            return rec
        await self.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                title=rec.title,
                event="task.cancel_requested",
                state="cancel_requested",
                message="正在取消任务",
            )
        )
        latest = await self.get_task(task_id)
        if latest is None:
            return None
        if latest.state.is_terminal:
            return latest
        await self.recover_cancel_commands_once(task_id=task_id, limit=1)
        return await self.get_task(task_id)

    async def recover_cancel_commands_once(
        self,
        *,
        task_id: str | None = None,
        limit: int = 100,
    ) -> int:
        principal = current_principal()
        commands = await self._command_outbox.list_due(
            tenant_id=principal.tenant_id,
            owner_id=principal.owner_id,
            task_id=task_id,
            limit=limit,
        )
        completed = 0
        for command in commands:
            if await self._execute_cancel_command(command):
                completed += 1
        return completed

    async def _execute_cancel_command(self, command: AgentCommandRecord) -> bool:
        lease = await self._lease_store.acquire(
            tenant_id=command.tenant_id,
            owner_id=command.owner_id,
            resource_kind="agent_command",
            resource_id=command.command_id,
            holder_id=self._holder_id,
            ttl_seconds=self._cancel_command_lease_ttl_seconds,
        )
        if lease is None:
            return False
        try:
            rec = await self.get_task(command.task_id)
            if rec is None:
                raise RuntimeError("durable cancel command lost its Agent task")
            if rec.state.is_terminal and not command.force_remote:
                return await self._complete_terminal_cancel_command(command, lease, rec)
            if rec.state != AgentTaskState.CANCEL_REQUESTED and not command.force_remote:
                raise RuntimeError("durable cancel command has no cancel_requested task")
            runner_task_id = command.runner_task_id or rec.runner_task_id
            try:
                cancelled = runner_task_id is None or await self.backend.cancel(
                    runner_task_id,
                    operation_key=command.operation_key,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return await self._defer_cancel_command(
                    command,
                    lease,
                    error=f"remote_cancel_{type(exc).__name__}",
                )
            if not cancelled:
                return await self._defer_cancel_command(
                    command,
                    lease,
                    error="remote_cancel_rejected",
                )
            latest = await self.get_task(command.task_id)
            if latest is None:
                return False
            if not latest.state.is_terminal:
                stored = await self.record_task_event(
                    EchoTaskEvent(
                        task_id=latest.task_id,
                        runner_task_id=runner_task_id,
                        title=latest.title,
                        event="task.cancelled",
                        state="cancelled",
                        message="任务已取消",
                    ),
                    cancel_command=command,
                    cancel_command_lease=lease,
                )
                latest = await self.get_task(command.task_id) or latest
                changed = stored is not None
            else:
                changed = await self._complete_terminal_cancel_command(command, lease, latest)
            if latest.state == AgentTaskState.CANCELLED:
                self._stop_bridge(latest)
            return changed
        finally:
            await self._lease_store.release(lease)

    async def _defer_cancel_command(
        self,
        command: AgentCommandRecord,
        lease: LeaseToken,
        *,
        error: str,
    ) -> bool:
        delay = min(
            self._cancel_command_retry_max_seconds,
            self._cancel_command_retry_base_seconds * (2 ** min(command.attempts, 16)),
        )
        updated = await self._command_outbox.mark_retry(
            command,
            lease,
            next_attempt_at=time.time() + delay,
            error=error,
        )
        if updated.attempts < self._cancel_command_max_attempts:
            return False
        latest = await self.get_task(command.task_id)
        if latest is None:
            return False
        if updated.force_remote and latest.state.is_terminal:
            return await self._command_outbox.mark_completed(
                updated,
                lease,
                outcome="cancel_failed",
            )
        if not latest.state.is_terminal:
            stored = await self.record_task_event(
                EchoTaskEvent(
                    task_id=latest.task_id,
                    runner_task_id=updated.runner_task_id or latest.runner_task_id,
                    title=latest.title,
                    event="task.cancel_failed",
                    state="cancel_failed",
                    message="取消失败，请检查 Agent Runner 状态",
                ),
                cancel_command=updated,
                cancel_command_lease=lease,
            )
            return stored is not None
        return await self._complete_terminal_cancel_command(updated, lease, latest)

    async def _complete_terminal_cancel_command(
        self,
        command: AgentCommandRecord,
        lease: LeaseToken,
        rec: AgentTaskRecord,
    ) -> bool:
        if rec.state == AgentTaskState.CANCELLED:
            return await self._command_outbox.mark_completed(
                command,
                lease,
                outcome="cancelled",
            )
        if rec.state == AgentTaskState.CANCEL_FAILED:
            return await self._command_outbox.mark_completed(
                command,
                lease,
                outcome="cancel_failed",
            )
        if rec.state.is_terminal:
            return await self._command_outbox.mark_completed(
                command,
                lease,
                outcome="terminal_won",
            )
        raise RuntimeError("cancel command completion requires a terminal Agent task")

    def _stop_bridge(self, rec: AgentTaskRecord) -> None:
        task = self._bridge_tasks.pop(self._bridge_key(rec), None)
        if task:
            task.cancel()

    async def retry_task(self, task_id: str) -> AgentTaskRecord | None:
        """Retry from the authoritative agent_tasks record and create a linked workflow."""

        rec = await self.get_task(task_id)
        if rec is None:
            return None
        raw_context = rec.envelope.get("context")
        context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
        raw_output_contract = rec.envelope.get("output_contract")
        output_contract: dict[str, Any] = (
            raw_output_contract if isinstance(raw_output_contract, dict) else {}
        )
        intent = AgentIntent(
            text=rec.intent_text,
            device_id=rec.device_id,
            conversation_id=rec.conversation_id,
            message_id=rec.message_id,
            title=rec.title,
            task_kind=rec.task_kind,
            context={**context, "retry_of_agent_task_id": rec.task_id},
            output_contract=output_contract,
            timeout_s=rec.timeout_s,
        )
        return await self.submit_task(intent)

    def start_recovery_loop(self) -> None:
        """Start the bounded bridge reaper when called from an async lifecycle."""

        if self._closed:
            return
        existing = self._recovery_task
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._bridge_recovery_loop(),
            name=f"agent-bridge-recovery:{self._holder_id}",
        )
        self._recovery_task = task

        def _cleanup(done: asyncio.Task[None]) -> None:
            if self._recovery_task is done:
                self._recovery_task = None
            if not done.cancelled():
                exc = done.exception()
                if exc is not None:
                    _log.warning(
                        "agent bridge recovery stopped error_type=%s",
                        type(exc).__name__,
                    )

        task.add_done_callback(_cleanup)

    async def _bridge_recovery_loop(self) -> None:
        while True:
            try:
                await self._recover_agent_bridges_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(
                    "agent bridge recovery scan failed error_type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(self._bridge_recovery_interval_seconds)

    async def _recover_agent_bridges_once(self) -> None:
        principals = await self.list_unfinished_principals()
        for principal in principals:
            if self._closed:
                return
            token = bind_principal(principal)
            try:
                await self.recover_cancel_commands_once()
                recoverable = await self._list_recoverable_tasks()
                for rec in recoverable:
                    if (
                        rec.runner_task_id
                        and rec.bridge_completed_at is None
                        and rec.state != AgentTaskState.WAITING_PERMISSION
                    ):
                        self._start_bridge_if_retry_due(rec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(
                    "agent bridge principal scan failed tenant=%s owner=%s error_type=%s",
                    principal.tenant_id,
                    principal.owner_id,
                    type(exc).__name__,
                )
            finally:
                reset_principal(token)

    def _start_bridge_if_retry_due(self, rec: AgentTaskRecord) -> None:
        key = self._bridge_key(rec)
        existing = self._bridge_tasks.get(key)
        if existing is not None and not existing.done():
            return
        if time.monotonic() < self._bridge_retry_at.get(key, 0.0):
            return
        self.start_bridge_for_task(rec)

    def _schedule_bridge_retry(self, key: tuple[str, str, str]) -> None:
        if self._closed:
            return
        attempt = min(self._bridge_retry_attempts.get(key, 0) + 1, 30)
        delay = min(
            self._bridge_retry_max_seconds,
            self._bridge_retry_base_seconds * (2 ** (attempt - 1)),
        )
        self._bridge_retry_attempts[key] = attempt
        self._bridge_retry_at[key] = time.monotonic() + delay

    def _clear_bridge_retry(self, key: tuple[str, str, str]) -> None:
        self._bridge_retry_attempts.pop(key, None)
        self._bridge_retry_at.pop(key, None)

    def start_bridge_for_task(self, rec: AgentTaskRecord) -> None:
        if (
            self._closed
            or rec.route != RUNNER_CLAUDE_CODE
            or not rec.runner_task_id
            or rec.bridge_completed_at is not None
            or not self.backend.enabled
        ):
            return
        self.start_recovery_loop()
        key = self._bridge_key(rec)
        existing = self._bridge_tasks.get(key)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_bridge(rec), name=f"agent-bridge:{rec.task_id}")
        self._bridge_tasks[key] = task

        def _cleanup(done: asyncio.Task[None]) -> None:
            if self._bridge_tasks.get(key) is done:
                self._bridge_tasks.pop(key, None)
            if not done.cancelled():
                exc = done.exception()
                if exc:
                    _log.warning(
                        "agent bridge crashed task=%s error_type=%s",
                        rec.task_id,
                        type(exc).__name__,
                    )

        task.add_done_callback(_cleanup)

    async def _run_bridge(self, rec: AgentTaskRecord) -> None:
        assert rec.runner_task_id is not None
        key = self._bridge_key(rec)
        lease: LeaseToken | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        bridge_task: asyncio.Task[bool] | None = None
        retry = True
        try:
            lease = await self._lease_store.acquire(
                tenant_id=rec.tenant_id,
                owner_id=rec.owner_id,
                resource_kind="agent_task",
                resource_id=rec.task_id,
                holder_id=self._holder_id,
                ttl_seconds=self._bridge_lease_ttl_seconds,
            )
            if lease is None:
                return
            heartbeat_task = asyncio.create_task(
                self._heartbeat_bridge_lease(lease),
                name=f"agent-lease-heartbeat:{rec.task_id}",
            )
            await self._replay_pending_projections(rec, lease)
            if await self._has_completed_runner_tail(rec):
                await self._mark_bridge_completed(rec, lease)
                retry = False
                self._clear_bridge_retry(key)
                return
            result_seen = await self._has_runner_result(rec)

            async def _record(
                event: EchoTaskEvent,
                *,
                raw_hash: str | None = None,
                raw_kind: str | None = None,
            ) -> EchoTaskEvent | None:
                return await self.record_task_event(
                    event,
                    raw_hash=raw_hash,
                    raw_kind=raw_kind,
                    lease_token=lease,
                )

            bridge = EchoTaskStreamBridge(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                agentos_base_url=self.backend.base_url,
                recorder=_record,
                conversation_id=rec.conversation_id,
                message_id=rec.message_id,
                title=rec.title,
                result_terminal_seen=result_seen,
            )
            bridge_task = asyncio.create_task(
                bridge.run(),
                name=f"agent-stream:{rec.task_id}",
            )
            done, _pending = await asyncio.wait(
                {bridge_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)
                return
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            if await bridge_task:
                await self._mark_bridge_completed(rec, lease)
                retry = False
                self._clear_bridge_retry(key)
        except asyncio.CancelledError:
            retry = False
            raise
        except Exception as exc:
            _log.warning(
                "agent bridge attempt failed task=%s error_type=%s",
                rec.task_id,
                type(exc).__name__,
            )
        finally:
            if bridge_task is not None and not bridge_task.done():
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if lease is not None:
                try:
                    await self._lease_store.release(lease)
                except Exception as exc:
                    _log.warning(
                        "agent bridge lease release failed task=%s error_type=%s",
                        rec.task_id,
                        type(exc).__name__,
                    )
            if retry:
                self._schedule_bridge_retry(key)

    async def _heartbeat_bridge_lease(self, lease: LeaseToken) -> None:
        while True:
            await asyncio.sleep(self._bridge_heartbeat_seconds)
            try:
                renewed = await self._lease_store.renew(
                    lease,
                    ttl_seconds=self._bridge_lease_ttl_seconds,
                )
            except Exception as exc:
                _log.warning(
                    "agent bridge lease heartbeat failed task=%s error_type=%s",
                    lease.resource_id,
                    type(exc).__name__,
                )
                return
            if renewed is None:
                _log.warning("agent bridge lease lost task=%s", lease.resource_id)
                return

    async def _has_runner_result(self, rec: AgentTaskRecord) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT 1 FROM agent_task_events
                   WHERE tenant_id = ? AND owner_id = ? AND task_id = ?
                     AND raw_kind = 'result'
                   LIMIT 1""",
                (rec.tenant_id, rec.owner_id, rec.task_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return row is not None

    async def _has_completed_runner_tail(self, rec: AgentTaskRecord) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT 1 FROM agent_task_events AS tail
                   WHERE tail.tenant_id = ? AND tail.owner_id = ? AND tail.task_id = ?
                     AND tail.raw_kind = 'task_state'
                     AND tail.state IN ('succeeded', 'failed', 'cancelled',
                                        'cancel_failed', 'timeout')
                     AND (
                         tail.state IN ('cancelled', 'cancel_failed')
                         OR EXISTS (
                             SELECT 1 FROM agent_task_events AS result
                             WHERE result.tenant_id = tail.tenant_id
                               AND result.owner_id = tail.owner_id
                               AND result.task_id = tail.task_id
                               AND result.raw_kind = 'result'
                               AND result.seq < tail.seq
                         )
                     )
                   LIMIT 1""",
                (rec.tenant_id, rec.owner_id, rec.task_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return row is not None

    async def _replay_pending_projections(
        self,
        rec: AgentTaskRecord,
        lease: LeaseToken,
    ) -> None:
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT seq FROM agent_task_events
                   WHERE tenant_id = ? AND owner_id = ? AND task_id = ?
                     AND projected_at IS NULL
                   ORDER BY seq ASC""",
                (rec.tenant_id, rec.owner_id, rec.task_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        for row in rows:
            await self._project_event(rec.task_id, int(row["seq"]), lease_token=lease)

    async def _mark_bridge_completed(self, rec: AgentTaskRecord, lease: LeaseToken) -> None:
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_task_lease(
                    lease,
                    tenant_id=rec.tenant_id,
                    owner_id=rec.owner_id,
                    task_id=rec.task_id,
                    conn=conn,
                )
                await conn.execute(
                    """UPDATE agent_tasks SET bridge_completed_at = ?
                       WHERE tenant_id = ? AND owner_id = ? AND task_id = ?
                         AND bridge_completed_at IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM agent_task_events AS event
                             WHERE event.tenant_id = agent_tasks.tenant_id
                               AND event.owner_id = agent_tasks.owner_id
                               AND event.task_id = agent_tasks.task_id
                               AND event.projected_at IS NULL
                         )""",
                    (utc_now_iso(), rec.tenant_id, rec.owner_id, rec.task_id),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    @staticmethod
    def _bridge_key(rec: AgentTaskRecord) -> tuple[str, str, str]:
        return rec.tenant_id, rec.owner_id, rec.task_id

    async def restore_unfinished(self) -> int:
        tasks = await self._list_recoverable_tasks()
        refreshed: list[AgentTaskRecord] = []
        for rec in tasks:
            await self._replay_pending_for_task(rec)
            current = await self.get_task(rec.task_id) or rec
            await self._reconcile_workflow_projection(current)
            await self.recover_cancel_commands_once(task_id=rec.task_id, limit=1)
            current = await self.get_task(rec.task_id) or current
            refreshed.append(current)
        if not self.backend.enabled:
            return 0
        count = 0
        for rec in refreshed:
            current = rec
            if rec.state == AgentTaskState.PENDING and not rec.runner_task_id:
                grant = await self.get_active_grant(device_id=rec.device_id)
                if grant is not None:
                    current = await self.resume_with_grant(rec.task_id, grant)
            if (
                current.runner_task_id
                and current.bridge_completed_at is None
                and current.state != AgentTaskState.WAITING_PERMISSION
            ):
                self.start_bridge_for_task(current)
                count += 1
        return count

    async def list_unfinished_principals(self) -> list[Principal]:
        """Return every persisted principal/device scope needing Agent recovery."""

        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT task.tenant_id, task.device_id, task.owner_id
                   FROM agent_tasks AS task
                   WHERE task.state IN ('pending', 'running', 'cancel_requested')
                      OR (task.runner_task_id IS NOT NULL
                          AND task.bridge_completed_at IS NULL)
                      OR EXISTS (
                          SELECT 1 FROM agent_task_events AS event
                          WHERE event.tenant_id = task.tenant_id
                            AND event.owner_id = task.owner_id
                            AND event.task_id = task.task_id
                            AND event.projected_at IS NULL
                      )
                      OR EXISTS (
                          SELECT 1 FROM workflow_runs AS run
                          WHERE run.tenant_id = task.tenant_id
                            AND run.owner_id = task.owner_id
                            AND run.run_id = task.workflow_run_id
                            AND run.state IN ('pending', 'running', 'cancel_requested')
                      )
                      OR EXISTS (
                          SELECT 1 FROM agent_command_outbox AS command
                          WHERE command.tenant_id = task.tenant_id
                            AND command.owner_id = task.owner_id
                            AND command.task_id = task.task_id
                            AND command.completed_at IS NULL
                      )
                   GROUP BY task.tenant_id, task.device_id, task.owner_id
                   ORDER BY task.tenant_id, task.owner_id, task.device_id"""
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            Principal(
                tenant_id=str(row["tenant_id"]),
                device_id=str(row["device_id"]),
                owner_id=str(row["owner_id"]),
                session_id=f"agent-restore:{row['owner_id']}:{row['device_id']}",
                mode="local" if row["owner_id"] == LEGACY_OWNER_ID else "public",
            )
            for row in rows
        ]

    async def _list_recoverable_tasks(self) -> list[AgentTaskRecord]:
        principal = current_principal()
        clauses = ["task.tenant_id = ?", "task.owner_id = ?"]
        args: list[Any] = [principal.tenant_id, principal.owner_id]
        if principal.mode == "public":
            clauses.append("task.device_id = ?")
            args.append(principal.device_id)
        clauses.append(
            """(
                task.state IN ('pending', 'running', 'cancel_requested')
                OR (task.runner_task_id IS NOT NULL AND task.bridge_completed_at IS NULL)
                OR EXISTS (
                    SELECT 1 FROM agent_task_events AS event
                    WHERE event.tenant_id = task.tenant_id
                      AND event.owner_id = task.owner_id
                      AND event.task_id = task.task_id
                      AND event.projected_at IS NULL
                )
                OR EXISTS (
                    SELECT 1 FROM workflow_runs AS run
                    WHERE run.tenant_id = task.tenant_id
                      AND run.owner_id = task.owner_id
                      AND run.run_id = task.workflow_run_id
                      AND run.state IN ('pending', 'running', 'cancel_requested')
                )
                OR EXISTS (
                    SELECT 1 FROM agent_command_outbox AS command
                    WHERE command.tenant_id = task.tenant_id
                      AND command.owner_id = task.owner_id
                      AND command.task_id = task.task_id
                      AND command.completed_at IS NULL
                )
            )"""
        )
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT task.* FROM agent_tasks AS task WHERE "
                + " AND ".join(clauses)
                + " ORDER BY task.submitted_at ASC, task.task_id ASC",
                args,
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_record(row) for row in rows]

    async def _replay_pending_for_task(self, rec: AgentTaskRecord) -> None:
        lease = await self._lease_store.acquire(
            tenant_id=rec.tenant_id,
            owner_id=rec.owner_id,
            resource_kind="agent_task",
            resource_id=rec.task_id,
            holder_id=self._holder_id,
            ttl_seconds=self._bridge_lease_ttl_seconds,
        )
        if lease is None:
            return
        try:
            await self._replay_pending_projections(rec, lease)
        finally:
            await self._lease_store.release(lease)

    async def _reconcile_workflow_projection(self, rec: AgentTaskRecord) -> None:
        """Repair workflow projection from authoritative agent_tasks after a crash."""

        if not rec.workflow_run_id:
            return
        run = await self.workflow.get_run(rec.workflow_run_id)
        if run is None or run.is_terminal:
            return
        if run.state == "pending" and (
            rec.state == AgentTaskState.RUNNING or rec.state.is_terminal
        ):
            run = await self.workflow.start_run(rec.workflow_run_id) or run
        if rec.state == AgentTaskState.CANCEL_REQUESTED and run.state != "cancel_requested":
            await self.workflow.request_cancel(
                rec.workflow_run_id,
                reason="恢复 Agent 取消请求",
            )
        elif rec.state == AgentTaskState.SUCCEEDED:
            await self.workflow.complete_run(
                rec.workflow_run_id,
                output={
                    "agent_task_id": rec.task_id,
                    "runner_task_id": rec.runner_task_id,
                    "artifacts": rec.artifacts,
                },
                message="从 Agent 权威状态恢复完成投影",
            )
        elif rec.state == AgentTaskState.FAILED:
            await self.workflow.fail_run(
                rec.workflow_run_id,
                error=rec.error or "Agent task failed before workflow projection",
            )
        elif rec.state == AgentTaskState.TIMEOUT:
            await self.workflow.timeout_run(
                rec.workflow_run_id,
                error=rec.error or "Agent task timed out before workflow projection",
            )
        elif rec.state == AgentTaskState.CANCELLED:
            if run.state != "cancel_requested":
                await self.workflow.request_cancel(
                    rec.workflow_run_id, reason="恢复 Agent 取消状态"
                )
            await self.workflow.mark_cancelled(rec.workflow_run_id)
        elif rec.state == AgentTaskState.CANCEL_FAILED:
            if run.state != "cancel_requested":
                await self.workflow.request_cancel(
                    rec.workflow_run_id, reason="恢复 Agent 取消失败状态"
                )
            await self.workflow.mark_cancel_failed(
                rec.workflow_run_id,
                error=rec.error or "Agent cancel failed before workflow projection",
            )

    async def _reconcile_terminal_task(self, rec: AgentTaskRecord) -> AgentTaskRecord:
        """Return a terminal task only after its user-visible Workflow agrees."""

        await self._reconcile_workflow_projection(rec)
        latest = await self._read_task(rec.task_id) or rec
        if latest.workflow_run_id:
            run = await self.workflow.get_run(latest.workflow_run_id)
            if run is None:
                raise RuntimeError("terminal Agent task is missing its Workflow projection")
            if run.state != latest.state.value:
                raise RuntimeError(
                    "terminal Agent task and Workflow projection disagree: "
                    f"{latest.state.value} != {run.state}"
                )
        return latest

    async def aclose(self) -> None:
        self._closed = True
        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        tasks = list(self._bridge_tasks.values())
        self._bridge_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bridge_retry_attempts.clear()
        self._bridge_retry_at.clear()


_service: AgentTaskService | None = None


def get_agent_task_service(settings: Settings, event_bus: InMemoryEventBus) -> AgentTaskService:
    global _service  # noqa: PLW0603
    if _service is None:
        _service = AgentTaskService(
            settings,
            event_bus,
            workflow=get_workflow_service(settings, event_bus),
        )
    _service.start_recovery_loop()
    return _service


async def aclose_agent_task_service() -> None:
    global _service  # noqa: PLW0603
    if _service is not None:
        await _service.aclose()
        _service = None


def reset_agent_task_service_for_test() -> None:
    global _service  # noqa: PLW0603
    _service = None
