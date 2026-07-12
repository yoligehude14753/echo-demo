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
- 业务校验、事件发布（留给 use_case 层）
- 大对象（音频文件本体）→ 文件系统存，DB 只存 ref
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.adapters.repo.connection import configure_aiosqlite_connection
from app.adapters.repo.migrator import run_migrations
from app.ports.repository import (
    AmbientAudioFileRecord,
    AmbientSegmentRecord,
    MeetingRecord,
    MeetingState,
    MinutesStatus,
    RagProjectionState,
    RepositoryPort,
    SpeakerProfileRecord,
)
from app.schemas.meeting import TranscriptSegment
from app.security.context import current_principal


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _scope() -> tuple[str, str, str]:
    """Return the server-validated persistence scope for the current request."""

    principal = current_principal()
    return principal.tenant_id, principal.device_id, principal.owner_id


class SQLiteRepository(RepositoryPort):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

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
            self._conn = await aiosqlite.connect(str(self._db_path))
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
    ) -> MeetingRecord:
        """Persist one active meeting or return the concurrent winner.

        ``BEGIN IMMEDIATE`` serializes the check/insert across repository
        instances.  Migration 033's partial unique index is the final arbiter;
        a losing process adopts that row instead of returning a phantom id or
        surfacing an HTTP 500.
        """
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                with contextlib.suppress(sqlite3.IntegrityError):
                    await conn.execute(
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
                    # The conflict may be either the same meeting id or the
                    # owner-scoped active-meeting index.  In both cases the
                    # authoritative active row is the only valid response.
                cur = await conn.execute(
                    "SELECT id, title, state, started_at, ended_at, finalized_at, "
                    "auto_started, minutes_json, raw_transcript_ref, "
                    "minutes_status, minutes_error, display_title, minutes_cleared_at, "
                    "rag_projection_state, rag_projection_error, rag_projected_at "
                    "FROM meetings WHERE tenant_id = ? AND owner_id = ? "
                    "AND state = 'in_meeting' ORDER BY started_at DESC, id DESC LIMIT 1",
                    (tenant_id, owner_id),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise RuntimeError("meeting insert conflicted without an active meeting")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return _meeting_from_row(row)

    async def update_meeting_state(
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
    ) -> None:
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
        if minutes_error is not None:
            fields.append("minutes_error = ?")
            values.append(minutes_error)
        if display_title is not None:
            fields.append("display_title = ?")
            values.append(display_title)
        if rag_projection_state is not None:
            fields.append("rag_projection_state = ?")
            values.append(rag_projection_state)
        if rag_projection_error is not None:
            fields.append("rag_projection_error = ?")
            values.append(rag_projection_error)
        if rag_projected_at is not None:
            fields.append("rag_projected_at = ?")
            values.append(_to_iso(rag_projected_at))
        values.extend((meeting_id, tenant_id, owner_id))
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                f"UPDATE meetings SET {', '.join(fields)} "
                "WHERE id = ? AND tenant_id = ? AND owner_id = ?",
                values,
            )
            await conn.commit()

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT id, title, state, started_at, ended_at, finalized_at, "
                "auto_started, minutes_json, raw_transcript_ref, "
                "minutes_status, minutes_error, display_title, minutes_cleared_at, "
                "rag_projection_state, rag_projection_error, rag_projected_at "
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
                "rag_projection_state, rag_projection_error, rag_projected_at FROM meetings "
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
                "rag_projected_at = NULL "
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
    ) -> None:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                """UPDATE meetings
                   SET rag_projection_state = ?, rag_projection_error = ?, rag_projected_at = ?
                   WHERE id = ? AND tenant_id = ? AND owner_id = ?""",
                (
                    state,
                    (error or "")[:500] or None,
                    _to_iso(projected_at) if projected_at is not None else None,
                    meeting_id,
                    tenant_id,
                    owner_id,
                ),
            )
            await conn.commit()

    async def list_meetings_needing_rag_projection(
        self,
        *,
        limit: int = 100,
    ) -> list[MeetingRecord]:
        tenant_id, _device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT id, title, state, started_at, ended_at, finalized_at,
                          auto_started, minutes_json, raw_transcript_ref,
                          minutes_status, minutes_error, display_title, minutes_cleared_at,
                          rag_projection_state, rag_projection_error, rag_projected_at
                   FROM meetings
                   WHERE tenant_id = ? AND owner_id = ?
                     AND rag_projection_state IN (
                         'index_pending', 'index_failed', 'delete_pending', 'delete_failed'
                     )
                   ORDER BY started_at ASC LIMIT ?""",
                (tenant_id, owner_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_meeting_from_row(row) for row in rows]

    async def list_meeting_rag_projection_scopes(self) -> list[tuple[str, str, str]]:
        """Internal startup repair scopes; request-facing reads remain principal scoped."""

        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                """SELECT tenant_id, MIN(device_id), owner_id
                   FROM meetings
                   WHERE rag_projection_state IN (
                       'index_pending', 'index_failed', 'delete_pending', 'delete_failed'
                   )
                   GROUP BY tenant_id, owner_id
                   ORDER BY tenant_id, owner_id"""
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
                    "SELECT text, start_ms, end_ms, speaker_id, speaker_label "
                    "FROM meeting_segments WHERE meeting_id = ? "
                    "AND tenant_id = ? AND owner_id = ? ORDER BY id ASC",
                    (meeting_id, tenant_id, owner_id),
                )
                rows = await cur.fetchall()
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
                "SELECT text, start_ms, end_ms, speaker_id, speaker_label "
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
    ) -> int:
        tenant_id, device_id, owner_id = _scope()
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "INSERT INTO ambient_segments "
                "(audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at, "
                "tenant_id, device_id, owner_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            row_id = cur.lastrowid
            await conn.commit()
            await cur.close()
        return int(row_id or 0)

    async def list_ambient_segments(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[AmbientSegmentRecord]:
        tenant_id, _device_id, owner_id = _scope()
        clauses: list[str] = ["tenant_id = ?", "owner_id = ?"]
        args: list[object] = [tenant_id, owner_id]
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
                "SELECT id, audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at "
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
                captured_at=_from_iso(r[6]) or datetime.fromtimestamp(0),
            )
            for r in rows
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
                captured_at=_from_iso(str(row[2])) or datetime.fromtimestamp(0),
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
            captured_at=_from_iso(str(row[2])) or datetime.fromtimestamp(0),
            quota_charged=bool(row[3]),
        )

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
    # migration 017 → 13 列（显式清理纪要 tombstone）；migration 026 → 16 列。
    return MeetingRecord(
        id=row[0],
        title=row[1],
        state=row[2],
        started_at=_from_iso(row[3]) or datetime.fromtimestamp(0),
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
    )


def _speaker_from_row(row: aiosqlite.Row | tuple[Any, ...]) -> SpeakerProfileRecord:
    return SpeakerProfileRecord(
        speaker_id=row[0],
        label=row[1],
        n_samples=row[2],
        first_seen_at=_from_iso(row[3]) or datetime.fromtimestamp(0),
        last_seen_at=_from_iso(row[4]) or datetime.fromtimestamp(0),
        embedding_blob=row[5],
    )


__all__ = ["SQLiteRepository"]
