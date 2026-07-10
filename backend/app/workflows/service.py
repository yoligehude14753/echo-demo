"""Workflow 0.3 状态机与事件投影。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from app.adapters.event_bus.inmemory import InMemoryEventBus
from app.config import Settings
from app.schemas.events import EchoEvent
from app.schemas.workflow import (
    TERMINAL_WORKFLOW_STATES,
    WorkflowEventDTO,
    WorkflowRunCreate,
    WorkflowRunDTO,
    WorkflowState,
    WorkflowVisibility,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_workflow_run_id() -> str:
    return f"run_{uuid4().hex}"


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


@dataclass(slots=True)
class WorkflowRunRecord:
    run_id: str
    kind: str
    source: str
    state: str
    title: str | None
    intent_text: str
    meeting_id: str | None = None
    todo_id: str | None = None
    agent_task_id: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    timeout_s: float | None = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_WORKFLOW_STATES

    def to_dto(self) -> WorkflowRunDTO:
        return WorkflowRunDTO(
            run_id=self.run_id,
            kind=self.kind,
            source=self.source,
            state=self.state,  # type: ignore[arg-type]
            title=self.title,
            intent_text=self.intent_text,
            meeting_id=self.meeting_id,
            todo_id=self.todo_id,
            agent_task_id=self.agent_task_id,
            input=self.input,
            output=self.output,
            error=self.error,
            timeout_s=self.timeout_s,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            updated_at=self.updated_at,
        )


@dataclass(slots=True)
class WorkflowEventRecord:
    run_id: str
    seq: int
    event_type: str
    state: str
    visibility: str
    message: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dto(self) -> WorkflowEventDTO:
        return WorkflowEventDTO(
            run_id=self.run_id,
            seq=self.seq,
            event_type=self.event_type,
            state=self.state,  # type: ignore[arg-type]
            visibility=self.visibility,  # type: ignore[arg-type]
            message=self.message,
            payload=self.payload,
            created_at=self.created_at,
        )


def _row_to_run(row: aiosqlite.Row) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=row["run_id"],
        kind=row["kind"],
        source=row["source"],
        state=row["state"],
        title=row["title"],
        intent_text=row["intent_text"],
        meeting_id=row["meeting_id"],
        todo_id=row["todo_id"],
        agent_task_id=row["agent_task_id"],
        input=_json_loads(row["input_json"], {}),
        output=_json_loads(row["output_json"], {}),
        error=row["error"],
        timeout_s=float(row["timeout_s"]) if row["timeout_s"] is not None else None,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event(row: aiosqlite.Row) -> WorkflowEventRecord:
    return WorkflowEventRecord(
        run_id=row["run_id"],
        seq=int(row["seq"]),
        event_type=row["event_type"],
        state=row["state"],
        visibility=row["visibility"],
        message=row["message"],
        payload=_json_loads(row["payload_json"], {}),
        created_at=row["created_at"],
    )


class WorkflowService:
    """持久化 workflow run/event，并把状态投影到主 WebSocket。"""

    def __init__(self, settings: Settings, event_bus: InMemoryEventBus) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(str(self.settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys=ON")
            yield conn

    async def create_run(
        self,
        body: WorkflowRunCreate,
        *,
        run_id: str | None = None,
    ) -> WorkflowRunRecord:
        now = utc_now_iso()
        record = WorkflowRunRecord(
            run_id=run_id or new_workflow_run_id(),
            kind=body.kind,
            source=body.source,
            state="pending",
            title=body.title,
            intent_text=body.intent_text,
            meeting_id=body.meeting_id,
            todo_id=body.todo_id,
            agent_task_id=body.agent_task_id,
            input=dict(body.input),
            output={},
            timeout_s=body.timeout_s,
            created_at=now,
            updated_at=now,
        )
        async with self._conn() as conn:
            await conn.execute(
                """INSERT INTO workflow_runs
                   (run_id, kind, source, state, title, intent_text, meeting_id, todo_id,
                    agent_task_id, input_json, output_json, error, timeout_s, created_at,
                    started_at, finished_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', NULL, ?, ?, NULL, NULL, ?)""",
                (
                    record.run_id,
                    record.kind,
                    record.source,
                    record.state,
                    record.title,
                    record.intent_text,
                    record.meeting_id,
                    record.todo_id,
                    record.agent_task_id,
                    json.dumps(record.input, ensure_ascii=False),
                    record.timeout_s,
                    record.created_at,
                    record.updated_at,
                ),
            )
            await conn.commit()
        await self.record_event(
            record.run_id,
            "workflow.created",
            message=record.title or record.intent_text[:120],
            payload={"kind": record.kind, "source": record.source},
            visibility="debug",
        )
        await self.publish_snapshot(record.run_id)
        return await self.get_run(record.run_id) or record

    async def get_run(self, run_id: str) -> WorkflowRunRecord | None:
        async with self._conn() as conn:
            cur = await conn.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,))
            row = await cur.fetchone()
            await cur.close()
        return _row_to_run(row) if row else None

    async def list_runs(
        self,
        *,
        meeting_id: str | None = None,
        todo_id: str | None = None,
        agent_task_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[WorkflowRunRecord]:
        clauses: list[str] = []
        args: list[Any] = []
        if meeting_id:
            clauses.append("meeting_id = ?")
            args.append(meeting_id)
        if todo_id:
            clauses.append("todo_id = ?")
            args.append(todo_id)
        if agent_task_id:
            clauses.append("agent_task_id = ?")
            args.append(agent_task_id)
        if state:
            clauses.append("state = ?")
            args.append(state)
        sql = "SELECT * FROM workflow_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        async with self._conn() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_run(row) for row in rows]

    async def list_events(self, run_id: str, *, after_seq: int = 0) -> list[WorkflowEventRecord]:
        async with self._conn() as conn:
            cur = await conn.execute(
                """SELECT * FROM workflow_events
                   WHERE run_id = ? AND seq > ?
                   ORDER BY seq ASC""",
                (run_id, after_seq),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_event(row) for row in rows]

    async def record_event(
        self,
        run_id: str,
        event_type: str,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        visibility: WorkflowVisibility = "user",
    ) -> WorkflowEventRecord | None:
        async with self._lock, self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ?",
                (run_id,),
            )
            run_row = await cur.fetchone()
            await cur.close()
            if run_row is None:
                return None
            run = _row_to_run(run_row)
            cur = await conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM workflow_events WHERE run_id = ?",
                (run_id,),
            )
            seq_row = await cur.fetchone()
            await cur.close()
            seq = int(seq_row["next_seq"] if seq_row else 1)
            created_at = utc_now_iso()
            event = WorkflowEventRecord(
                run_id=run_id,
                seq=seq,
                event_type=event_type,
                state=run.state,
                visibility=visibility,
                message=message,
                payload=dict(payload or {}),
                created_at=created_at,
            )
            await conn.execute(
                """INSERT INTO workflow_events
                   (run_id, seq, event_type, state, visibility, message, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.run_id,
                    event.seq,
                    event.event_type,
                    event.state,
                    event.visibility,
                    event.message,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at,
                ),
            )
            await conn.commit()

        await self.event_bus.publish(
            EchoEvent(
                type="workflow.event",
                meeting_id=run.meeting_id,
                payload=event.to_dto().model_dump(mode="json"),
            )
        )
        return event

    async def _set_state(
        self,
        run_id: str,
        state: WorkflowState,
        *,
        event_type: str,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> WorkflowRunRecord | None:
        now = utc_now_iso()
        async with self._conn() as conn:
            cur = await conn.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,))
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return None
            rec = _row_to_run(row)
            if rec.is_terminal and state != rec.state:
                return rec
            started_at = now if started and rec.started_at is None else rec.started_at
            finished_at = now if finished else rec.finished_at
            next_output = rec.output if output is None else output
            await conn.execute(
                """UPDATE workflow_runs
                   SET state = ?, output_json = ?, error = ?, started_at = ?,
                       finished_at = ?, updated_at = ?
                   WHERE run_id = ?""",
                (
                    state,
                    json.dumps(next_output, ensure_ascii=False),
                    error,
                    started_at,
                    finished_at,
                    now,
                    run_id,
                ),
            )
            await conn.commit()
        await self.record_event(run_id, event_type, message=message, payload=payload or {})
        await self.publish_snapshot(run_id)
        return await self.get_run(run_id)

    async def start_run(self, run_id: str) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "running",
            event_type="workflow.started",
            message="任务开始执行",
            started=True,
        )

    async def complete_run(
        self,
        run_id: str,
        *,
        output: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "succeeded",
            event_type="workflow.succeeded",
            message=message or "任务完成",
            output=output or {},
            finished=True,
        )

    async def fail_run(
        self,
        run_id: str,
        *,
        error: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "failed",
            event_type="workflow.failed",
            message=error,
            payload=payload or {"error": error},
            error=error,
            finished=True,
        )

    async def timeout_run(self, run_id: str, *, error: str = "workflow timeout") -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "timeout",
            event_type="workflow.timeout",
            message=error,
            payload={"error": error},
            error=error,
            finished=True,
        )

    async def request_cancel(
        self,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "cancel_requested",
            event_type="workflow.cancel_requested",
            message=reason or "已请求取消",
            payload={"reason": reason} if reason else {},
        )

    async def mark_cancelled(
        self,
        run_id: str,
        *,
        message: str = "任务已取消",
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "cancelled",
            event_type="workflow.cancelled",
            message=message,
            finished=True,
        )

    async def mark_cancel_failed(
        self,
        run_id: str,
        *,
        error: str,
    ) -> WorkflowRunRecord | None:
        return await self._set_state(
            run_id,
            "cancel_failed",
            event_type="workflow.cancel_failed",
            message=error,
            payload={"error": error},
            error=error,
            finished=True,
        )

    async def retry_run(
        self,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> WorkflowRunRecord | None:
        old = await self.get_run(run_id)
        if old is None:
            return None
        retry_input = dict(old.input)
        retry_input["retry_of"] = old.run_id
        if reason:
            retry_input["retry_reason"] = reason
        new_run = await self.create_run(
            WorkflowRunCreate(
                kind=old.kind,
                source=old.source,
                title=old.title,
                intent_text=old.intent_text,
                meeting_id=old.meeting_id,
                todo_id=old.todo_id,
                agent_task_id=old.agent_task_id,
                input=retry_input,
                timeout_s=old.timeout_s,
            )
        )
        await self.record_event(
            old.run_id,
            "workflow.retry_created",
            message="已创建重试任务",
            payload={"retry_run_id": new_run.run_id, "reason": reason},
            visibility="debug",
        )
        return new_run

    async def publish_snapshot(self, run_id: str) -> None:
        rec = await self.get_run(run_id)
        if rec is None:
            return
        await self.event_bus.publish(
            EchoEvent(
                type="workflow.snapshot",
                meeting_id=rec.meeting_id,
                payload=rec.to_dto().model_dump(mode="json"),
            )
        )

    async def restore_unfinished(self) -> int:
        runs = await self.list_runs(limit=500)
        count = 0
        for rec in runs:
            if rec.is_terminal:
                continue
            await self.record_event(
                rec.run_id,
                "workflow.restored",
                message="任务已从本地历史恢复",
                payload={"state": rec.state},
                visibility="debug",
            )
            await self.publish_snapshot(rec.run_id)
            count += 1
        return count


_service: WorkflowService | None = None


def get_workflow_service(settings: Settings, event_bus: InMemoryEventBus) -> WorkflowService:
    global _service  # noqa: PLW0603
    if _service is None:
        _service = WorkflowService(settings, event_bus)
    return _service


def reset_workflow_service_for_test() -> None:
    global _service  # noqa: PLW0603
    _service = None
