"""Agent task service：持久化、授权、AgentOS bridge 与 EchoEvent 广播。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.agents.agentos import AgentOSBackend
from app.agents.base import AgentIntent, AgentSubmitResult, AgentTaskState, new_echo_task_id
from app.agents.events import (
    EchoTaskEvent,
    default_snapshot,
    reduce_snapshot,
    utc_now_iso,
)
from app.agents.stream_bridge import EchoTaskStreamBridge
from app.config import Settings
from app.schemas.events import EchoEvent

_log = logging.getLogger("echodesk.agents")

RUNNER_CLAUDE_CODE = "claude_code"
PROFILE_FULL_ACCESS = "claude_code_full_access"
PERMISSION_MODE_BYPASS = "bypassPermissions"


@dataclass(slots=True)
class AgentTaskRecord:
    task_id: str
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
    last_seq: int = 0
    submitted_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
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


def _row_to_record(row: aiosqlite.Row) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=row["task_id"],
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
        last_seq=int(row["last_seq"] or 0),
        submitted_at=row["submitted_at"],
        finished_at=row["finished_at"],
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
    def __init__(self, settings: Settings, event_bus: InMemoryEventBus) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.backend = AgentOSBackend(settings)
        self._bridge_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(str(self.settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            yield conn

    async def get_active_grant(
        self,
        *,
        device_id: str,
        runner: str = RUNNER_CLAUDE_CODE,
    ) -> AgentRunnerGrant | None:
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM agent_runner_grants
                   WHERE device_id = ? AND runner = ? AND revoked_at IS NULL
                   ORDER BY granted_at DESC LIMIT 1""",
                (device_id, runner),
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
        existing = await self.get_active_grant(device_id=device_id)
        if existing and existing.permission_profile == permission_profile:
            return existing
        grant_id = f"grant_{hashlib.sha1(f'{device_id}:{utc_now_iso()}'.encode()).hexdigest()[:24]}"
        now = utc_now_iso()
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO agent_runner_grants
                   (grant_id, device_id, runner, permission_profile, permission_mode,
                    workspace_ids_json, granted_at, revoked_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (
                    grant_id,
                    device_id,
                    RUNNER_CLAUDE_CODE,
                    permission_profile,
                    PERMISSION_MODE_BYPASS,
                    json.dumps(workspace_ids or [], ensure_ascii=False),
                    now,
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
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE agent_runner_grants SET last_used_at = ? WHERE grant_id = ?",
                (utc_now_iso(), grant_id),
            )
            await conn.commit()

    async def revoke_grant(self, grant_id: str) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute(
                """UPDATE agent_runner_grants
                   SET revoked_at = ?
                   WHERE grant_id = ? AND revoked_at IS NULL""",
                (utc_now_iso(), grant_id),
            )
            await conn.commit()
            return bool(cur.rowcount)

    async def submit_task(self, intent: AgentIntent) -> AgentTaskRecord:
        intent.echo_task_id = intent.echo_task_id or new_echo_task_id()
        intent.title = intent.title or _title_from_text(intent.text)
        grant = await self.get_active_grant(device_id=intent.device_id)
        if grant is None:
            return await self.record_permission_required(intent)
        intent.grant_id = grant.grant_id
        intent.permission_profile = grant.permission_profile
        result = await self.backend.submit(intent)
        if not result.accepted:
            rec = await self._insert_task(
                intent=intent,
                result=AgentSubmitResult(
                    task_id=intent.echo_task_id,
                    accepted=True,
                    provider=RUNNER_CLAUDE_CODE,
                ),
                state=AgentTaskState.FAILED,
            )
            await self.record_task_event(
                EchoTaskEvent(
                    task_id=rec.task_id,
                    conversation_id=rec.conversation_id,
                    message_id=rec.message_id,
                    title=rec.title,
                    event="task.failed",
                    state="failed",
                    message=result.error or "任务暂时无法启动",
                )
            )
            return await self.get_task(rec.task_id) or rec

        await self.touch_grant(grant.grant_id)
        rec = await self._insert_task(
            intent=intent,
            result=result,
            state=AgentTaskState.PENDING,
        )
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

    async def record_permission_required(self, intent: AgentIntent) -> AgentTaskRecord:
        rec = await self._insert_task(
            intent=intent,
            result=AgentSubmitResult(
                task_id=intent.echo_task_id or new_echo_task_id(),
                accepted=True,
                provider=RUNNER_CLAUDE_CODE,
            ),
            state=AgentTaskState.WAITING_PERMISSION,
        )
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

    async def resume_with_grant(self, task_id: str, grant: AgentRunnerGrant) -> AgentTaskRecord:
        rec = await self.get_task(task_id)
        if rec is None:
            raise KeyError(task_id)
        if rec.state != AgentTaskState.WAITING_PERMISSION:
            return rec
        if grant.device_id != rec.device_id:
            raise PermissionError("grant device does not match task device")
        intent = AgentIntent(
            text=rec.intent_text,
            device_id=rec.device_id,
            echo_task_id=rec.task_id,
            conversation_id=rec.conversation_id,
            message_id=rec.message_id,
            title=rec.title,
            task_kind=rec.task_kind,
            context=rec.envelope.get("context") if isinstance(rec.envelope.get("context"), dict) else {},
            output_contract=(
                rec.envelope.get("output_contract")
                if isinstance(rec.envelope.get("output_contract"), dict)
                else {}
            ),
            grant_id=grant.grant_id,
            permission_profile=grant.permission_profile,
            timeout_s=rec.timeout_s,
        )
        result = await self.backend.submit(intent)
        if not result.accepted:
            await self.record_task_event(
                EchoTaskEvent(
                    task_id=rec.task_id,
                    title=rec.title,
                    event="task.failed",
                    state="failed",
                    message=result.error or "任务暂时无法启动",
                )
            )
            return await self.get_task(rec.task_id) or rec
        await self.touch_grant(grant.grant_id)
        async with self._conn() as conn:
            await conn.execute(
                """UPDATE agent_tasks
                   SET runner_task_id = ?, state = 'pending', grant_id = ?,
                       permission_profile = ?, progress_text = ?
                   WHERE task_id = ?""",
                (
                    result.runner_task_id,
                    grant.grant_id,
                    grant.permission_profile,
                    "任务已提交，等待执行",
                    rec.task_id,
                ),
            )
            await conn.commit()
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

    async def _insert_task(
        self,
        *,
        intent: AgentIntent,
        result: AgentSubmitResult,
        state: AgentTaskState,
    ) -> AgentTaskRecord:
        now = utc_now_iso()
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
        async with self._conn() as conn:
            await conn.execute(
                """INSERT OR REPLACE INTO agent_tasks
                   (task_id, runner_task_id, device_id, conversation_id, message_id,
                    title, intent_text, route, task_kind, state, progress_text,
                    final_text, error, artifacts_json, snapshot_json, envelope_json,
                    grant_id, permission_profile, last_seq, submitted_at, finished_at, timeout_s)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '[]', ?, ?,
                           ?, ?, 0, ?, NULL, ?)""",
                (
                    task_id,
                    result.runner_task_id,
                    intent.device_id,
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
                    now,
                    intent.timeout_s,
                ),
            )
            await conn.commit()
        rec = await self.get_task(task_id)
        if rec is None:
            raise RuntimeError(f"agent task insert failed: {task_id}")
        return rec

    async def record_task_event(
        self,
        event: EchoTaskEvent,
        *,
        raw_hash: str | None = None,
    ) -> EchoTaskEvent | None:
        async with self._lock, self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                (event.task_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return None
            rec = _row_to_record(row)
            if raw_hash:
                cur = await conn.execute(
                    """SELECT seq FROM agent_task_events
                       WHERE task_id = ? AND raw_event_hash = ?""",
                    (event.task_id, raw_hash),
                )
                dup = await cur.fetchone()
                await cur.close()
                if dup is not None:
                    return None
            seq = rec.last_seq + 1
            stored = event.model_copy(update={"seq": seq})
            snapshot = reduce_snapshot(rec.snapshot, stored)
            stored = stored.model_copy(update={"snapshot": snapshot})
            state = _state(stored.state)
            finished_at = utc_now_iso() if state.is_terminal else rec.finished_at
            payload_json = stored.model_dump_json()
            await conn.execute(
                """INSERT INTO agent_task_events
                   (task_id, seq, event, state, visibility, payload_json,
                    raw_event_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    stored.task_id,
                    seq,
                    stored.event,
                    stored.state,
                    stored.visibility,
                    payload_json,
                    raw_hash,
                    stored.ts,
                ),
            )
            await conn.execute(
                """UPDATE agent_tasks
                   SET state = ?, progress_text = ?, final_text = ?, error = ?,
                       artifacts_json = ?, snapshot_json = ?, last_seq = ?,
                       runner_task_id = COALESCE(?, runner_task_id),
                       finished_at = ?
                   WHERE task_id = ?""",
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
                ),
            )
            await conn.commit()
        await self.event_bus.publish(
            EchoEvent(
                type="agent.task.event",
                payload=stored.model_dump(mode="json"),
            )
        )
        return stored

    async def get_task(self, task_id: str) -> AgentTaskRecord | None:
        async with self._conn() as conn:
            cur = await conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,))
            row = await cur.fetchone()
            await cur.close()
        return _row_to_record(row) if row else None

    async def list_tasks(self, *, device_id: str | None = None, limit: int = 50) -> list[AgentTaskRecord]:
        sql = "SELECT * FROM agent_tasks"
        args: list[Any] = []
        if device_id:
            sql += " WHERE device_id = ?"
            args.append(device_id)
        sql += " ORDER BY submitted_at DESC LIMIT ?"
        args.append(limit)
        async with self._conn() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_record(r) for r in rows]

    async def list_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
    ) -> tuple[list[EchoTaskEvent], dict[str, Any], int]:
        rec = await self.get_task(task_id)
        if rec is None:
            return [], {}, 0
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT payload_json FROM agent_task_events
                   WHERE task_id = ? AND seq > ?
                   ORDER BY seq ASC""",
                (task_id, after_seq),
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
        if rec.runner_task_id:
            await self.backend.cancel(rec.runner_task_id)
        await self.record_task_event(
            EchoTaskEvent(
                task_id=rec.task_id,
                runner_task_id=rec.runner_task_id,
                title=rec.title,
                event="task.cancelled",
                state="cancelled",
                message="任务已取消",
            )
        )
        task = self._bridge_tasks.pop(rec.task_id, None)
        if task:
            task.cancel()
        return await self.get_task(task_id)

    def start_bridge_for_task(self, rec: AgentTaskRecord) -> None:
        if rec.route != RUNNER_CLAUDE_CODE or not rec.runner_task_id or not self.backend.enabled:
            return
        existing = self._bridge_tasks.get(rec.task_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_bridge(rec), name=f"agent-bridge:{rec.task_id}")
        self._bridge_tasks[rec.task_id] = task

        def _cleanup(done: asyncio.Task[None]) -> None:
            self._bridge_tasks.pop(rec.task_id, None)
            if not done.cancelled():
                exc = done.exception()
                if exc:
                    _log.warning("agent bridge crashed task=%s: %s", rec.task_id, exc)

        task.add_done_callback(_cleanup)

    async def _run_bridge(self, rec: AgentTaskRecord) -> None:
        assert rec.runner_task_id is not None
        bridge = EchoTaskStreamBridge(
            task_id=rec.task_id,
            runner_task_id=rec.runner_task_id,
            agentos_base_url=self.backend.base_url,
            recorder=self.record_task_event,
            conversation_id=rec.conversation_id,
            message_id=rec.message_id,
            title=rec.title,
        )
        await bridge.run()

    async def restore_unfinished(self) -> int:
        if not self.backend.enabled:
            return 0
        count = 0
        for rec in await self.list_tasks(limit=200):
            if rec.state in {AgentTaskState.PENDING, AgentTaskState.RUNNING} and rec.runner_task_id:
                self.start_bridge_for_task(rec)
                count += 1
        return count

    async def aclose(self) -> None:
        tasks = list(self._bridge_tasks.values())
        self._bridge_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


_service: AgentTaskService | None = None


def get_agent_task_service(settings: Settings, event_bus: InMemoryEventBus) -> AgentTaskService:
    global _service  # noqa: PLW0603
    if _service is None:
        _service = AgentTaskService(settings, event_bus)
    return _service


async def aclose_agent_task_service() -> None:
    global _service  # noqa: PLW0603
    if _service is not None:
        await _service.aclose()
        _service = None


def reset_agent_task_service_for_test() -> None:
    global _service  # noqa: PLW0603
    _service = None
