"""SQLite 实现 RepositoryPort，单文件 ``~/.echodesk/echodesk.db``。

特性：
- 启动时 ``init()`` 自动建表（CREATE IF NOT EXISTS），无需 alembic
- 所有写路径串行通过 ``asyncio.Lock``，规避 sqlite 的"database is locked"
- aiosqlite 单连接（开 WAL），单进程并发足够
- 时间戳统一存 ISO-8601 UTC
- speaker_id / speaker_label 全程可空（旧数据兼容）

不在本类做的事：
- 业务校验、事件发布（留给 use_case 层）
- 大对象（音频文件本体）→ 文件系统存，DB 只存 ref
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import aiosqlite

from app.ports.repository import (
    AmbientSegmentRecord,
    MeetingRecord,
    MeetingState,
    RepositoryPort,
    SpeakerProfileRecord,
)
from app.schemas.meeting import TranscriptSegment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT,
    state TEXT NOT NULL CHECK(state IN ('in_meeting','ended','finalized')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    finalized_at TEXT,
    auto_started INTEGER NOT NULL DEFAULT 0,
    minutes_json TEXT,
    raw_transcript_ref TEXT
);

CREATE TABLE IF NOT EXISTS meeting_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    text TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_id TEXT,
    speaker_label TEXT,
    captured_at TEXT NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meeting_segments_meeting
    ON meeting_segments(meeting_id, start_ms);

CREATE TABLE IF NOT EXISTS meeting_speaker_labels (
    meeting_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (meeting_id, speaker_id),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ambient_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio_ref TEXT NOT NULL,
    text TEXT NOT NULL,
    speaker_id TEXT,
    speaker_label TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ambient_segments_captured
    ON ambient_segments(captured_at);
CREATE INDEX IF NOT EXISTS idx_ambient_segments_speaker
    ON ambient_segments(speaker_id);

CREATE TABLE IF NOT EXISTS speakers (
    speaker_id TEXT PRIMARY KEY,
    label TEXT,
    n_samples INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    embedding_blob BLOB
);
"""


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


class SQLiteRepository(RepositoryPort):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self._db_path))
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.executescript(_SCHEMA)
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
    ) -> None:
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                "INSERT OR IGNORE INTO meetings (id, title, state, started_at, auto_started) "
                "VALUES (?, ?, 'in_meeting', ?, ?)",
                (meeting_id, title, _to_iso(started_at), 1 if auto_started else 0),
            )
            await conn.commit()

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
    ) -> None:
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
        values.append(meeting_id)
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                f"UPDATE meetings SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            await conn.commit()

    async def get_meeting(self, meeting_id: str) -> MeetingRecord | None:
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT id, title, state, started_at, ended_at, finalized_at, "
                "auto_started, minutes_json, raw_transcript_ref "
                "FROM meetings WHERE id = ?",
                (meeting_id,),
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
        async with self._lock:
            conn = self._require_conn()
            sql = (
                "SELECT id, title, state, started_at, ended_at, finalized_at, "
                "auto_started, minutes_json, raw_transcript_ref FROM meetings"
            )
            args: tuple[object, ...] = ()
            if state is not None:
                sql += " WHERE state = ?"
                args = (state,)
            sql += " ORDER BY started_at DESC LIMIT ?"
            args = (*args, limit)
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        return [_meeting_from_row(r) for r in rows]

    # ── Meeting segments ────────────────────────────────────────
    async def append_meeting_segment(
        self,
        meeting_id: str,
        seg: TranscriptSegment,
        *,
        captured_at: datetime,
    ) -> None:
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                "INSERT INTO meeting_segments "
                "(meeting_id, text, start_ms, end_ms, speaker_id, speaker_label, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    meeting_id,
                    seg.text,
                    seg.start_ms,
                    seg.end_ms,
                    seg.speaker_id,
                    seg.speaker_label,
                    _to_iso(captured_at),
                ),
            )
            await conn.commit()

    async def list_meeting_segments(
        self,
        meeting_id: str,
    ) -> list[TranscriptSegment]:
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT text, start_ms, end_ms, speaker_id, speaker_label "
                "FROM meeting_segments WHERE meeting_id = ? ORDER BY id ASC",
                (meeting_id,),
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

    # ── per-meeting speaker label map ───────────────────────────
    async def upsert_meeting_speaker_label(
        self,
        meeting_id: str,
        speaker_id: str,
        label: str,
    ) -> None:
        async with self._lock:
            conn = self._require_conn()
            await conn.execute(
                "INSERT INTO meeting_speaker_labels (meeting_id, speaker_id, label) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET label = excluded.label",
                (meeting_id, speaker_id, label),
            )
            await conn.commit()

    async def get_meeting_speaker_labels(
        self,
        meeting_id: str,
    ) -> dict[str, str]:
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT speaker_id, label FROM meeting_speaker_labels "
                "WHERE meeting_id = ?",
                (meeting_id,),
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
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "INSERT INTO ambient_segments "
                "(audio_ref, text, speaker_id, speaker_label, duration_ms, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    audio_ref,
                    text,
                    speaker_id,
                    speaker_label,
                    duration_ms,
                    _to_iso(captured_at),
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
        clauses: list[str] = []
        args: list[object] = []
        if since is not None:
            clauses.append("captured_at >= ?")
            args.append(_to_iso(since))
        if until is not None:
            clauses.append("captured_at <= ?")
            args.append(_to_iso(until))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
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
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute("SELECT COUNT(*) FROM ambient_segments")
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    # ── Global speakers registry ────────────────────────────────
    async def upsert_speaker(
        self,
        speaker_id: str,
        *,
        captured_at: datetime,
        label: str | None = None,
        embedding_blob: bytes | None = None,
    ) -> None:
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT first_seen_at, n_samples FROM speakers WHERE speaker_id = ?",
                (speaker_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                await conn.execute(
                    "INSERT INTO speakers "
                    "(speaker_id, label, n_samples, first_seen_at, last_seen_at, embedding_blob) "
                    "VALUES (?, ?, 1, ?, ?, ?)",
                    (
                        speaker_id,
                        label,
                        _to_iso(captured_at),
                        _to_iso(captured_at),
                        embedding_blob,
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
                vals.append(speaker_id)
                await conn.execute(
                    f"UPDATE speakers SET {', '.join(sets)} WHERE speaker_id = ?",
                    vals,
                )
            await conn.commit()

    async def get_speaker(self, speaker_id: str) -> SpeakerProfileRecord | None:
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT speaker_id, label, n_samples, first_seen_at, last_seen_at, embedding_blob "
                "FROM speakers WHERE speaker_id = ?",
                (speaker_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return _speaker_from_row(row) if row else None

    async def list_speakers(self) -> list[SpeakerProfileRecord]:
        async with self._lock:
            conn = self._require_conn()
            cur = await conn.execute(
                "SELECT speaker_id, label, n_samples, first_seen_at, last_seen_at, embedding_blob "
                "FROM speakers ORDER BY last_seen_at DESC"
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_speaker_from_row(r) for r in rows]


def _meeting_from_row(row: aiosqlite.Row | tuple) -> MeetingRecord:
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
    )


def _speaker_from_row(row: aiosqlite.Row | tuple) -> SpeakerProfileRecord:
    return SpeakerProfileRecord(
        speaker_id=row[0],
        label=row[1],
        n_samples=row[2],
        first_seen_at=_from_iso(row[3]) or datetime.fromtimestamp(0),
        last_seen_at=_from_iso(row[4]) or datetime.fromtimestamp(0),
        embedding_blob=row[5],
    )


__all__ = ["SQLiteRepository"]
