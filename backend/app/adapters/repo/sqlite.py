"""SQLite 实现 RepositoryPort，单文件 ``~/.echodesk/echodesk.db``。

特性：
- 启动时 ``init()`` 打开连接 + 设 PRAGMA（WAL + foreign_keys）+ 跑 schema migration
- schema DDL 由 ``app.adapters.repo.migrator.run_migrations`` 负责（P2.4）；
  本类不再维护 inline ``CREATE TABLE`` 字面值——破坏性变更通过新增
  ``migrations/NNN_*.sql`` 加 schema_version 来推进
- 所有写路径串行通过 ``asyncio.Lock``，规避 sqlite 的"database is locked"
- aiosqlite 单连接（开 WAL），单进程并发足够
- 时间戳统一存 ISO-8601 UTC
- speaker_id / speaker_label 全程可空（旧数据兼容）

不在本类做的事：
- DDL / schema migration（→ ``migrator.py`` + ``migrations/NNN_*.sql``）
- 业务校验、事件 I/O 发布（留给 use_case/dispatcher；本层只做同事务 outbox staging）
- 大对象（音频文件本体）→ 文件系统存，DB 只存 ref
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from app.adapters.repo.connection import (
    SQLITE_BUSY_TIMEOUT_MS,
    configure_aiosqlite_connection,
    rollback_aiosqlite_connection,
)
from app.adapters.repo.migrator import run_migrations
from app.ports.repository import (
    AmbientAudioFileRecord,
    AmbientMeetingImportResult,
    AmbientSegmentRecord,
    CaptureAmbientAppendResult,
    CaptureClaimLost,
    CaptureClaimStatus,
    CaptureIdempotencyConflict,
    CaptureMeetingAppendResult,
    CaptureReceiptSettlement,
    CaptureRequestClaim,
    MeetingCreateResult,
    MeetingRecord,
    MeetingState,
    MinutesStatus,
    RagProjectionState,
    RepositoryPort,
    SpeakerProfileRecord,
)
from app.schemas.meeting import TranscriptSegment
from app.security.context import current_principal

_RAG_PROJECTION_RETRY_BASE_S = 5.0
_RAG_PROJECTION_RETRY_MAX_S = 3600.0
_MEETING_RAG_PROJECTION_SOURCE_STATES: dict[
    RagProjectionState,
    tuple[RagProjectionState, ...],
] = {
    "indexed": ("index_pending", "index_failed", "indexed"),
    "index_failed": ("index_pending", "index_failed"),
    "deleted": ("delete_pending", "delete_failed", "deleted"),
    "delete_failed": ("delete_pending", "delete_failed"),
}
_AMBIENT_RAG_PROJECTION_SOURCE_STATES: dict[
    RagProjectionState,
    tuple[RagProjectionState, ...],
] = {
    "indexed": ("reconcile_pending", "index_pending", "index_failed", "indexed"),
    "index_failed": ("reconcile_pending", "index_pending", "index_failed"),
}
_CAPTURE_RECEIPT_COLUMNS = """operation_key_hash, request_fingerprint, state,
    lease_holder, lease_fence, lease_expires_at, ambient_stored,
    ambient_segment_id, meeting_id, stt_status, capture_mode"""


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    parsed = datetime.fromisoformat(s)
    # Legacy SQLite rows may contain UTC values without an offset (for example
    # values written by CURRENT_TIMESTAMP).  Normalize them at the repository
    # boundary so API clients never reinterpret a UTC value as local time.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _rag_projection_retry_at(attempts: int) -> datetime:
    delay_s = min(
        _RAG_PROJECTION_RETRY_MAX_S,
        _RAG_PROJECTION_RETRY_BASE_S * (2 ** min(max(0, attempts - 1), 10)),
    )
    return datetime.now(UTC) + timedelta(seconds=delay_s)


def _scope() -> tuple[str, str, str]:
    """Return the server-validated persistence scope for the current request."""

    principal = current_principal()
    return principal.tenant_id, principal.device_id, principal.owner_id


class SQLiteRepository(RepositoryPort):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._capture_receipt_changed = asyncio.Event()

    async def init(self) -> None:
        """打开连接 + 设 PRAGMA + 跑 schema migration（P2.4）。

        lifespan 已会先调一次 ``run_migrations`` 拿到结构化结果用于日志/早失败；
        这里再跑一次做兜底，覆盖直接构造 ``SQLiteRepository`` 的调用方
        （主要是 unit test ``SQLiteRepository(tmp_path / "echo.db"); await repo.init()``）。
        已应用的版本会被 skip，幂等，无副作用。

        若 migration 失败抛 ``RuntimeError``——半成品 schema 不如直接停。
        """
        async with self._lock:
            if self._conn is not None:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # migration 走独立连接（不抢 self._conn 的 lock）
        result = await run_migrations(self._db_path)
        if result.errors:
            raise RuntimeError(f"sqlite migrations failed: {result.errors}")
        async with self._lock:
            self._conn = await aiosqlite.connect(
                str(self._db_path),
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            )
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await configure_aiosqlite_connection(self._conn)
            await self._conn.commit()

    async def aclose(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteRepository.init() not called")
        return self._conn

    # ── Meetings ─────────────────────────────────────────────────
    async def create_meeting(
        self,
        meeting_id: str,
        *,
        started_at: datetime,
        title: str | None = None,
        auto_started: bool = False,
        state_event_reason: str | None = None,
    ) -> MeetingRecord:
        result = await self.create_meeting_boundary(
            meeting_id,
            started_at=started_at,
            title=title,
            auto_started=auto_started,
            state_event_reason=state_event_reason,
        )
        return result.meeting

    async def create_meeting_boundary(
        self,
        meeting_id: str,
        *,
        started_at: datetime,
        title: str | None = None,
        auto_started: bool = False,
        state_event_reason: str | None = None,
    ) -> MeetingCreateResult:
        """Persist one active meeting and its observable start events atomically.

        ``BEGIN IMMEDIATE`` serializes the check/insert across repository
        instances.  Migration 033's partial unique index is the final arbiter;
        a losing process adopts that row instead of returning a phantom id or
        surfacing an HTTP 500.  Only the transaction that inserts the active
        row stages events, so retries and concurrent losers cannot duplicate
        ``meeting.started`` / ``meeting.state_changed``.
        """
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = False
                try:
                    created = await conn.execute(
                        "INSERT INTO meetings "
                        "(id, title, state, started_at, auto_started, "
                        "tenant_id, device_id, owner_id) "
                        "VALUES (?, ?, 'in_meeting', ?, ?, ?, ?, ?)",
                        (
                            meeting_id,
                            title,
                            _to_iso(started_at),
                            1 if auto_started else 0,
                            tenant_id,
                            device_id,
                            owner_id,
                        ),
                    )
                    inserted = created.rowcount == 1
                    await created.close()
                except sqlite3.IntegrityError:
                    # The conflict may be either the same meeting id or the
                    # owner-scoped active-meeting index.  In both cases the
                    # authoritative active row is the only valid response.
                    inserted = False
                cur = await conn.execute(
                    "SELECT id, title, state, started_at, ended_at, finalized_at, "
                    "auto_started, minutes_json, raw_transcript_ref, "
                    "minutes_status, minutes_error, display_title, minutes_cleared_at, "
                    "rag_projection_state, rag_projection_error, rag_projected_at, "
                    "rag_projection_attempts, rag_projection_next_retry_at, "
                    "rag_projection_generation, minutes_generation_run_id, "
                    "minutes_generation_cancelled_at "
                    "FROM meetings WHERE tenant_id = ? AND owner_id = ? "
                    "AND state = 'in_meeting' ORDER BY started_at DESC, id DESC LIMIT 1",
                    (tenant_id, owner_id),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise RuntimeError("meeting insert conflicted without an active meeting")
                if inserted:
                    event_rows = [
                        (
                            tenant_id,
                            device_id,
                            owner_id,
                            meeting_id,
                            "meeting.started",
                            json.dumps(
                                {
                                    "meeting_id": meeting_id,
                                    "payload": {"auto_started": auto_started},
                                },
                                ensure_ascii=False,
                            ),
                            _to_iso(started_at),
                        )
                    ]
                    if state_event_reason is not None:
                        event_rows.append(
                            (
                                tenant_id,
                                device_id,
                                owner_id,
                                meeting_id,
                                "meeting.state_changed",
                                json.dumps(
                                    {
                                        "meeting_id": meeting_id,
                                        "payload": {
                                            "mode": "in_meeting",
                                            "started_by": "auto" if auto_started else "manual",
                                            "reason": state_event_reason,
                                        },
                                    },
                                    ensure_ascii=False,
                                ),
                                _to_iso(started_at),
                            )
                        )
                    await conn.executemany(
                        """INSERT INTO workflow_outbox
                           (tenant_id, device_id, owner_id, aggregate_type, aggregate_id,
                            event_type, payload_json, created_at)
                           VALUES (?, ?, ?, 'domain', ?, ?, ?, ?)""",
                        event_rows,
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return MeetingCreateResult(meeting=_meeting_from_row(row), created=inserted)

    async def update_meeting_state(  # noqa: PLR0912,PLR0915 - sparse update contract
        self,
        meeting_id: str,
        *,
        state: MeetingState,
        title: str | None = None,
        ended_at: datetime | None = None,
        finalized_at: datetime | None = None,
        minutes_json: str | None = None,
        raw_transcript_ref: str | None = None,
        minutes_status: MinutesStatus | None = None,
        minutes_error: str | None = None,
        display_title: str | None = None,
        rag_projection_state: RagProjectionState | None = None,
        rag_projection_error: str | None = None,
        rag_projected_at: datetime | None = None,
    ) -> int | None:
        tenant_id, _device_id, owner_id = _scope()
        # 用动态 SET 列表，避免空字段误改
        fields: list[str] = ["state = ?"]
        values: list[object] = [state]
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if ended_at is not None:
            fields.append("ended_at = ?")
            values.append(_to_iso(ended_at))
        if finalized_at is not None:
            fields.append("finalized_at = ?")
            values.append(_to_iso(finalized_at))
        if minutes_json is not None:
            fields.append("minutes_json = ?")
            values.append(minutes_json)
        if raw_transcript_ref is not None:
            fields.append("raw_transcript_ref = ?")
            values.append(raw_transcript_ref)
        if minutes_status is not None:
            fields.append("minutes_status = ?")
            values.append(minutes_status)
            # Any explicit generation attempt supersedes an older user-clear
            # tombstone. This keeps subsequent failed attempts recoverable.
            fields.append("minutes_cleared_at = NULL")
            fields.append("minutes_generation_cancelled_at = NULL")
            if minutes_status in {"ok", "generation_failed", "no_content"}:
                fields.append("minutes_generation_run_id = NULL")
        if minutes_error is not None:
            fields.append("minutes_error = ?")
            values.append(minutes_error)
        if display_title is not None:
            fields.append("display_title = ?")
            values.append(display_title)
        if rag_projection_state is not None:
            fields.append("rag_projection_state = ?")
            values.append(rag_projection_state)
            if rag_projection_state in {
                "index_pending",
                "delete_pending",
                "indexed",
                "deleted",
            }:
                fields.extend(
                    [
                        "rag_projection_attempts = 0",
                        "rag_projection_next_retry_at = NULL",
                    ]
                )
            if rag_projection_state in {"index_pending", "delete_pending"}:
                fields.append("rag_projection_generation = rag_projection_generation + 1")
        if rag_projection_error is not None:
            fields.append("rag_projection_error = ?")
            values.append(rag_projection_error)
        if rag_projected_at is not None:
            fields.append("rag_projected_at = ?")
            values.append(_to_iso(rag_projected_at))
        values.extend((meeting_id, tenant_id, owner_id))
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                f"UPDATE meetings SET {', '.join(fields)} "
                "WHERE id = ? AND tenant_id = ? AND owner_id = ? "
                "RETURNING rag_projection_generation",
                values,
            )
            row = await cur.fetchone()
            await cur.close()
            await conn.commit()
        return int(row[0]) if row is not None else None

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT id, title, state, started_at, ended_at, finalized_at, "
                "auto_started, minutes_json, raw_transcript_ref, "
                "minutes_status, minutes_error, display_title, minutes_cleared_at, "
                "rag_projection_state, rag_projection_error, rag_projected_at, "
                "rag_projection_attempts, rag_projection_next_retry_at, "
                "rag_projection_generation, minutes_generation_run_id, "
                "minutes_generation_cancelled_at "
                "FROM meetings WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                (meeting_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return _meeting_from_row(row)

    async def list_meetings(
        self,
        *,
        state: MeetingState | None = None,
        limit: int = 50,
    ) -> list[MeetingRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            sql = (
                "SELECT id, title, state, started_at, ended_at, finalized_at, "
                "auto_started, minutes_json, raw_transcript_ref, "
                "minutes_status, minutes_error, display_title, minutes_cleared_at, "
                "rag_projection_state, rag_projection_error, rag_projected_at, "
                "rag_projection_attempts, rag_projection_next_retry_at, "
                "rag_projection_generation, minutes_generation_run_id, "
                "minutes_generation_cancelled_at FROM meetings "
                "WHERE tenant_id = ? AND owner_id = ?"
            )
            args: tuple[object, ...] = (tenant_id, owner_id)
            if state is not None:
                sql += " AND state = ?"
                args = (*args, state)
            sql += " ORDER BY started_at DESC LIMIT ?"
            args = (*args, limit)
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        return [_meeting_from_row(r) for r in rows]

    async def clear_meeting_outputs(
        self,
        meeting_id: str,
        *,
        clear_minutes: bool = True,
    ) -> None:
        if not clear_minutes:
            return
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                "UPDATE meetings SET "
                "state = CASE WHEN state = 'finalized' THEN 'ended' ELSE state END, "
                "minutes_json = NULL, minutes_status = NULL, minutes_error = NULL, "
                "display_title = NULL, finalized_at = NULL, "
                "minutes_cleared_at = CURRENT_TIMESTAMP, "
                "rag_projection_state = 'delete_pending', rag_projection_error = NULL, "
                "rag_projected_at = NULL, rag_projection_attempts = 0, "
                "rag_projection_next_retry_at = NULL, "
                "rag_projection_generation = rag_projection_generation + 1, "
                "minutes_generation_run_id = NULL, "
                "minutes_generation_cancelled_at = NULL "
                "WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                (meeting_id, tenant_id, owner_id),
            )
            await conn.commit()

    async def set_meeting_rag_projection(
        self,
        meeting_id: str,
        *,
        state: RagProjectionState,
        error: str | None = None,
        projected_at: datetime | None = None,
        retry_backoff: bool = False,
        expected_generation: int | None = None,
    ) -> bool:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                attempts = 0
                next_retry_at: datetime | None = None
                if retry_backoff and state in {"index_failed", "delete_failed"}:
                    cur = await conn.execute(
                        "SELECT rag_projection_attempts FROM meetings "
                        "WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                        (meeting_id, tenant_id, owner_id),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    attempts = int(row[0] if row else 0) + 1
                    next_retry_at = _rag_projection_retry_at(attempts)
                where_generation = (
                    " AND rag_projection_generation = ?" if expected_generation is not None else ""
                )
                allowed_source_states = _MEETING_RAG_PROJECTION_SOURCE_STATES.get(state)
                where_state = ""
                if allowed_source_states:
                    placeholders = ", ".join("?" for _ in allowed_source_states)
                    where_state = f" AND rag_projection_state IN ({placeholders})"
                values: list[object] = [
                    state,
                    (error or "")[:500] or None,
                    _to_iso(projected_at) if projected_at is not None else None,
                    attempts,
                    _to_iso(next_retry_at) if next_retry_at is not None else None,
                    meeting_id,
                    tenant_id,
                    owner_id,
                ]
                if expected_generation is not None:
                    values.append(expected_generation)
                if allowed_source_states:
                    values.extend(allowed_source_states)
                changed = await conn.execute(
                    """UPDATE meetings
                       SET rag_projection_state = ?, rag_projection_error = ?,
                           rag_projected_at = ?, rag_projection_attempts = ?,
                           rag_projection_next_retry_at = ?
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?"""
                    + where_generation
                    + where_state,
                    values,
                )
                updated = changed.rowcount == 1
                await changed.close()
                await conn.commit()
                return updated
            except BaseException:
                await conn.rollback()
                raise

    async def list_meetings_needing_rag_projection(
        self,
        *,
        limit: int = 100,
    ) -> list[MeetingRecord]:
        tenant_id, _device_id, owner_id = _scope()
        now = _to_iso(datetime.now(UTC))
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT id, title, state, started_at, ended_at, finalized_at,
                          auto_started, minutes_json, raw_transcript_ref,
                          minutes_status, minutes_error, display_title, minutes_cleared_at,
                          rag_projection_state, rag_projection_error, rag_projected_at,
                          rag_projection_attempts, rag_projection_next_retry_at,
                          rag_projection_generation, minutes_generation_run_id,
                          minutes_generation_cancelled_at
                   FROM meetings
                   WHERE tenant_id = ? AND owner_id = ?
                     AND rag_projection_state IN (
                         'index_pending', 'index_failed', 'delete_pending', 'delete_failed'
                     )
                     AND (
                         rag_projection_next_retry_at IS NULL
                         OR rag_projection_next_retry_at <= ?
                     )
                   ORDER BY COALESCE(rag_projection_next_retry_at, ''), started_at ASC
                   LIMIT ?""",
                (tenant_id, owner_id, now, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_meeting_from_row(row) for row in rows]

    async def list_meeting_rag_projection_scopes(self) -> list[tuple[str, str, str]]:
        """Internal startup repair scopes; request-facing reads remain principal scoped."""

        now = _to_iso(datetime.now(UTC))
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT tenant_id, MIN(device_id), owner_id
                   FROM meetings
                   WHERE rag_projection_state IN (
                       'index_pending', 'index_failed', 'delete_pending', 'delete_failed'
                   )
                     AND (
                         rag_projection_next_retry_at IS NULL
                         OR rag_projection_next_retry_at <= ?
                     )
                   GROUP BY tenant_id, owner_id
                   ORDER BY tenant_id, owner_id""",
                (now,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    async def list_rag_projection_scopes(self) -> list[tuple[str, str, str]]:
        """Return every principal scope with due meeting or ambient work."""

        now = _to_iso(datetime.now(UTC))
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT tenant_id, MIN(device_id), owner_id
                   FROM (
                       SELECT tenant_id, device_id, owner_id
                       FROM meetings
                       WHERE rag_projection_state IN (
                           'index_pending', 'index_failed', 'delete_pending', 'delete_failed'
                       )
                         AND (
                             rag_projection_next_retry_at IS NULL
                             OR rag_projection_next_retry_at <= ?
                         )
                       UNION ALL
                       SELECT tenant_id, device_id, owner_id
                       FROM ambient_segments
                       WHERE rag_projection_state IN (
                           'reconcile_pending', 'index_pending', 'index_failed'
                       )
                         AND (
                             rag_projection_next_retry_at IS NULL
                             OR rag_projection_next_retry_at <= ?
                         )
                   ) AS due_projection
                   GROUP BY tenant_id, owner_id
                   ORDER BY tenant_id, owner_id""",
                (now, now),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    # ── Meeting segments ────────────────────────────────────────
    async def append_meeting_segment(
        self,
        meeting_id: str,
        seg: TranscriptSegment,
        *,
        captured_at: datetime,
    ) -> bool:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "INSERT INTO meeting_segments "
                "(meeting_id, text, start_ms, end_ms, speaker_id, speaker_label, captured_at, "
                "tenant_id, device_id, owner_id) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
                "WHERE EXISTS (SELECT 1 FROM meetings "
                "WHERE id = ? AND tenant_id = ? AND owner_id = ? "
                "AND state = 'in_meeting')",
                (
                    meeting_id,
                    seg.text,
                    seg.start_ms,
                    seg.end_ms,
                    seg.speaker_id,
                    seg.speaker_label,
                    _to_iso(captured_at),
                    tenant_id,
                    device_id,
                    owner_id,
                    meeting_id,
                    tenant_id,
                    owner_id,
                ),
            )
            inserted = cur.rowcount == 1
            await cur.close()
            await conn.commit()
        return inserted

    async def append_capture_meeting_segments(
        self,
        meeting_id: str,
        segments: list[TranscriptSegment],
        *,
        captured_at: datetime,
        capture_operation_key: str,
        request_fingerprint: str,
    ) -> CaptureMeetingAppendResult:
        """原子写入一次 capture 的全部 meeting 段落，或返回规范重放行。"""

        if not capture_operation_key or not request_fingerprint:
            raise ValueError("capture operation key and request fingerprint are required")
        if not segments:
            return CaptureMeetingAppendResult(accepted=True, inserted=False, segments=[])
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._load_capture_meeting_segments_tx(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    device_id=device_id,
                    meeting_id=None,
                    capture_operation_key=capture_operation_key,
                )
                if existing:
                    fingerprints = {str(row[5] or "") for row in existing}
                    if fingerprints != {request_fingerprint}:
                        raise CaptureIdempotencyConflict(
                            "capture operation key already has a different fingerprint"
                        )
                    await conn.commit()
                    return CaptureMeetingAppendResult(
                        accepted=True,
                        inserted=False,
                        segments=[_transcript_segment_from_capture_row(row) for row in existing],
                    )

                active_cur = await conn.execute(
                    "SELECT 1 FROM meetings WHERE id = ? AND tenant_id = ? "
                    "AND owner_id = ? AND state = 'in_meeting'",
                    (meeting_id, tenant_id, owner_id),
                )
                active = await active_cur.fetchone()
                await active_cur.close()
                if active is None:
                    await conn.commit()
                    return CaptureMeetingAppendResult(
                        accepted=False,
                        inserted=False,
                        segments=[],
                    )

                await conn.executemany(
                    """INSERT INTO meeting_segments
                       (meeting_id, text, start_ms, end_ms, speaker_id, speaker_label,
                        captured_at, tenant_id, device_id, owner_id,
                        capture_operation_key, capture_segment_ordinal,
                        capture_request_fingerprint)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            meeting_id,
                            segment.text,
                            segment.start_ms,
                            segment.end_ms,
                            segment.speaker_id,
                            segment.speaker_label,
                            _to_iso(captured_at),
                            tenant_id,
                            device_id,
                            owner_id,
                            capture_operation_key,
                            ordinal,
                            request_fingerprint,
                        )
                        for ordinal, segment in enumerate(segments)
                    ],
                )
                canonical = await self._load_capture_meeting_segments_tx(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    device_id=device_id,
                    meeting_id=None,
                    capture_operation_key=capture_operation_key,
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return CaptureMeetingAppendResult(
            accepted=True,
            inserted=True,
            segments=[_transcript_segment_from_capture_row(row) for row in canonical],
        )

    async def list_capture_meeting_segments(
        self,
        meeting_id: str,
        *,
        capture_operation_key: str,
    ) -> list[TranscriptSegment]:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            rows = await self._load_capture_meeting_segments_tx(
                self._require_conn(),
                tenant_id=tenant_id,
                owner_id=owner_id,
                device_id=device_id,
                meeting_id=meeting_id,
                capture_operation_key=capture_operation_key,
            )
        return [_transcript_segment_from_capture_row(row) for row in rows]

    async def get_capture_meeting_id(self, *, capture_operation_key: str) -> str | None:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            rows = await self._load_capture_meeting_segments_tx(
                self._require_conn(),
                tenant_id=tenant_id,
                owner_id=owner_id,
                device_id=device_id,
                meeting_id=None,
                capture_operation_key=capture_operation_key,
            )
        meeting_ids = {str(row[6]) for row in rows}
        if len(meeting_ids) > 1:
            raise CaptureIdempotencyConflict(
                "capture operation is associated with multiple canonical meetings"
            )
        return next(iter(meeting_ids), None)

    async def update_capture_meeting_speaker(
        self, *, capture_operation_key: str, request_fingerprint: str,
        speaker_id: str | None, speaker_label: str,
    ) -> list[TranscriptSegment]:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    "UPDATE meeting_segments SET speaker_id = ?, speaker_label = ? "
                    "WHERE tenant_id = ? AND owner_id = ? AND device_id = ? "
                    "AND capture_operation_key = ? AND capture_request_fingerprint = ?",
                    (speaker_id, speaker_label, tenant_id, owner_id, device_id,
                     capture_operation_key, request_fingerprint),
                )
                rows = await self._load_capture_meeting_segments_tx(
                    conn, tenant_id=tenant_id, owner_id=owner_id, device_id=device_id,
                    meeting_id=None, capture_operation_key=capture_operation_key,
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return [_transcript_segment_from_capture_row(row) for row in rows]

    @staticmethod
    async def _load_capture_meeting_segments_tx(
        conn: aiosqlite.Connection,
        *,
        tenant_id: str,
        owner_id: str,
        device_id: str,
        meeting_id: str | None,
        capture_operation_key: str,
    ) -> list[aiosqlite.Row | tuple[Any, ...]]:
        meeting_clause = " AND meeting_id = ?" if meeting_id is not None else ""
        parameters: list[object] = [tenant_id, owner_id, device_id, capture_operation_key]
        if meeting_id is not None:
            parameters.append(meeting_id)
        cur = await conn.execute(
            """SELECT text, start_ms, end_ms, speaker_id, speaker_label,
                      capture_request_fingerprint, meeting_id, capture_operation_key
               FROM meeting_segments
               WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                 AND capture_operation_key = ?"""
            + meeting_clause
            + " ORDER BY capture_segment_ordinal ASC, id ASC",
            parameters,
        )
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def snapshot_meeting_segments_for_finalize(
        self,
        meeting_id: str,
        *,
        ended_at: datetime,
    ) -> list[TranscriptSegment]:
        """Atomically close the append gate and read the complete transcript.

        SQLite's write reservation establishes a stable ordering with every
        repository instance: an append committed before this transaction is
        selected; an append that arrives afterwards sees ``state='ended'`` and
        is rejected.  Retries against ended/finalized meetings simply read the
        same authoritative segment set.
        """
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                exists_cur = await conn.execute(
                    "SELECT 1 FROM meetings WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                    (meeting_id, tenant_id, owner_id),
                )
                exists = await exists_cur.fetchone()
                await exists_cur.close()
                if exists is None:
                    raise LookupError(f"meeting {meeting_id} not found")
                await conn.execute(
                    "UPDATE meetings SET state = 'ended', ended_at = COALESCE(ended_at, ?) "
                    "WHERE id = ? AND tenant_id = ? AND owner_id = ? "
                    "AND state = 'in_meeting'",
                    (_to_iso(ended_at), meeting_id, tenant_id, owner_id),
                )
                cur = await conn.execute(
                    "SELECT text, start_ms, end_ms, speaker_id, speaker_label, capture_operation_key "
                    "FROM meeting_segments WHERE meeting_id = ? "
                    "AND tenant_id = ? AND owner_id = ? ORDER BY id ASC",
                    (meeting_id, tenant_id, owner_id),
                )
                rows = list(await cur.fetchall())
                await cur.close()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return [
            TranscriptSegment(
                text=row[0],
                start_ms=row[1],
                end_ms=row[2],
                speaker_id=row[3],
                speaker_label=row[4],
                capture_correlation=_capture_correlation(row[5]),
            )
            for row in rows
        ]

    async def list_meeting_segments(
        self,
        meeting_id: str,
    ) -> list[TranscriptSegment]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT text, start_ms, end_ms, speaker_id, speaker_label, capture_operation_key "
                "FROM meeting_segments WHERE meeting_id = ? "
                "AND tenant_id = ? AND owner_id = ? ORDER BY id ASC",
                (meeting_id, tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            TranscriptSegment(
                text=r[0],
                start_ms=r[1],
                end_ms=r[2],
                speaker_id=r[3],
                speaker_label=r[4],
                capture_correlation=_capture_correlation(r[5]),
            )
            for r in rows
        ]

    async def count_meeting_segments(self, meeting_id: str) -> int:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM meeting_segments WHERE meeting_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (meeting_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    async def count_meeting_speakers(self, meeting_id: str) -> int:
        """该会议出现过的不同 speaker_id 数（NULL 不计）。

        优先 distinct meeting_segments.speaker_id；兼容只填 speaker_label 的旧
        数据，再 fallback 到 distinct speaker_label。
        """
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT COUNT(DISTINCT speaker_id) FROM meeting_segments "
                "WHERE meeting_id = ? AND tenant_id = ? AND owner_id = ? "
                "AND speaker_id IS NOT NULL",
                (meeting_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            n_id = int(row[0]) if row else 0
            await cur.close()
            if n_id > 0:
                return n_id
            cur = await conn.execute(
                "SELECT COUNT(DISTINCT speaker_label) FROM meeting_segments "
                "WHERE meeting_id = ? AND tenant_id = ? AND owner_id = ? "
                "AND speaker_label IS NOT NULL",
                (meeting_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    # ── per-meeting speaker label map ───────────────────────────
    async def upsert_meeting_speaker_label(
        self,
        meeting_id: str,
        speaker_id: str,
        label: str,
    ) -> None:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                "INSERT INTO meeting_speaker_labels "
                "(meeting_id, speaker_id, label, tenant_id, device_id, owner_id) "
                "SELECT ?, ?, ?, ?, ?, ? "
                "WHERE EXISTS (SELECT 1 FROM meetings "
                "WHERE id = ? AND tenant_id = ? AND owner_id = ?) "
                "ON CONFLICT(tenant_id, owner_id, meeting_id, speaker_id) "
                "DO UPDATE SET label = excluded.label, device_id = excluded.device_id",
                (
                    meeting_id,
                    speaker_id,
                    label,
                    tenant_id,
                    device_id,
                    owner_id,
                    meeting_id,
                    tenant_id,
                    owner_id,
                ),
            )
            await conn.commit()

    async def get_meeting_speaker_labels(
        self,
        meeting_id: str,
    ) -> dict[str, str]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT speaker_id, label FROM meeting_speaker_labels WHERE meeting_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (meeting_id, tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return {r[0]: r[1] for r in rows}

    # ── Ambient segments ────────────────────────────────────────
    async def append_ambient_segment(
        self,
        *,
        audio_ref: str,
        text: str,
        captured_at: datetime,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        duration_ms: int = 0,
        client_segment_id: str | None = None,
    ) -> int:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "INSERT INTO ambient_segments "
                "(audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at, "
                "tenant_id, device_id, owner_id, client_segment_id, rag_projection_state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'index_pending')",
                (
                    audio_ref,
                    text,
                    speaker_id,
                    speaker_label,
                    duration_ms,
                    _to_iso(captured_at),
                    tenant_id,
                    device_id,
                    owner_id,
                    client_segment_id,
                ),
            )
            row_id = cur.lastrowid
            await conn.commit()
            await cur.close()
        return int(row_id or 0)

    async def append_capture_ambient_segment(
        self,
        *,
        audio_ref: str,
        text: str,
        captured_at: datetime,
        capture_operation_key: str,
        request_fingerprint: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        duration_ms: int = 0,
        client_segment_id: str | None = None,
    ) -> CaptureAmbientAppendResult:
        """按作用域 operation hash 原子插入或返回 ambient 规范行。"""

        if not capture_operation_key or not request_fingerprint:
            raise ValueError("capture operation key and request fingerprint are required")
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing_cur = await conn.execute(
                    """SELECT id, capture_request_fingerprint
                       FROM ambient_segments
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                         AND capture_operation_key = ?""",
                    (tenant_id, owner_id, device_id, capture_operation_key),
                )
                existing = await existing_cur.fetchone()
                await existing_cur.close()
                if existing is not None:
                    if str(existing[1] or "") != request_fingerprint:
                        raise CaptureIdempotencyConflict(
                            "capture operation key already has a different fingerprint"
                        )
                    await conn.commit()
                    return CaptureAmbientAppendResult(
                        segment_id=int(existing[0]),
                        inserted=False,
                    )

                cur = await conn.execute(
                    """INSERT INTO ambient_segments
                       (audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at,
                        tenant_id, device_id, owner_id, client_segment_id,
                        rag_projection_state, capture_operation_key,
                        capture_request_fingerprint)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'index_pending', ?, ?)""",
                    (
                        audio_ref,
                        text,
                        speaker_id,
                        speaker_label,
                        duration_ms,
                        _to_iso(captured_at),
                        tenant_id,
                        device_id,
                        owner_id,
                        client_segment_id,
                        capture_operation_key,
                        request_fingerprint,
                    ),
                )
                row_id = int(cur.lastrowid or 0)
                await cur.close()
                if row_id <= 0:
                    raise RuntimeError("capture ambient insert returned no canonical id")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return CaptureAmbientAppendResult(segment_id=row_id, inserted=True)

    async def update_ambient_segment_speaker(
        self, segment_id: int, *, speaker_id: str | None, speaker_label: str
    ) -> bool:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "UPDATE ambient_segments SET speaker_id = ?, speaker_label = ? "
                "WHERE id = ? AND tenant_id = ? AND owner_id = ? AND device_id = ?",
                (speaker_id, speaker_label, segment_id, tenant_id, owner_id, device_id),
            )
            changed = cur.rowcount == 1
            await cur.close()
            await conn.commit()
        return changed

    async def get_ambient_segment(self, segment_id: int) -> AmbientSegmentRecord | None:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            cur = await self._require_conn().execute(
                """SELECT id, audio_ref, text, speaker_id, speaker_label, duration_ms,
                          captured_at, device_id, client_segment_id,
                          rag_projection_state, rag_projection_error, rag_projected_at,
                          rag_projection_attempts, rag_projection_next_retry_at
                   FROM ambient_segments
                   WHERE id = ? AND tenant_id = ? AND owner_id = ? AND device_id = ?""",
                (segment_id, tenant_id, owner_id, device_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _ambient_segment_from_row(row) if row is not None else None

    async def list_ambient_segments(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        current_device_only: bool = False,
        limit: int = 100,
    ) -> list[AmbientSegmentRecord]:
        tenant_id, device_id, owner_id = _scope()
        clauses: list[str] = ["tenant_id = ?", "owner_id = ?"]
        args: list[object] = [tenant_id, owner_id]
        if current_device_only:
            clauses.append("device_id = ?")
            args.append(device_id)
        if since is not None:
            clauses.append("captured_at >= ?")
            args.append(_to_iso(since))
        if until is not None:
            clauses.append("captured_at <= ?")
            args.append(_to_iso(until))
        where = "WHERE " + " AND ".join(clauses)
        args.append(limit)
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT id, audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at, device_id, client_segment_id, "
                "rag_projection_state, rag_projection_error, rag_projected_at, "
                "rag_projection_attempts, rag_projection_next_retry_at, "
                "capture_operation_key, capture_request_fingerprint "
                f"FROM ambient_segments {where} ORDER BY captured_at DESC LIMIT ?",
                args,
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            AmbientSegmentRecord(
                id=r[0],
                audio_ref=r[1],
                text=r[2],
                speaker_id=r[3],
                speaker_label=r[4],
                duration_ms=r[5],
                captured_at=_from_iso(r[6]) or datetime.fromtimestamp(0, UTC),
                device_id=r[7],
                client_segment_id=r[8],
                rag_projection_state=r[9],
                rag_projection_error=r[10],
                rag_projected_at=_from_iso(r[11]),
                rag_projection_attempts=int(r[12]),
                rag_projection_next_retry_at=_from_iso(r[13]),
                capture_operation_key=r[14],
                capture_request_fingerprint=r[15],
            )
            for r in rows
        ]

    async def import_ambient_segments_to_meeting(
        self,
        meeting_id: str,
        *,
        ambient_segment_ids: list[int],
        meeting_started_at: datetime,
    ) -> AmbientMeetingImportResult:
        """Copy selected current-device ambient rows into one active meeting.

        ``source_ambient_segment_id`` is the durable provenance/dedupe key for
        legacy rows. Modern capture rows also retain their operation key, so
        the triggering chunk and its later live overlay resolve to one
        canonical meeting segment instead of being appended twice.
        """

        source_ids = sorted({int(item) for item in ambient_segment_ids if int(item) > 0})
        if not source_ids:
            return AmbientMeetingImportResult(accepted=True)
        tenant_id, device_id, owner_id = _scope()
        started_at = meeting_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        placeholders = ",".join("?" for _ in source_ids)
        inserted: list[TranscriptSegment] = []
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                active_cur = await conn.execute(
                    "SELECT 1 FROM meetings WHERE id = ? AND tenant_id = ? "
                    "AND owner_id = ? AND state = 'in_meeting'",
                    (meeting_id, tenant_id, owner_id),
                )
                active = await active_cur.fetchone()
                await active_cur.close()
                if active is None:
                    await conn.rollback()
                    return AmbientMeetingImportResult(accepted=False)
                cur = await conn.execute(
                    "SELECT id, text, speaker_id, speaker_label, duration_ms, captured_at, "
                    "capture_operation_key, capture_request_fingerprint "
                    "FROM ambient_segments WHERE tenant_id = ? AND owner_id = ? "
                    "AND device_id = ? AND id IN ("
                    + placeholders
                    + ") ORDER BY captured_at ASC, id ASC",
                    (tenant_id, owner_id, device_id, *source_ids),
                )
                rows = list(await cur.fetchall())
                await cur.close()
                for row in rows:
                    text = str(row[1] or "").strip()
                    captured_at = _from_iso(str(row[5]))
                    if not text or captured_at is None:
                        continue
                    if captured_at.tzinfo is None:
                        captured_at = captured_at.replace(tzinfo=UTC)
                    duration_ms = max(0, int(row[4] or 0))
                    chunk_started_at = captured_at - timedelta(milliseconds=duration_ms)
                    start_ms = max(
                        0,
                        round((chunk_started_at - started_at).total_seconds() * 1000),
                    )
                    end_ms = max(
                        start_ms,
                        round((captured_at - started_at).total_seconds() * 1000),
                    )
                    operation_key = str(row[6]) if row[6] else None
                    fingerprint = str(row[7]) if row[7] else None
                    changed = await conn.execute(
                        """INSERT OR IGNORE INTO meeting_segments
                           (meeting_id, text, start_ms, end_ms, speaker_id, speaker_label,
                            captured_at, tenant_id, device_id, owner_id,
                            capture_operation_key, capture_segment_ordinal,
                            capture_request_fingerprint, source_ambient_segment_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            meeting_id,
                            text,
                            start_ms,
                            end_ms,
                            row[2],
                            row[3],
                            _to_iso(captured_at),
                            tenant_id,
                            device_id,
                            owner_id,
                            operation_key,
                            0 if operation_key else None,
                            fingerprint,
                            int(row[0]),
                        ),
                    )
                    was_inserted = changed.rowcount == 1
                    await changed.close()
                    if was_inserted:
                        inserted.append(
                            TranscriptSegment(
                                text=text,
                                start_ms=start_ms,
                                end_ms=end_ms,
                                speaker_id=row[2],
                                speaker_label=row[3],
                                capture_correlation=_capture_correlation(operation_key),
                            )
                        )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        async with self._lock:
            cur = await self._require_conn().execute(
                "SELECT text, start_ms, end_ms, speaker_id, speaker_label, "
                "capture_operation_key FROM meeting_segments "
                "WHERE meeting_id = ? AND tenant_id = ? AND owner_id = ? "
                "AND device_id = ? AND source_ambient_segment_id IN ("
                + placeholders
                + ") ORDER BY start_ms ASC, id ASC",
                (meeting_id, tenant_id, owner_id, device_id, *source_ids),
            )
            canonical_rows = list(await cur.fetchall())
            await cur.close()
        return AmbientMeetingImportResult(
            accepted=True,
            inserted_count=len(inserted),
            segments=[
                TranscriptSegment(
                    text=row[0],
                    start_ms=row[1],
                    end_ms=row[2],
                    speaker_id=row[3],
                    speaker_label=row[4],
                    capture_correlation=_capture_correlation(row[5]),
                )
                for row in canonical_rows
            ],
            inserted_segments=inserted,
        )

    async def set_ambient_rag_projection(
        self,
        segment_id: int,
        *,
        state: RagProjectionState,
        error: str | None = None,
        projected_at: datetime | None = None,
        retry_backoff: bool = False,
    ) -> bool:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                attempts = 0
                next_retry_at: datetime | None = None
                if retry_backoff and state == "index_failed":
                    cur = await conn.execute(
                        "SELECT rag_projection_attempts FROM ambient_segments "
                        "WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                        (segment_id, tenant_id, owner_id),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    attempts = int(row[0] if row else 0) + 1
                    next_retry_at = _rag_projection_retry_at(attempts)
                allowed_source_states = _AMBIENT_RAG_PROJECTION_SOURCE_STATES.get(state)
                where_state = ""
                if allowed_source_states:
                    placeholders = ", ".join("?" for _ in allowed_source_states)
                    where_state = f" AND rag_projection_state IN ({placeholders})"
                values: list[object] = [
                    state,
                    (error or "")[:500] or None,
                    _to_iso(projected_at) if projected_at is not None else None,
                    attempts,
                    _to_iso(next_retry_at) if next_retry_at is not None else None,
                    segment_id,
                    tenant_id,
                    owner_id,
                ]
                if allowed_source_states:
                    values.extend(allowed_source_states)
                changed = await conn.execute(
                    """UPDATE ambient_segments
                       SET rag_projection_state = ?, rag_projection_error = ?,
                           rag_projected_at = ?, rag_projection_attempts = ?,
                           rag_projection_next_retry_at = ?
                       WHERE id = ? AND tenant_id = ? AND owner_id = ?"""
                    + where_state,
                    values,
                )
                updated = changed.rowcount == 1
                await changed.close()
                await conn.commit()
                return updated
            except BaseException:
                await conn.rollback()
                raise

    async def list_ambient_segments_needing_rag_projection(
        self,
        *,
        limit: int = 100,
    ) -> list[AmbientSegmentRecord]:
        tenant_id, _device_id, owner_id = _scope()
        now = _to_iso(datetime.now(UTC))
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT id, audio_ref, text, speaker_id, speaker_label, duration_ms,
                          captured_at, rag_projection_state, rag_projection_error,
                          rag_projected_at, rag_projection_attempts,
                          rag_projection_next_retry_at
                   FROM ambient_segments
                   WHERE tenant_id = ? AND owner_id = ?
                     AND rag_projection_state IN (
                         'reconcile_pending', 'index_pending', 'index_failed'
                     )
                     AND (
                         rag_projection_next_retry_at IS NULL
                         OR rag_projection_next_retry_at <= ?
                     )
                   ORDER BY COALESCE(rag_projection_next_retry_at, ''), captured_at, id
                   LIMIT ?""",
                (tenant_id, owner_id, now, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            AmbientSegmentRecord(
                id=row[0],
                audio_ref=row[1],
                text=row[2],
                speaker_id=row[3],
                speaker_label=row[4],
                duration_ms=row[5],
                captured_at=_from_iso(row[6]) or datetime.fromtimestamp(0, UTC),
                rag_projection_state=row[7],
                rag_projection_error=row[8],
                rag_projected_at=_from_iso(row[9]),
                rag_projection_attempts=int(row[10]),
                rag_projection_next_retry_at=_from_iso(row[11]),
            )
            for row in rows
        ]

    async def count_ambient_segments(self) -> int:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM ambient_segments WHERE tenant_id = ? AND owner_id = ?",
                (tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    async def register_ambient_audio_file(
        self,
        *,
        audio_ref: str,
        size_bytes: int,
        captured_at: datetime,
        quota_charged: bool,
    ) -> None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            try:
                await conn.execute(
                    """INSERT INTO ambient_audio_files
                       (tenant_id, owner_id, audio_ref, size_bytes, captured_at, quota_charged)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tenant_id, owner_id, audio_ref) DO UPDATE SET
                           size_bytes = excluded.size_bytes,
                           captured_at = excluded.captured_at,
                           quota_charged = excluded.quota_charged""",
                    (
                        tenant_id,
                        owner_id,
                        audio_ref,
                        max(0, size_bytes),
                        _to_iso(captured_at),
                        1 if quota_charged else 0,
                    ),
                )
                await conn.commit()
            except BaseException:
                # 请求取消也不能在进程级长连接上遗留未完成写事务。
                with contextlib.suppress(BaseException):
                    await rollback_aiosqlite_connection(conn)
                raise

    async def list_ambient_audio_files(self) -> list[AmbientAudioFileRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT audio_ref, size_bytes, captured_at, quota_charged
                   FROM ambient_audio_files
                   WHERE tenant_id = ? AND owner_id = ?
                   ORDER BY captured_at ASC, audio_ref ASC""",
                (tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            AmbientAudioFileRecord(
                audio_ref=str(row[0]),
                size_bytes=int(row[1]),
                captured_at=_from_iso(str(row[2])) or datetime.fromtimestamp(0, UTC),
                quota_charged=bool(row[3]),
            )
            for row in rows
        ]

    async def delete_ambient_audio_file(
        self,
        audio_ref: str,
    ) -> AmbientAudioFileRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT audio_ref, size_bytes, captured_at, quota_charged
                   FROM ambient_audio_files
                   WHERE tenant_id = ? AND owner_id = ? AND audio_ref = ?""",
                (tenant_id, owner_id, audio_ref),
            )
            row = await cur.fetchone()
            await cur.close()
            await conn.execute(
                """DELETE FROM ambient_audio_files
                   WHERE tenant_id = ? AND owner_id = ? AND audio_ref = ?""",
                (tenant_id, owner_id, audio_ref),
            )
            await conn.execute(
                """UPDATE ambient_segments SET audio_ref = ''
                   WHERE tenant_id = ? AND owner_id = ? AND audio_ref = ?""",
                (tenant_id, owner_id, audio_ref),
            )
            await conn.commit()
        if row is None:
            return None
        return AmbientAudioFileRecord(
            audio_ref=str(row[0]),
            size_bytes=int(row[1]),
            captured_at=_from_iso(str(row[2])) or datetime.fromtimestamp(0, UTC),
            quota_charged=bool(row[3]),
        )

    # ── Capture durable idempotency ─────────────────────────────
    async def claim_capture_request(
        self,
        *,
        operation_key_hash: str,
        client_segment_hash: str,
        idempotency_key_hash: str | None,
        request_fingerprint: str,
        holder_id: str,
        lease_seconds: float,
        meeting_id: str | None = None,
        capture_mode: str | None = None,
    ) -> CaptureRequestClaim:
        """原子 claim、完成重放或过期 takeover；正文永不进入 receipt。"""

        self._validate_capture_claim_inputs(
            operation_key_hash=operation_key_hash,
            client_segment_hash=client_segment_hash,
            idempotency_key_hash=idempotency_key_hash,
            request_fingerprint=request_fingerprint,
            holder_id=holder_id,
            lease_seconds=lease_seconds,
        )
        tenant_id, device_id, owner_id = _scope()
        now = time.time()
        expires_at = now + lease_seconds
        now_iso = _to_iso(datetime.now(UTC))
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    f"""SELECT {_CAPTURE_RECEIPT_COLUMNS}
                        FROM capture_request_receipts
                        WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                          AND (
                              operation_key_hash = ? OR client_segment_hash = ?
                              OR (? IS NOT NULL AND idempotency_key_hash = ?)
                          )""",
                    (
                        tenant_id,
                        owner_id,
                        device_id,
                        operation_key_hash,
                        client_segment_hash,
                        idempotency_key_hash,
                        idempotency_key_hash,
                    ),
                )
                matching_rows = list(await cur.fetchall())
                await cur.close()
                if len(matching_rows) > 1:
                    raise CaptureIdempotencyConflict(
                        "capture identity aliases resolve to different receipts"
                    )
                if not matching_rows:
                    cur = await conn.execute(
                        """INSERT INTO capture_request_receipts
                           (tenant_id, owner_id, device_id, operation_key_hash,
                            client_segment_hash, idempotency_key_hash,
                            request_fingerprint, state, lease_holder, lease_fence,
                            lease_expires_at, meeting_id, capture_mode,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, 1, ?, ?, ?, ?, ?)
                           RETURNING """
                        + _CAPTURE_RECEIPT_COLUMNS,
                        (
                            tenant_id,
                            owner_id,
                            device_id,
                            operation_key_hash,
                            client_segment_hash,
                            idempotency_key_hash,
                            request_fingerprint,
                            holder_id,
                            expires_at,
                            meeting_id,
                            capture_mode,
                            now_iso,
                            now_iso,
                        ),
                    )
                    inserted_row = await cur.fetchone()
                    await cur.close()
                    await conn.commit()
                    assert inserted_row is not None
                    return _capture_claim_from_row(inserted_row, status="acquired")

                existing_row = matching_rows[0]
                if str(existing_row[1]) != request_fingerprint:
                    raise CaptureIdempotencyConflict(
                        "capture identity key already has a different request fingerprint"
                    )
                if str(existing_row[2]) == "completed":
                    await conn.commit()
                    return _capture_claim_from_row(existing_row, status="completed")

                # Bind the meeting before ASR starts. Older receipts may have
                # been created before this fence existed; upgrading only the
                # still-processing row is safe and keeps the receipt body-free.
                if (meeting_id and existing_row[8] is None) or (
                    capture_mode and existing_row[10] is None
                ):
                    await conn.execute(
                        """UPDATE capture_request_receipts
                           SET meeting_id = COALESCE(meeting_id, ?),
                               capture_mode = COALESCE(capture_mode, ?),
                               updated_at = ?
                           WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                             AND operation_key_hash = ? AND state = 'processing'""",
                        (
                            meeting_id,
                            capture_mode,
                            now_iso,
                            tenant_id,
                            owner_id,
                            device_id,
                            str(existing_row[0]),
                        ),
                    )

                current_holder = str(existing_row[3])
                current_expires_at = float(existing_row[5])
                if current_holder == holder_id or current_expires_at <= now:
                    cur = await conn.execute(
                        """UPDATE capture_request_receipts
                           SET lease_holder = ?,
                               lease_fence = CASE
                                   WHEN lease_holder = ? AND lease_expires_at > ?
                                   THEN lease_fence
                                   ELSE lease_fence + 1
                               END,
                               lease_expires_at = ?, updated_at = ?
                           WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                             AND operation_key_hash = ? AND state = 'processing'
                             AND lease_holder = ? AND lease_fence = ?
                             AND lease_expires_at = ?
                             AND (lease_holder = ? OR lease_expires_at <= ?)
                           RETURNING """
                        + _CAPTURE_RECEIPT_COLUMNS,
                        (
                            holder_id,
                            holder_id,
                            now,
                            expires_at,
                            now_iso,
                            tenant_id,
                            owner_id,
                            device_id,
                            str(existing_row[0]),
                            current_holder,
                            int(existing_row[4]),
                            current_expires_at,
                            holder_id,
                            now,
                        ),
                    )
                    claimed = await cur.fetchone()
                    await cur.close()
                    if claimed is None:
                        raise CaptureClaimLost("capture claim changed during takeover")
                    await conn.commit()
                    return _capture_claim_from_row(claimed, status="acquired")

                await conn.commit()
                return _capture_claim_from_row(existing_row, status="in_progress")
            except BaseException:
                await conn.rollback()
                raise

    async def settle_capture_requests(
        self,
        meeting_id: str,
        *,
        timeout_s: float,
    ) -> CaptureReceiptSettlement:
        """Wait for meeting-bound receipts without closing meeting_segments first.

        The event makes the common same-process path prompt; the short bounded
        recheck also observes completions made by another repository instance.
        """

        if not meeting_id:
            return CaptureReceiptSettlement()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("capture settle timeout must be finite and positive")
        tenant_id, device_id, owner_id = _scope()
        started = time.monotonic()
        deadline = started + timeout_s

        async def pending_count() -> int:
            async with self._lock:
                cur = await self._require_conn().execute(
                    """SELECT COUNT(*) FROM capture_request_receipts
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                         AND state = 'processing'
                         AND meeting_id = ?""",
                    (tenant_id, owner_id, device_id, meeting_id),
                )
                row = await cur.fetchone()
                await cur.close()
            return int(row[0]) if row is not None else 0

        self._capture_receipt_changed.clear()
        initial_pending = await pending_count()
        remaining = initial_pending
        while remaining > 0:
            if time.monotonic() >= deadline:
                break
            self._capture_receipt_changed.clear()
            remaining = await pending_count()
            if remaining == 0:
                break
            wait_s = min(0.25, max(0.0, deadline - time.monotonic()))
            if wait_s <= 0:
                break
            try:
                await asyncio.wait_for(self._capture_receipt_changed.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass

        waited_ms = (time.monotonic() - started) * 1000
        return CaptureReceiptSettlement(
            initial_pending=initial_pending,
            remaining_pending=remaining,
            waited_ms=waited_ms,
            timed_out=remaining > 0,
        )

    async def renew_capture_request_claim(
        self,
        claim: CaptureRequestClaim,
        *,
        lease_seconds: float,
    ) -> CaptureRequestClaim | None:
        if claim.status != "acquired" or not claim.lease_holder:
            raise ValueError("only an acquired capture claim can be renewed")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        tenant_id, device_id, owner_id = _scope()
        now = time.time()
        now_iso = _to_iso(datetime.now(UTC))
        async with self._lock:
            cur = await self._require_conn().execute(
                """UPDATE capture_request_receipts
                   SET lease_expires_at = ?, updated_at = ?
                   WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                     AND operation_key_hash = ? AND request_fingerprint = ?
                     AND state = 'processing' AND lease_holder = ? AND lease_fence = ?
                     AND lease_expires_at > ?
                   RETURNING """
                + _CAPTURE_RECEIPT_COLUMNS,
                (
                    now + lease_seconds,
                    now_iso,
                    tenant_id,
                    owner_id,
                    device_id,
                    claim.operation_key_hash,
                    claim.request_fingerprint,
                    claim.lease_holder,
                    claim.lease_fence,
                    now,
                ),
            )
            row = await cur.fetchone()
            await cur.close()
            await self._require_conn().commit()
        return _capture_claim_from_row(row, status="acquired") if row is not None else None

    async def release_capture_request_claim(self, claim: CaptureRequestClaim) -> bool:
        if claim.status != "acquired" or not claim.lease_holder:
            return False
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """UPDATE capture_request_receipts
                   SET lease_expires_at = 0, updated_at = ?
                   WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                     AND operation_key_hash = ? AND request_fingerprint = ?
                     AND state = 'processing' AND lease_holder = ? AND lease_fence = ?""",
                (
                    _to_iso(datetime.now(UTC)),
                    tenant_id,
                    owner_id,
                    device_id,
                    claim.operation_key_hash,
                    claim.request_fingerprint,
                    claim.lease_holder,
                    claim.lease_fence,
                ),
            )
            released = cur.rowcount == 1
            await cur.close()
            await conn.commit()
        return released

    async def complete_capture_request(
        self,
        claim: CaptureRequestClaim,
        *,
        ambient_stored: bool,
        ambient_segment_id: int | None,
        meeting_id: str | None,
        stt_status: str,
        capture_mode: str,
    ) -> CaptureRequestClaim:
        """在仍持有 fence 时提交非内容结果元数据。"""

        if claim.status != "acquired" or not claim.lease_holder:
            raise CaptureClaimLost("capture claim is not owned")
        if not stt_status or not capture_mode:
            raise ValueError("capture completion status and mode are required")
        tenant_id, device_id, owner_id = _scope()
        now_iso = _to_iso(datetime.now(UTC))
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """SELECT id FROM ambient_segments
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                         AND capture_operation_key = ?
                         AND capture_request_fingerprint = ?""",
                    (
                        tenant_id,
                        owner_id,
                        device_id,
                        claim.operation_key_hash,
                        claim.request_fingerprint,
                    ),
                )
                canonical_ambient_rows = list(await cur.fetchall())
                await cur.close()
                if len(canonical_ambient_rows) > 1:
                    raise CaptureIdempotencyConflict(
                        "capture operation has multiple canonical ambient rows"
                    )
                canonical_ambient_id = (
                    int(canonical_ambient_rows[0][0]) if canonical_ambient_rows else None
                )
                if canonical_ambient_id != ambient_segment_id:
                    raise CaptureClaimLost(
                        "capture completion does not reference its canonical ambient row"
                    )

                cur = await conn.execute(
                    """SELECT DISTINCT meeting_id FROM meeting_segments
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                         AND capture_operation_key = ?
                         AND capture_request_fingerprint = ?""",
                    (
                        tenant_id,
                        owner_id,
                        device_id,
                        claim.operation_key_hash,
                        claim.request_fingerprint,
                    ),
                )
                canonical_meeting_rows = list(await cur.fetchall())
                await cur.close()
                if len(canonical_meeting_rows) > 1:
                    raise CaptureIdempotencyConflict(
                        "capture operation has canonical rows in multiple meetings"
                    )
                if canonical_meeting_rows and str(canonical_meeting_rows[0][0]) != meeting_id:
                    raise CaptureClaimLost(
                        "capture completion does not reference its canonical meeting"
                    )
                cur = await conn.execute(
                    """UPDATE capture_request_receipts
                       SET state = 'completed', lease_expires_at = 0,
                           ambient_stored = ?, ambient_segment_id = ?, meeting_id = ?,
                           stt_status = ?, capture_mode = ?, updated_at = ?, completed_at = ?
                       WHERE tenant_id = ? AND owner_id = ? AND device_id = ?
                         AND operation_key_hash = ? AND request_fingerprint = ?
                         AND state = 'processing' AND lease_holder = ? AND lease_fence = ?
                       RETURNING """
                    + _CAPTURE_RECEIPT_COLUMNS,
                    (
                        1 if ambient_stored else 0,
                        ambient_segment_id,
                        meeting_id,
                        stt_status,
                        capture_mode,
                        now_iso,
                        now_iso,
                        tenant_id,
                        owner_id,
                        device_id,
                        claim.operation_key_hash,
                        claim.request_fingerprint,
                        claim.lease_holder,
                        claim.lease_fence,
                    ),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise CaptureClaimLost("capture claim expired or was superseded")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        self._capture_receipt_changed.set()
        return _capture_claim_from_row(row, status="completed")

    @staticmethod
    def _validate_capture_claim_inputs(
        *,
        operation_key_hash: str,
        client_segment_hash: str,
        idempotency_key_hash: str | None,
        request_fingerprint: str,
        holder_id: str,
        lease_seconds: float,
    ) -> None:
        hashes = [operation_key_hash, client_segment_hash, request_fingerprint]
        if idempotency_key_hash is not None:
            hashes.append(idempotency_key_hash)
        if any(len(value) != 64 for value in hashes):
            raise ValueError("capture identity hashes must contain 64 hex characters")
        if any(character not in "0123456789abcdef" for value in hashes for character in value):
            raise ValueError("capture identity hashes must be lowercase hexadecimal")
        if not holder_id or len(holder_id) > 128:
            raise ValueError("capture claim holder is invalid")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")

    # ── Global speakers registry ────────────────────────────────
    async def upsert_speaker(
        self,
        speaker_id: str,
        *,
        captured_at: datetime,
        label: str | None = None,
        embedding_blob: bytes | None = None,
    ) -> None:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT first_seen_at, n_samples FROM speakers WHERE speaker_id = ? "
                "AND tenant_id = ? AND owner_id = ?",
                (speaker_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                await conn.execute(
                    "INSERT INTO speakers "
                    "(speaker_id, label, n_samples, first_seen_at, last_seen_at, embedding_blob, "
                    "tenant_id, device_id, owner_id) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (
                        speaker_id,
                        label,
                        _to_iso(captured_at),
                        _to_iso(captured_at),
                        embedding_blob,
                        tenant_id,
                        device_id,
                        owner_id,
                    ),
                )
            else:
                sets = ["last_seen_at = ?", "n_samples = n_samples + 1"]
                vals: list[object] = [_to_iso(captured_at)]
                if label is not None:
                    sets.append("label = ?")
                    vals.append(label)
                if embedding_blob is not None:
                    sets.append("embedding_blob = ?")
                    vals.append(embedding_blob)
                vals.extend((speaker_id, tenant_id, owner_id))
                await conn.execute(
                    f"UPDATE speakers SET {', '.join(sets)} WHERE speaker_id = ? "
                    "AND tenant_id = ? AND owner_id = ?",
                    vals,
                )
            await conn.commit()

    async def get_speaker(self, speaker_id: str) -> SpeakerProfileRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT speaker_id, label, n_samples, first_seen_at, last_seen_at, embedding_blob "
                "FROM speakers WHERE speaker_id = ? AND tenant_id = ? AND owner_id = ?",
                (speaker_id, tenant_id, owner_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return _speaker_from_row(row) if row else None

    async def list_speakers(self) -> list[SpeakerProfileRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT speaker_id, label, n_samples, first_seen_at, last_seen_at, embedding_blob "
                "FROM speakers WHERE tenant_id = ? AND owner_id = ? "
                "ORDER BY last_seen_at DESC",
                (tenant_id, owner_id),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_speaker_from_row(r) for r in rows]


def _meeting_from_row(row: aiosqlite.Row | tuple[Any, ...]) -> MeetingRecord:
    # 长度兼容：旧 schema 9 列；migration 003 → 11 列；migration 004 → 12 列
    # migration 017 → 13 列（显式清理纪要 tombstone）；migration 026 → 16 列；
    # migration 038 → 21 列（投影退避、generation fence 与 workflow marker）。
    return MeetingRecord(
        id=row[0],
        title=row[1],
        state=row[2],
        started_at=_from_iso(row[3]) or datetime.fromtimestamp(0, UTC),
        ended_at=_from_iso(row[4]),
        finalized_at=_from_iso(row[5]),
        auto_started=bool(row[6]),
        minutes_json=row[7],
        raw_transcript_ref=row[8],
        minutes_status=row[9] if len(row) > 9 else None,
        minutes_error=row[10] if len(row) > 10 else None,
        display_title=row[11] if len(row) > 11 else None,
        minutes_cleared_at=_from_iso(row[12]) if len(row) > 12 else None,
        rag_projection_state=row[13] if len(row) > 13 else None,
        rag_projection_error=row[14] if len(row) > 14 else None,
        rag_projected_at=_from_iso(row[15]) if len(row) > 15 else None,
        rag_projection_attempts=int(row[16]) if len(row) > 16 else 0,
        rag_projection_next_retry_at=_from_iso(row[17]) if len(row) > 17 else None,
        rag_projection_generation=int(row[18]) if len(row) > 18 else 0,
        minutes_generation_run_id=row[19] if len(row) > 19 else None,
        minutes_generation_cancelled_at=_from_iso(row[20]) if len(row) > 20 else None,
    )


def _transcript_segment_from_capture_row(
    row: aiosqlite.Row | tuple[Any, ...],
) -> TranscriptSegment:
    return TranscriptSegment(
        text=row[0],
        start_ms=row[1],
        end_ms=row[2],
        speaker_id=row[3],
        speaker_label=row[4],
        capture_correlation=_capture_correlation(row[7] if len(row) > 7 else None),
    )


def _capture_correlation(capture_operation_key: object) -> str | None:
    if not isinstance(capture_operation_key, str) or not capture_operation_key:
        return None
    return f"capture-{capture_operation_key[:16]}"


def _ambient_segment_from_row(
    row: aiosqlite.Row | tuple[Any, ...],
) -> AmbientSegmentRecord:
    return AmbientSegmentRecord(
        id=int(row[0]),
        audio_ref=str(row[1]),
        text=str(row[2]),
        speaker_id=row[3],
        speaker_label=row[4],
        duration_ms=int(row[5]),
        captured_at=_from_iso(str(row[6])) or datetime.fromtimestamp(0, UTC),
        device_id=row[7],
        client_segment_id=row[8],
        rag_projection_state=row[9],
        rag_projection_error=row[10],
        rag_projected_at=_from_iso(row[11]),
        rag_projection_attempts=int(row[12]),
        rag_projection_next_retry_at=_from_iso(row[13]),
    )


def _capture_claim_from_row(
    row: aiosqlite.Row | tuple[Any, ...],
    *,
    status: CaptureClaimStatus,
) -> CaptureRequestClaim:
    return CaptureRequestClaim(
        status=status,
        operation_key_hash=str(row[0]),
        request_fingerprint=str(row[1]),
        lease_holder=str(row[3]) if row[3] is not None else None,
        lease_fence=int(row[4]),
        lease_expires_at=float(row[5]),
        ambient_stored=bool(row[6]),
        ambient_segment_id=int(row[7]) if row[7] is not None else None,
        meeting_id=str(row[8]) if row[8] is not None else None,
        stt_status=str(row[9]) if row[9] is not None else None,
        capture_mode=str(row[10]) if row[10] is not None else None,
    )


def _speaker_from_row(row: aiosqlite.Row | tuple[Any, ...]) -> SpeakerProfileRecord:
    return SpeakerProfileRecord(
        speaker_id=row[0],
        label=row[1],
        n_samples=row[2],
        first_seen_at=_from_iso(row[3]) or datetime.fromtimestamp(0, UTC),
        last_seen_at=_from_iso(row[4]) or datetime.fromtimestamp(0, UTC),
        embedding_blob=row[5],
    )


__all__ = ["SQLiteRepository"]
