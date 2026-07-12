"""Cross-process commit store for the BM25 JSON cache and revision manifest."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.adapters.repo.connection import configure_sqlite_connection
from app.security import LEGACY_DEVICE_ID, LEGACY_OWNER_ID, LEGACY_TENANT_ID

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rag_documents (
    tenant_id TEXT NOT NULL DEFAULT 'legacy-local',
    device_id TEXT NOT NULL DEFAULT 'legacy-local',
    owner_id TEXT NOT NULL DEFAULT 'legacy-local',
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'upload',
    source_path TEXT,
    index_path TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_id, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_rag_documents_owner_source
    ON rag_documents(tenant_id, owner_id, source, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_documents_owner_source_path_unique
    ON rag_documents(tenant_id, owner_id, source_path)
    WHERE source_path IS NOT NULL;
CREATE TABLE IF NOT EXISTS bm25_index_state (
    index_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bm25_index_documents (
    index_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    source_path TEXT,
    index_path TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_revision INTEGER NOT NULL CHECK(updated_revision >= 1),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_key, tenant_id, owner_id, doc_id),
    FOREIGN KEY (index_key) REFERENCES bm25_index_state(index_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bm25_index_documents_revision
    ON bm25_index_documents(index_key, updated_revision);
CREATE INDEX IF NOT EXISTS idx_bm25_index_documents_scope
    ON bm25_index_documents(index_key, tenant_id, owner_id, doc_id);
"""

Payload = dict[str, Any]
PayloadMutator = Callable[[Payload | None], Payload | None]


class BM25IndexStoreError(RuntimeError):
    """The durable BM25 manifest or its cache failed an integrity check."""


@dataclass(frozen=True, slots=True)
class StoredIndexDocument:
    payload: Payload
    index_path: Path
    content_hash: str
    updated_revision: int


@dataclass(frozen=True, slots=True)
class IndexSnapshot:
    revision: int
    documents: tuple[StoredIndexDocument, ...]


@dataclass(frozen=True, slots=True)
class IndexMutation:
    revision: int
    payload: Payload | None
    changed: bool


def _encode_payload(payload: Payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _payload_fields(
    payload: Payload,
) -> tuple[str, str, str, str, str, str, str | None]:
    tenant_id = str(payload.get("tenant_id") or LEGACY_TENANT_ID)
    owner_id = str(payload.get("owner_id") or LEGACY_OWNER_ID)
    device_id = str(payload.get("device_id") or LEGACY_DEVICE_ID)
    doc_id = str(payload.get("doc_id") or "").strip()
    if not doc_id:
        raise BM25IndexStoreError("BM25 payload is missing doc_id")
    title = str(payload.get("doc_title") or doc_id)
    source = "upload"
    source_path: str | None = None
    chunks = payload.get("chunks")
    if isinstance(chunks, list) and chunks:
        first = chunks[0]
        metadata = first.get("metadata") if isinstance(first, dict) else None
        if isinstance(metadata, dict):
            source = str(metadata.get("source") or metadata.get("kind") or source)
            raw_source_path = metadata.get("source_path")
            source_path = str(raw_source_path) if raw_source_path else None
    return tenant_id, owner_id, device_id, doc_id, title, source, source_path


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".bm25-{path.name}-{uuid4().hex}.tmp"
    try:
        with temp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _cache_matches(path: Path, content_hash: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == content_hash
    except OSError:
        return False


class BM25IndexStore:
    """SQLite-coordinated payload manifest for one physical BM25 index directory."""

    def __init__(
        self,
        db_path: Path | str,
        index_dir: Path | str,
        *,
        logger: logging.Logger,
        max_scope_payload_bytes: int,
    ) -> None:
        if max_scope_payload_bytes < 1:
            raise ValueError("BM25 scope payload limit must be positive")
        self.db_path = Path(db_path).expanduser()
        self.index_dir = Path(index_dir).expanduser().resolve()
        self.index_key = str(self.index_dir)
        self.log = logger
        self.max_scope_payload_bytes = max_scope_payload_bytes
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()
        self._bootstrap_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        configure_sqlite_connection(conn)
        return conn

    def _initialize_schema(self) -> None:
        """Migration 031 is authoritative; this is a direct-adapter test fallback."""

        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                """INSERT OR IGNORE INTO bm25_index_state(index_key, revision)
                   VALUES (?, 0)""",
                (self.index_key,),
            )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def _safe_index_path(self, raw: Path | str) -> Path:
        path = Path(raw).expanduser().resolve()
        if path.parent != self.index_dir or path.suffix.lower() != ".json":
            raise BM25IndexStoreError(f"BM25 cache path escapes index directory: {path}")
        return path

    def _normalize_payload(self, payload: Payload, revision: int) -> Payload:
        cloned = json.loads(json.dumps(payload, ensure_ascii=False))
        if not isinstance(cloned, dict):  # pragma: no cover - input type is already a dict
            raise BM25IndexStoreError("BM25 payload is not an object")
        normalized: Payload = cloned
        tenant_id, owner_id, device_id, doc_id, _title, _source, _source_path = _payload_fields(
            normalized
        )
        normalized["tenant_id"] = tenant_id
        normalized["owner_id"] = owner_id
        normalized["device_id"] = device_id
        normalized["doc_id"] = doc_id
        normalized["index_revision"] = revision
        normalized["_bm25_index_key"] = self.index_key
        return normalized

    def _revision(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT revision FROM bm25_index_state WHERE index_key = ?",
            (self.index_key,),
        ).fetchone()
        if row is None:
            raise BM25IndexStoreError("BM25 index state row is missing")
        return int(row[0])

    def _set_revision(self, conn: sqlite3.Connection, revision: int) -> None:
        conn.execute(
            """UPDATE bm25_index_state
               SET revision = ?, updated_at = CURRENT_TIMESTAMP
               WHERE index_key = ?""",
            (revision, self.index_key),
        )

    def _upsert_manifest(
        self,
        conn: sqlite3.Connection,
        payload: Payload,
        index_path: Path,
        content_hash: str,
    ) -> None:
        if not self._table_exists(conn, "rag_documents"):
            raise BM25IndexStoreError("rag_documents manifest table is missing")
        tenant_id, owner_id, device_id, doc_id, title, source, source_path = _payload_fields(
            payload
        )
        try:
            conn.execute(
                """INSERT INTO rag_documents
                   (tenant_id, device_id, owner_id, doc_id, title, source, source_path,
                    index_path, content_hash, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', CURRENT_TIMESTAMP,
                           CURRENT_TIMESTAMP)
                   ON CONFLICT(tenant_id, owner_id, doc_id) DO UPDATE SET
                       device_id = excluded.device_id,
                       title = excluded.title,
                       source = excluded.source,
                       source_path = excluded.source_path,
                       index_path = excluded.index_path,
                       content_hash = excluded.content_hash,
                       status = 'ready',
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    tenant_id,
                    device_id,
                    owner_id,
                    doc_id,
                    title,
                    source,
                    source_path,
                    str(index_path),
                    content_hash,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BM25IndexStoreError(f"rag_documents manifest rejected {doc_id}: {exc}") from exc

    def _delete_manifest(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        owner_id: str,
        doc_id: str,
    ) -> None:
        if self._table_exists(conn, "rag_documents"):
            conn.execute(
                """DELETE FROM rag_documents
                   WHERE tenant_id = ? AND owner_id = ? AND doc_id = ?""",
                (tenant_id, owner_id, doc_id),
            )

    def _insert_document(
        self,
        conn: sqlite3.Connection,
        payload: Payload,
        index_path: Path,
        revision: int,
    ) -> tuple[Payload, bytes, str]:
        normalized = self._normalize_payload(payload, revision)
        raw = _encode_payload(normalized)
        content_hash = hashlib.sha256(raw).hexdigest()
        tenant_id, owner_id, device_id, doc_id, title, source, source_path = _payload_fields(
            normalized
        )
        other_payload_bytes = int(
            conn.execute(
                """SELECT COALESCE(SUM(length(payload_json)), 0)
                   FROM bm25_index_documents
                   WHERE index_key = ? AND tenant_id = ? AND owner_id = ?
                     AND doc_id <> ?""",
                (self.index_key, tenant_id, owner_id, doc_id),
            ).fetchone()[0]
        )
        next_payload_bytes = other_payload_bytes + len(raw)
        if next_payload_bytes > self.max_scope_payload_bytes:
            raise BM25IndexStoreError(
                "BM25 principal index exceeds payload limit: "
                f"{next_payload_bytes} > {self.max_scope_payload_bytes}"
            )
        conn.execute(
            """INSERT INTO bm25_index_documents
               (index_key, tenant_id, owner_id, doc_id, device_id, title, source,
                source_path, index_path, payload_json, content_hash, updated_revision,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(index_key, tenant_id, owner_id, doc_id) DO UPDATE SET
                   device_id = excluded.device_id,
                   title = excluded.title,
                   source = excluded.source,
                   source_path = excluded.source_path,
                   index_path = excluded.index_path,
                   payload_json = excluded.payload_json,
                   content_hash = excluded.content_hash,
                   updated_revision = excluded.updated_revision,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                self.index_key,
                tenant_id,
                owner_id,
                doc_id,
                device_id,
                title,
                source,
                source_path,
                str(index_path),
                raw.decode("utf-8"),
                content_hash,
                revision,
            ),
        )
        self._upsert_manifest(conn, normalized, index_path, content_hash)
        return normalized, raw, content_hash

    def _bootstrap_legacy_json(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = self._revision(conn)
            count = int(
                conn.execute(
                    """SELECT COUNT(*) FROM bm25_index_documents
                       WHERE index_key = ?""",
                    (self.index_key,),
                ).fetchone()[0]
            )
            if revision != 0 or count != 0:
                conn.commit()
                return
            imported = 0
            target_revision = 1
            for path in sorted(self.index_dir.glob("*.json")):
                conn.execute("SAVEPOINT bm25_legacy_document")
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("top-level JSON must be an object")
                    self._insert_document(conn, payload, path.resolve(), target_revision)
                    conn.execute("RELEASE SAVEPOINT bm25_legacy_document")
                    imported += 1
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT bm25_legacy_document")
                    conn.execute("RELEASE SAVEPOINT bm25_legacy_document")
                    self.log.warning(
                        "rag index file corrupt, skipping (doc 将不可用): %s → %s",
                        path,
                        exc,
                    )
            if imported:
                self._set_revision(conn, target_revision)
            conn.commit()

    def current_revision(self) -> int:
        with self._connect() as conn:
            return self._revision(conn)

    def _manifest_exists(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        owner_id: str,
        doc_id: str,
    ) -> bool:
        if not self._table_exists(conn, "rag_documents"):
            return True
        row = conn.execute(
            """SELECT 1 FROM rag_documents
               WHERE tenant_id = ? AND owner_id = ? AND doc_id = ?""",
            (tenant_id, owner_id, doc_id),
        ).fetchone()
        return row is not None

    def _repair_orphan_caches(
        self,
        tracked_paths: set[Path],
        *,
        tenant_id: str,
        owner_id: str,
    ) -> None:
        for temp in self.index_dir.glob(".bm25-*.tmp"):
            temp.unlink(missing_ok=True)
        for path in self.index_dir.glob("*.json"):
            resolved = path.resolve()
            if resolved in tracked_paths:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict) or payload.get("_bm25_index_key") != self.index_key:
                continue
            try:
                payload_scope = _payload_fields(payload)[:2]
            except BM25IndexStoreError:
                continue
            if payload_scope == (tenant_id, owner_id):
                path.unlink(missing_ok=True)
        _fsync_directory(self.index_dir)

    def snapshot(self, *, tenant_id: str, owner_id: str) -> IndexSnapshot:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scope_payload_bytes = int(
                conn.execute(
                    """SELECT COALESCE(SUM(length(payload_json)), 0)
                       FROM bm25_index_documents
                       WHERE index_key = ? AND tenant_id = ? AND owner_id = ?""",
                    (self.index_key, tenant_id, owner_id),
                ).fetchone()[0]
            )
            if scope_payload_bytes > self.max_scope_payload_bytes:
                raise BM25IndexStoreError(
                    "BM25 principal index exceeds payload limit: "
                    f"{scope_payload_bytes} > {self.max_scope_payload_bytes}"
                )
            rows = conn.execute(
                """SELECT * FROM bm25_index_documents
                   WHERE index_key = ? AND tenant_id = ? AND owner_id = ?
                   ORDER BY doc_id""",
                (self.index_key, tenant_id, owner_id),
            ).fetchall()
            externally_deleted: list[sqlite3.Row] = []
            for row in rows:
                index_path = self._safe_index_path(str(row["index_path"]))
                if index_path.exists():
                    continue
                if not self._manifest_exists(
                    conn,
                    str(row["tenant_id"]),
                    str(row["owner_id"]),
                    str(row["doc_id"]),
                ):
                    externally_deleted.append(row)
            if externally_deleted:
                revision = self._revision(conn) + 1
                for row in externally_deleted:
                    conn.execute(
                        """DELETE FROM bm25_index_documents
                           WHERE index_key = ? AND tenant_id = ? AND owner_id = ?
                             AND doc_id = ?""",
                        (
                            self.index_key,
                            row["tenant_id"],
                            row["owner_id"],
                            row["doc_id"],
                        ),
                    )
                self._set_revision(conn, revision)
                rows = conn.execute(
                    """SELECT * FROM bm25_index_documents
                       WHERE index_key = ? AND tenant_id = ? AND owner_id = ?
                       ORDER BY doc_id""",
                    (self.index_key, tenant_id, owner_id),
                ).fetchall()

            documents: list[StoredIndexDocument] = []
            tracked_paths: set[Path] = set()
            for row in rows:
                raw = str(row["payload_json"]).encode("utf-8")
                content_hash = hashlib.sha256(raw).hexdigest()
                if content_hash != str(row["content_hash"]):
                    raise BM25IndexStoreError(
                        f"BM25 durable payload hash mismatch: {row['doc_id']}"
                    )
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise BM25IndexStoreError(
                        f"BM25 durable payload is not an object: {row['doc_id']}"
                    )
                index_path = self._safe_index_path(str(row["index_path"]))
                if not _cache_matches(index_path, content_hash):
                    _atomic_write(index_path, raw)
                tracked_paths.add(index_path)
                self._upsert_manifest(conn, payload, index_path, content_hash)
                documents.append(
                    StoredIndexDocument(
                        payload=payload,
                        index_path=index_path,
                        content_hash=content_hash,
                        updated_revision=int(row["updated_revision"]),
                    )
                )
            revision = self._revision(conn)
            self._repair_orphan_caches(
                tracked_paths,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            conn.commit()
            return IndexSnapshot(revision=revision, documents=tuple(documents))

    def replace_document(self, payload: Payload, index_path: Path) -> IndexMutation:
        tenant_id, owner_id, _device_id, doc_id, _title, _source, _source_path = _payload_fields(
            payload
        )
        return self.mutate_document(
            tenant_id,
            owner_id,
            doc_id,
            index_path,
            lambda _existing: payload,
        )

    def mutate_document(
        self,
        tenant_id: str,
        owner_id: str,
        doc_id: str,
        index_path: Path,
        mutator: PayloadMutator,
    ) -> IndexMutation:
        target = self._safe_index_path(index_path)
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT * FROM bm25_index_documents
                       WHERE index_key = ? AND tenant_id = ? AND owner_id = ?
                         AND doc_id = ?""",
                    (self.index_key, tenant_id, owner_id, doc_id),
                ).fetchone()
                existing: Payload | None = None
                if row is not None:
                    decoded = json.loads(str(row["payload_json"]))
                    if not isinstance(decoded, dict):
                        raise BM25IndexStoreError(
                            f"BM25 durable payload is not an object: {doc_id}"
                        )
                    existing = decoded
                    target = self._safe_index_path(str(row["index_path"]))
                proposed = mutator(existing)
                revision = self._revision(conn)
                if proposed is None:
                    if row is None:
                        conn.commit()
                        return IndexMutation(revision, None, False)
                    target.unlink(missing_ok=True)
                    _fsync_directory(self.index_dir)
                    self._delete_manifest(conn, tenant_id, owner_id, doc_id)
                    conn.execute(
                        """DELETE FROM bm25_index_documents
                           WHERE index_key = ? AND tenant_id = ? AND owner_id = ?
                             AND doc_id = ?""",
                        (self.index_key, tenant_id, owner_id, doc_id),
                    )
                    revision += 1
                    self._set_revision(conn, revision)
                    conn.commit()
                    return IndexMutation(revision, None, True)

                fields = _payload_fields(proposed)
                if fields[0] != tenant_id or fields[1] != owner_id or fields[3] != doc_id:
                    raise BM25IndexStoreError("BM25 mutator changed the document identity")
                revision += 1
                normalized, raw, _content_hash = self._insert_document(
                    conn, proposed, target, revision
                )
                _atomic_write(target, raw)
                self._set_revision(conn, revision)
                conn.commit()
                return IndexMutation(revision, normalized, True)
        except BaseException:
            try:
                self.snapshot(tenant_id=tenant_id, owner_id=owner_id)
            except Exception as repair_exc:  # pragma: no cover - best effort after root error
                self.log.error("BM25 cache rollback repair failed: %s", repair_exc)
            raise


__all__ = [
    "BM25IndexStore",
    "BM25IndexStoreError",
    "IndexMutation",
    "IndexSnapshot",
    "StoredIndexDocument",
]
