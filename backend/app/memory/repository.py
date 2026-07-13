"""Owner-scoped persistence and episodic source reads for EchoDesk memory."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from app.adapters.repo.connection import (
    configure_aiosqlite_connection,
    open_aiosqlite_connection,
)
from app.memory.models import (
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryWriteCandidate,
    ProfileSettingRecord,
    ProvenanceInput,
    ProvenanceRecord,
    RecallCandidate,
)

_SPACE_RE = re.compile(r"\s+")
_KEY_RE = re.compile(r"[^0-9a-zA-Z\u3400-\u9fff:_./-]+")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _SPACE_RE.sub(" ", normalized).strip()


def normalize_key(value: str) -> str:
    normalized = _KEY_RE.sub("-", normalize_text(value)).strip("-:./")
    return normalized[:256] or sha256(value.encode("utf-8")).hexdigest()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: object, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord.model_validate(
        {
            "memory_id": row["memory_id"],
            "kind": row["kind"],
            "content": row["content"],
            "canonical_key": row["canonical_key"],
            "subject": row["subject"],
            "confidence": row["confidence"],
            "salience": row["salience"],
            "scope": row["scope"],
            "status": row["status"],
            "hit_count": row["hit_count"],
            "source_count": row["source_count"],
            "user_confirmed": row["user_confirmed"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
            "updated_at": row["updated_at"],
            "confirmed_at": row["confirmed_at"],
            "superseded_at": row["superseded_at"],
            "superseded_by": row["superseded_by"],
            "deleted_at": row["deleted_at"],
            "revision": row["revision"],
            "metadata": _json_load(row["metadata_json"], {}),
        }
    )


def _provenance_from_row(row: sqlite3.Row) -> ProvenanceRecord:
    return ProvenanceRecord.model_validate(
        {
            "provenance_id": row["provenance_id"],
            "memory_id": row["memory_id"],
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "source_segment_id": row["source_segment_id"],
            "meeting_id": row["meeting_id"],
            "artifact_id": row["artifact_id"],
            "excerpt": row["excerpt"],
            "confidence": row["confidence"],
            "occurred_at": row["occurred_at"],
            "created_at": row["created_at"],
            "metadata": _json_load(row["metadata_json"], {}),
        }
    )


def _profile_from_row(row: sqlite3.Row) -> ProfileSettingRecord:
    return ProfileSettingRecord.model_validate(
        {
            "config_key": row["config_key"],
            "value": _json_load(row["value_json"], None),
            "description": row["description"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "confirmed_at": row["confirmed_at"],
            "deleted_at": row["deleted_at"],
            "revision": row["revision"],
        }
    )


def _lexical_similarity(query: str, content: str) -> float:
    q = normalize_text(query).replace(" ", "")
    c = normalize_text(content).replace(" ", "")
    if not q or not c:
        return 0.0
    if q in c or c in q:
        return min(1.0, 0.72 + 0.28 * min(len(q), len(c)) / max(len(q), len(c)))
    q_units = set(q if len(q) < 3 else (q[i : i + 2] for i in range(len(q) - 1)))
    c_units = set(c if len(c) < 3 else (c[i : i + 2] for i in range(len(c) - 1)))
    if not q_units or not c_units:
        return 0.0
    return len(q_units & c_units) / math.sqrt(len(q_units) * len(c_units))


def _bounded_text(value: object, limit: int = 2_000) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    return text[:limit]


def _minutes_text(raw: object) -> str:
    parsed = _json_load(raw, {})
    if not isinstance(parsed, dict):
        return _bounded_text(raw)
    parts: list[str] = []
    for key in ("title", "summary", "overview", "decisions", "action_items", "todos"):
        value = parsed.get(key)
        if value:
            parts.append(f"{key}: {_bounded_text(_json_dump(value), 1_200)}")
    return _bounded_text("；".join(parts) or raw)


class MemoryRepository:
    """SQLite adapter for L1/L2/L3 memory data."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()

    @staticmethod
    async def _select_existing_tx(
        conn: Any,
        scope: MemoryScope,
        candidate: MemoryWriteCandidate,
    ) -> sqlite3.Row | None:
        if candidate.existing_memory_id:
            cursor = await conn.execute(
                """SELECT * FROM memory_nodes
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?
                     AND status = 'active'""",
                (scope.tenant_id, scope.owner_id, candidate.existing_memory_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                return cast(sqlite3.Row, row)
        cursor = await conn.execute(
            """SELECT * FROM memory_nodes
               WHERE tenant_id = ? AND owner_id = ? AND status = 'active'
                 AND kind = ? AND (canonical_key = ? OR normalized_content = ?)
               ORDER BY user_confirmed DESC, updated_at DESC LIMIT 1""",
            (
                scope.tenant_id,
                scope.owner_id,
                candidate.kind,
                normalize_key(candidate.canonical_key),
                normalize_text(candidate.content),
            ),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return cast(sqlite3.Row | None, row)

    @staticmethod
    async def _insert_provenance_tx(
        conn: Any,
        scope: MemoryScope,
        memory_id: str,
        provenance: ProvenanceInput,
    ) -> bool:
        excerpt_hash = sha256(provenance.excerpt.encode("utf-8")).hexdigest()
        cursor = await conn.execute(
            """INSERT OR IGNORE INTO memory_provenance (
                   tenant_id, owner_id, provenance_id, memory_id,
                   source_kind, source_id, source_segment_id, meeting_id,
                   artifact_id, excerpt, excerpt_sha256, confidence,
                   occurred_at, created_at, metadata_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scope.tenant_id,
                scope.owner_id,
                f"prov_{uuid4().hex}",
                memory_id,
                provenance.source_kind,
                provenance.source_id,
                provenance.source_segment_id,
                provenance.meeting_id,
                provenance.artifact_id,
                provenance.excerpt,
                excerpt_hash,
                provenance.confidence,
                _iso(provenance.occurred_at),
                _iso(utc_now()),
                _json_dump(provenance.metadata),
            ),
        )
        inserted = cursor.rowcount > 0
        await cursor.close()
        return bool(inserted)

    @staticmethod
    async def _insert_relation_tx(
        conn: Any,
        scope: MemoryScope,
        source_id: str,
        target_id: str,
        relation_kind: str,
        confidence: float,
    ) -> None:
        if source_id == target_id:
            return
        await conn.execute(
            """INSERT OR IGNORE INTO memory_relations (
                   tenant_id, owner_id, relation_id, source_memory_id,
                   target_memory_id, relation_kind, confidence, created_at
               )
               SELECT ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (
                   SELECT 1 FROM memory_nodes
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?
                     AND status != 'deleted'
               )""",
            (
                scope.tenant_id,
                scope.owner_id,
                f"rel_{uuid4().hex}",
                source_id,
                target_id,
                relation_kind,
                confidence,
                _iso(utc_now()),
                scope.tenant_id,
                scope.owner_id,
                target_id,
            ),
        )

    async def upsert_candidate(
        self,
        scope: MemoryScope,
        candidate: MemoryWriteCandidate,
        provenance: ProvenanceInput,
    ) -> MemoryRecord | None:
        if candidate.action == "ignore":
            return None
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._select_existing_tx(conn, scope, candidate)
                memory_id = await self._write_candidate_tx(
                    conn,
                    scope,
                    candidate,
                    provenance,
                    existing,
                )
                for target_id in dict.fromkeys(candidate.relation_memory_ids):
                    await self._insert_relation_tx(
                        conn,
                        scope,
                        memory_id,
                        target_id,
                        "related_to",
                        candidate.confidence,
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            cursor = await conn.execute(
                """SELECT * FROM memory_nodes
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (scope.tenant_id, scope.owner_id, memory_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return _memory_from_row(row) if row is not None else None

    async def _write_candidate_tx(
        self,
        conn: Any,
        scope: MemoryScope,
        candidate: MemoryWriteCandidate,
        provenance: ProvenanceInput,
        existing: sqlite3.Row | None,
    ) -> str:
        if existing is not None and candidate.action != "supersede":
            memory_id = str(existing["memory_id"])
            inserted = await self._insert_provenance_tx(conn, scope, memory_id, provenance)
            await conn.execute(
                """UPDATE memory_nodes
                   SET hit_count = hit_count + 1,
                       source_count = source_count + ?,
                       confidence = MAX(confidence, ?),
                       salience = MAX(salience, ?),
                       last_seen_at = ?, updated_at = ?, revision = revision + 1
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (
                    int(inserted),
                    candidate.confidence,
                    candidate.salience,
                    _iso(provenance.occurred_at),
                    _iso(utc_now()),
                    scope.tenant_id,
                    scope.owner_id,
                    memory_id,
                ),
            )
            return memory_id

        old_memory_id = str(existing["memory_id"]) if existing is not None else None
        if old_memory_id:
            await conn.execute(
                """UPDATE memory_nodes
                   SET status = 'superseded', superseded_at = ?, updated_at = ?,
                       revision = revision + 1
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (
                    _iso(utc_now()),
                    _iso(utc_now()),
                    scope.tenant_id,
                    scope.owner_id,
                    old_memory_id,
                ),
            )
        memory_id = f"mem_{uuid4().hex}"
        now = utc_now()
        await conn.execute(
            """INSERT INTO memory_nodes (
                   tenant_id, owner_id, memory_id, kind, content,
                   normalized_content, canonical_key, subject, confidence,
                   salience, scope, created_at, last_seen_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scope.tenant_id,
                scope.owner_id,
                memory_id,
                candidate.kind,
                candidate.content.strip(),
                normalize_text(candidate.content),
                normalize_key(candidate.canonical_key),
                candidate.subject,
                candidate.confidence,
                candidate.salience,
                candidate.scope,
                _iso(now),
                _iso(provenance.occurred_at),
                _iso(now),
            ),
        )
        await self._insert_provenance_tx(conn, scope, memory_id, provenance)
        if old_memory_id:
            await conn.execute(
                """UPDATE memory_nodes SET superseded_by = ?
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (memory_id, scope.tenant_id, scope.owner_id, old_memory_id),
            )
            await self._insert_relation_tx(
                conn,
                scope,
                memory_id,
                old_memory_id,
                "supersedes",
                candidate.confidence,
            )
        return memory_id

    async def get_node(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        include_deleted: bool = False,
    ) -> MemoryRecord | None:
        clause = "" if include_deleted else "AND status != 'deleted'"
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                f"""SELECT * FROM memory_nodes
                    WHERE tenant_id = ? AND owner_id = ? AND memory_id = ? {clause}""",
                (scope.tenant_id, scope.owner_id, memory_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _memory_from_row(row) if row is not None else None

    async def list_nodes(
        self,
        scope: MemoryScope,
        *,
        query: str | None = None,
        kind: MemoryKind | None = None,
        include_deleted: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        clauses = ["tenant_id = ?", "owner_id = ?"]
        args: list[object] = [scope.tenant_id, scope.owner_id]
        if not include_deleted:
            clauses.append("status = 'active'")
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        fetch_limit = min(500, max(limit * 8, limit)) if query else limit
        args.append(fetch_limit)
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                f"""SELECT * FROM memory_nodes WHERE {" AND ".join(clauses)}
                    ORDER BY user_confirmed DESC, salience DESC, last_seen_at DESC
                    LIMIT ?""",
                tuple(args),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        records = [_memory_from_row(row) for row in rows]
        if query:
            records.sort(
                key=lambda item: (
                    _lexical_similarity(query, item.content),
                    item.salience,
                    item.last_seen_at,
                ),
                reverse=True,
            )
        return records[:limit]

    async def list_provenance(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        limit: int = 100,
    ) -> list[ProvenanceRecord]:
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                """SELECT * FROM memory_provenance
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?
                   ORDER BY occurred_at DESC, created_at DESC LIMIT ?""",
                (scope.tenant_id, scope.owner_id, memory_id, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_provenance_from_row(row) for row in rows]

    async def confirm_node(self, scope: MemoryScope, memory_id: str) -> MemoryRecord | None:
        now = _iso(utc_now())
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            await conn.execute(
                """UPDATE memory_nodes
                   SET user_confirmed = 1, confidence = 1.0,
                       confirmed_at = ?, updated_at = ?, revision = revision + 1
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?
                     AND status = 'active'""",
                (now, now, scope.tenant_id, scope.owner_id, memory_id),
            )
            await conn.commit()
        return await self.get_node(scope, memory_id)

    async def update_node(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        content: str | None = None,
        kind: MemoryKind | None = None,
        canonical_key: str | None = None,
        salience: float | None = None,
        memory_scope: str | None = None,
    ) -> MemoryRecord | None:
        current = await self.get_node(scope, memory_id)
        if current is None or current.status != "active":
            return None
        updated_content = content.strip() if content is not None else current.content
        now = _iso(utc_now())
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            await conn.execute(
                """UPDATE memory_nodes
                   SET content = ?, normalized_content = ?, kind = ?, canonical_key = ?,
                       salience = ?, scope = ?, user_confirmed = 1, confidence = 1.0,
                       confirmed_at = ?, updated_at = ?, revision = revision + 1
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?
                     AND status = 'active'""",
                (
                    updated_content,
                    normalize_text(updated_content),
                    kind or current.kind,
                    normalize_key(canonical_key or current.canonical_key),
                    salience if salience is not None else current.salience,
                    memory_scope or current.scope,
                    now,
                    now,
                    scope.tenant_id,
                    scope.owner_id,
                    memory_id,
                ),
            )
            await self._insert_provenance_tx(
                conn,
                scope,
                memory_id,
                ProvenanceInput(
                    source_kind="user_explicit",
                    source_id=f"edit:{memory_id}:{current.revision + 1}",
                    excerpt=updated_content,
                    confidence=1.0,
                    occurred_at=utc_now(),
                    metadata={"action": "user_edit", "revision": current.revision + 1},
                ),
            )
            await conn.commit()
        return await self.get_node(scope, memory_id)

    async def delete_node(self, scope: MemoryScope, memory_id: str) -> bool:
        now = _iso(utc_now())
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            cursor = await conn.execute(
                """UPDATE memory_nodes
                   SET status = 'deleted', content = '[已删除]', normalized_content = '',
                       canonical_key = 'deleted:' || memory_id, subject = NULL,
                       metadata_json = '{}', deleted_at = ?, updated_at = ?,
                       revision = revision + 1
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?
                     AND status != 'deleted'""",
                (now, now, scope.tenant_id, scope.owner_id, memory_id),
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await conn.execute(
                """DELETE FROM memory_relations
                   WHERE tenant_id = ? AND owner_id = ?
                     AND (source_memory_id = ? OR target_memory_id = ?)""",
                (scope.tenant_id, scope.owner_id, memory_id, memory_id),
            )
            await conn.execute(
                """DELETE FROM memory_provenance
                   WHERE tenant_id = ? AND owner_id = ? AND memory_id = ?""",
                (scope.tenant_id, scope.owner_id, memory_id),
            )
            await conn.execute(
                """UPDATE memory_extraction_runs SET output_json = '{}'
                   WHERE tenant_id = ? AND owner_id = ?
                     AND instr(output_json, ?) > 0""",
                (scope.tenant_id, scope.owner_id, memory_id),
            )
            await conn.commit()
        return deleted

    async def upsert_profile_setting(
        self,
        scope: MemoryScope,
        config_key: str,
        value: Any,
        *,
        description: str | None = None,
    ) -> ProfileSettingRecord:
        key = normalize_key(config_key)
        now = _iso(utc_now())
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            await conn.execute(
                """INSERT INTO memory_profile_settings (
                       tenant_id, owner_id, config_key, value_json, description,
                       created_at, updated_at, confirmed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner_id, config_key) DO UPDATE SET
                       value_json = excluded.value_json,
                       description = excluded.description,
                       source = 'user_explicit',
                       updated_at = excluded.updated_at,
                       confirmed_at = excluded.confirmed_at,
                       deleted_at = NULL,
                       revision = memory_profile_settings.revision + 1""",
                (
                    scope.tenant_id,
                    scope.owner_id,
                    key,
                    _json_dump(value),
                    description,
                    now,
                    now,
                    now,
                ),
            )
            await conn.commit()
            cursor = await conn.execute(
                """SELECT * FROM memory_profile_settings
                   WHERE tenant_id = ? AND owner_id = ? AND config_key = ?""",
                (scope.tenant_id, scope.owner_id, key),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _profile_from_row(cast(sqlite3.Row, row))

    async def list_profile_settings(self, scope: MemoryScope) -> list[ProfileSettingRecord]:
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                """SELECT * FROM memory_profile_settings
                   WHERE tenant_id = ? AND owner_id = ? AND deleted_at IS NULL
                   ORDER BY updated_at DESC""",
                (scope.tenant_id, scope.owner_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_profile_from_row(row) for row in rows]

    async def delete_profile_setting(self, scope: MemoryScope, config_key: str) -> bool:
        now = _iso(utc_now())
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            cursor = await conn.execute(
                """UPDATE memory_profile_settings
                   SET value_json = 'null', description = NULL,
                       deleted_at = ?, updated_at = ?, revision = revision + 1
                   WHERE tenant_id = ? AND owner_id = ? AND config_key = ?
                     AND deleted_at IS NULL""",
                (now, now, scope.tenant_id, scope.owner_id, normalize_key(config_key)),
            )
            deleted = cursor.rowcount > 0
            await cursor.close()
            await conn.commit()
        return deleted

    async def current_meeting_candidates(
        self,
        scope: MemoryScope,
        *,
        max_age_s: float,
        limit: int,
    ) -> list[RecallCandidate]:
        cutoff = _iso(utc_now() - timedelta(seconds=max_age_s))
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                """SELECT s.id, s.meeting_id, s.text, s.captured_at,
                          COALESCE(s.speaker_label, s.speaker_id, '说话人') AS speaker
                   FROM meeting_segments AS s
                   JOIN meetings AS m
                     ON m.tenant_id = s.tenant_id AND m.owner_id = s.owner_id
                    AND m.id = s.meeting_id
                   WHERE s.tenant_id = ? AND s.owner_id = ?
                     AND m.state = 'in_meeting' AND s.captured_at >= ?
                   ORDER BY s.captured_at DESC, s.id DESC LIMIT ?""",
                (scope.tenant_id, scope.owner_id, cutoff, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            RecallCandidate(
                candidate_id=f"l0-meeting-segment:{row['id']}",
                level="L0",
                content=f"{row['speaker']}：{_bounded_text(row['text'])}",
                source_ref=f"meeting:{row['meeting_id']}#segment:{row['id']}",
                occurred_at=_datetime(row["captured_at"]),
                salience=0.72,
                kind="current_meeting",
                metadata={"meeting_id": str(row["meeting_id"]), "segment_id": int(row["id"])},
            )
            for row in rows
        ]

    async def episodic_candidates(
        self,
        scope: MemoryScope,
        *,
        limit_per_kind: int = 60,
    ) -> list[RecallCandidate]:
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            meeting_segments = await self._fetchall(
                conn,
                """SELECT s.id, s.meeting_id, s.text, s.captured_at,
                          COALESCE(m.display_title, m.title, s.meeting_id) AS meeting_title,
                          COALESCE(s.speaker_label, s.speaker_id, '说话人') AS speaker
                   FROM meeting_segments AS s
                   JOIN meetings AS m
                     ON m.tenant_id = s.tenant_id AND m.owner_id = s.owner_id
                    AND m.id = s.meeting_id
                   WHERE s.tenant_id = ? AND s.owner_id = ? AND m.state != 'in_meeting'
                   ORDER BY s.captured_at DESC, s.id DESC LIMIT ?""",
                (scope.tenant_id, scope.owner_id, limit_per_kind),
            )
            meetings = await self._fetchall(
                conn,
                """SELECT id, COALESCE(display_title, title, id) AS meeting_title,
                          minutes_json, COALESCE(finalized_at, ended_at, started_at) AS occurred_at
                   FROM meetings
                   WHERE tenant_id = ? AND owner_id = ? AND minutes_json IS NOT NULL
                   ORDER BY occurred_at DESC LIMIT ?""",
                (scope.tenant_id, scope.owner_id, max(10, limit_per_kind // 3)),
            )
            ambient = await self._fetchall(
                conn,
                """SELECT id, text, captured_at,
                          COALESCE(speaker_label, speaker_id, '环境记录') AS speaker
                   FROM ambient_segments
                   WHERE tenant_id = ? AND owner_id = ?
                   ORDER BY captured_at DESC, id DESC LIMIT ?""",
                (scope.tenant_id, scope.owner_id, limit_per_kind),
            )
            artifacts = await self._fetchall(
                conn,
                """SELECT artifact_id, artifact_type, title, metadata_json, created_at
                   FROM artifacts
                   WHERE tenant_id = ? AND owner_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (scope.tenant_id, scope.owner_id, max(20, limit_per_kind // 2)),
            )
        return self._episodic_rows_to_candidates(
            meeting_segments,
            meetings,
            ambient,
            artifacts,
        )

    @staticmethod
    async def _fetchall(conn: Any, sql: str, args: tuple[object, ...]) -> list[sqlite3.Row]:
        cursor = await conn.execute(sql, args)
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    @staticmethod
    def _episodic_rows_to_candidates(
        segments: list[sqlite3.Row],
        meetings: list[sqlite3.Row],
        ambient: list[sqlite3.Row],
        artifacts: list[sqlite3.Row],
    ) -> list[RecallCandidate]:
        out: list[RecallCandidate] = []
        for row in segments:
            out.append(
                RecallCandidate(
                    candidate_id=f"l1-meeting-segment:{row['id']}",
                    level="L1",
                    content=(
                        f"会议《{row['meeting_title']}》中 {row['speaker']}："
                        f"{_bounded_text(row['text'])}"
                    ),
                    source_ref=f"meeting:{row['meeting_id']}#segment:{row['id']}",
                    occurred_at=_datetime(row["captured_at"]),
                    salience=0.62,
                    kind="meeting_segment",
                    metadata={"meeting_id": str(row["meeting_id"]), "segment_id": int(row["id"])},
                )
            )
        for row in meetings:
            out.append(
                RecallCandidate(
                    candidate_id=f"l1-meeting-minutes:{row['id']}",
                    level="L1",
                    content=f"会议《{row['meeting_title']}》纪要：{_minutes_text(row['minutes_json'])}",
                    source_ref=f"meeting:{row['id']}#minutes",
                    occurred_at=_datetime(row["occurred_at"]),
                    salience=0.78,
                    kind="meeting_minutes",
                    metadata={"meeting_id": str(row["id"])},
                )
            )
        for row in ambient:
            out.append(
                RecallCandidate(
                    candidate_id=f"l1-ambient:{row['id']}",
                    level="L1",
                    content=f"{row['speaker']}：{_bounded_text(row['text'])}",
                    source_ref=f"ambient:{row['id']}",
                    occurred_at=_datetime(row["captured_at"]),
                    salience=0.48,
                    kind="ambient_segment",
                    metadata={"segment_id": int(row["id"])},
                )
            )
        for row in artifacts:
            metadata = _json_load(row["metadata_json"], {})
            summary = ""
            if isinstance(metadata, dict):
                summary = _bounded_text(
                    metadata.get("summary") or metadata.get("description") or "",
                    800,
                )
            out.append(
                RecallCandidate(
                    candidate_id=f"l1-artifact:{row['artifact_id']}",
                    level="L1",
                    content=(
                        f"工作产物《{row['title'] or row['artifact_type']}》"
                        f"{f'：{summary}' if summary else ''}"
                    ),
                    source_ref=f"artifact:{row['artifact_id']}",
                    occurred_at=_datetime(row["created_at"]),
                    salience=0.7,
                    kind="artifact",
                    metadata={"artifact_id": str(row["artifact_id"])},
                )
            )
        return out

    async def semantic_candidates(
        self,
        scope: MemoryScope,
        *,
        limit: int = 100,
    ) -> list[RecallCandidate]:
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            conn.row_factory = sqlite3.Row
            rows = await self._fetchall(
                conn,
                """SELECT n.*,
                          (SELECT p.source_kind || ':' || p.source_id
                           FROM memory_provenance AS p
                           WHERE p.tenant_id = n.tenant_id AND p.owner_id = n.owner_id
                             AND p.memory_id = n.memory_id
                           ORDER BY p.occurred_at DESC, p.created_at DESC LIMIT 1)
                          AS latest_source_ref
                   FROM memory_nodes AS n
                   WHERE n.tenant_id = ? AND n.owner_id = ? AND n.status = 'active'
                   ORDER BY n.user_confirmed DESC, n.salience DESC, n.last_seen_at DESC
                   LIMIT ?""",
                (scope.tenant_id, scope.owner_id, limit),
            )
        return [
            RecallCandidate(
                candidate_id=f"l2-memory:{row['memory_id']}",
                level="L2",
                content=str(row["content"]),
                source_ref=str(row["latest_source_ref"] or f"memory:{row['memory_id']}"),
                occurred_at=_datetime(row["last_seen_at"]),
                salience=float(row["salience"]),
                confidence=float(row["confidence"]),
                kind=str(row["kind"]),
                memory_id=str(row["memory_id"]),
                metadata={
                    "canonical_key": str(row["canonical_key"]),
                    "user_confirmed": bool(row["user_confirmed"]),
                    "source_count": int(row["source_count"]),
                },
            )
            for row in rows
        ]

    async def profile_candidates(self, scope: MemoryScope) -> list[RecallCandidate]:
        rows = await self.list_profile_settings(scope)
        return [
            RecallCandidate(
                candidate_id=f"l3-profile:{row.config_key}",
                level="L3",
                content=f"{row.config_key}：{_bounded_text(_json_dump(row.value))}",
                source_ref=f"profile:{row.config_key}",
                occurred_at=row.updated_at,
                salience=1.0,
                confidence=1.0,
                kind="explicit_profile_setting",
                metadata={"config_key": row.config_key, "description": row.description},
            )
            for row in rows
        ]

    async def record_extraction_run(
        self,
        scope: MemoryScope,
        *,
        run_id: str,
        source_kind: str,
        source_id: str,
        input_sha256: str,
        model: str,
        model_display_name: str,
        state: str,
        latency_ms: float,
        candidate_count: int,
        output: Any,
        error: str | None,
        created_at: datetime,
    ) -> None:
        finished_at = _iso(utc_now()) if state != "running" else None
        async with open_aiosqlite_connection(self.db_path) as conn:
            await configure_aiosqlite_connection(conn)
            await conn.execute(
                """INSERT INTO memory_extraction_runs (
                       tenant_id, owner_id, run_id, source_kind, source_id,
                       input_sha256, model, model_display_name, state, latency_ms,
                       candidate_count, output_json, error, created_at, finished_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, owner_id, run_id) DO UPDATE SET
                       state = excluded.state,
                       latency_ms = excluded.latency_ms,
                       candidate_count = excluded.candidate_count,
                       output_json = excluded.output_json,
                       error = excluded.error,
                       finished_at = excluded.finished_at""",
                (
                    scope.tenant_id,
                    scope.owner_id,
                    run_id,
                    source_kind,
                    source_id,
                    input_sha256,
                    model,
                    model_display_name,
                    state,
                    max(0.0, latency_ms),
                    max(0, candidate_count),
                    _json_dump(output),
                    error,
                    _iso(created_at),
                    finished_at,
                ),
            )
            await conn.commit()


__all__ = ["MemoryRepository", "normalize_key", "normalize_text", "utc_now"]
