"""RAG adapter: jieba 分词 + BM25Okapi 倒排索引（多文档 + 会议统一索引）。

参考 echo/experiments/2026-05-26_pdf_rag_e2e/pdf_rag_e2e.py：
- PDF: pdfplumber 按页解析（pypdf 把 "ChatGPT" 切碎，BM25 召回归零；pdfplumber OK）
- chunk: 600 字 + 100 overlap，跨页保留页码归属
- tokenize: 中英混合，中文 jieba 词 + 英文 [a-z0-9]+ regex 兜底

权威 manifest：SQLite ``bm25_index_state`` / ``bm25_index_documents``
JSON 缓存：~/.echo-demo/rag_index/{doc_id}.json（原子替换、可从 manifest 重建）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.rag.index_store import BM25IndexStore, IndexSnapshot
from app.config import Settings
from app.schemas.rag import RagChunk
from app.security import LEGACY_OWNER_ID, LEGACY_TENANT_ID
from app.security.context import current_principal

log = logging.getLogger("echodesk.rag")

_TENANT_META = "_echodesk_tenant_id"
_OWNER_META = "_echodesk_owner_id"
_MAX_CACHED_SCOPE_SNAPSHOTS = 8


def _scope() -> tuple[str, str]:
    principal = current_principal()
    return principal.tenant_id, principal.owner_id


def _scoped_metadata(metadata: dict[str, str]) -> dict[str, str]:
    tenant_id, owner_id = _scope()
    return {**metadata, _TENANT_META: tenant_id, _OWNER_META: owner_id}


def _belongs_to_scope(chunk: RagChunk, scope: tuple[str, str] | None = None) -> bool:
    tenant_id, owner_id = scope or _scope()
    return (
        chunk.metadata.get(_TENANT_META, LEGACY_TENANT_ID) == tenant_id
        and chunk.metadata.get(_OWNER_META, LEGACY_OWNER_ID) == owner_id
    )


def _tokenize_cn_en(text: str) -> list[str]:
    """jieba 中文词 + 英文/数字串。"""
    import jieba

    text = text.lower()
    tokens: list[str] = []
    for raw in jieba.cut_for_search(text):
        tok = raw.strip()
        if not tok:
            continue
        tokens.append(tok)
        tokens.extend(re.findall(r"[a-z0-9]+", tok))
    # 单字符过滤
    return [t for t in tokens if len(t) >= 1]


class RagError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _IndexedChunk:
    """Deeply immutable in-memory representation of one committed chunk."""

    doc_id: str
    doc_title: str
    chunk_id: str
    text: str
    metadata: tuple[tuple[str, str], ...]
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ScopeIndexSnapshot:
    """One committed principal snapshot that remains valid after LRU eviction."""

    scope: tuple[str, str]
    revision: int
    chunks: tuple[_IndexedChunk, ...]
    ambient_fingerprints: frozenset[tuple[str, str, str]]


def _normalize_captured_at(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


class BM25Rag:
    """实现 ports.rag.RagPort。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._index_dir = Path(settings.rag_index_dir).expanduser()
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._top_k = settings.rag_top_k
        self._chunk_size = settings.rag_pdf_chunk_tokens
        self._chunk_overlap = settings.rag_pdf_chunk_overlap

        self._lock = asyncio.Lock()
        self._memory_lock = threading.RLock()
        self._reload_lock = threading.Lock()
        # One principal can own a large parsed index. Keep a small LRU aligned
        # with the process runtime bound rather than retaining every identity
        # that ever queried this singleton.
        self._snapshot_capacity = max(
            1,
            min(settings.runtime_scope_max_entries, _MAX_CACHED_SCOPE_SNAPSHOTS),
        )
        self._scope_snapshots: OrderedDict[tuple[str, str], _ScopeIndexSnapshot] = OrderedDict()
        self._store = BM25IndexStore(
            settings.db_path,
            self._index_dir,
            logger=log,
            max_scope_payload_bytes=(settings.rag_index_max_payload_bytes_per_principal),
        )
        # Constructor runs under a bound principal during startup/tests. Other
        # scopes are loaded lazily into independent immutable snapshots.
        self._load_index(force=True)

    def _cached_snapshot(self, scope: tuple[str, str]) -> _ScopeIndexSnapshot | None:
        with self._memory_lock:
            return self._scope_snapshots.get(scope)

    def _touch_snapshot(self, snapshot: _ScopeIndexSnapshot) -> None:
        with self._memory_lock:
            if self._scope_snapshots.get(snapshot.scope) is snapshot:
                self._scope_snapshots.move_to_end(snapshot.scope)

    def _remember_snapshot(self, snapshot: _ScopeIndexSnapshot) -> None:
        with self._memory_lock:
            current = self._scope_snapshots.get(snapshot.scope)
            if current is not None and current.revision > snapshot.revision:
                return
            self._scope_snapshots[snapshot.scope] = snapshot
            self._scope_snapshots.move_to_end(snapshot.scope)
            while len(self._scope_snapshots) > self._snapshot_capacity:
                self._scope_snapshots.popitem(last=False)

    def _build_scope_snapshot(
        self,
        scope: tuple[str, str],
        durable_snapshot: IndexSnapshot,
    ) -> _ScopeIndexSnapshot:
        """Parse one committed store snapshot without mutating shared cache state.

        历史问题（2026-05-28，sub-scanner-fix）：原来 corrupt JSON / 半写文件被
        ``except Exception: continue`` 静默吞掉。结果：scanner 刚才报 ``added=N``
        但下次启动 ``list_docs`` 只剩 M < N 个，用户视角"我的文档不见了"且没有任何
        日志。修法：把 corrupt 文件改为 warning 日志（含文件路径 + 异常），并把单
        个 chunk schema 失败也单独 warn，避免整个 doc 因一个脏 chunk 全丢。
        """

        chunks: list[_IndexedChunk] = []
        validated_chunks = 0
        for stored in durable_snapshot.documents:
            data = stored.payload
            doc_id = data.get("doc_id") or stored.index_path.stem
            tenant_id = str(data.get("tenant_id") or LEGACY_TENANT_ID)
            owner_id = str(data.get("owner_id") or LEGACY_OWNER_ID)
            loaded = 0
            raw_chunks = data.get("chunks")
            if not isinstance(raw_chunks, list):
                raw_chunks = []
            for raw_chunk in raw_chunks:
                try:
                    if not isinstance(raw_chunk, dict):
                        raise TypeError("chunk must be an object")
                    metadata = raw_chunk.setdefault("metadata", {})
                    if not isinstance(metadata, dict):
                        raise TypeError("chunk metadata must be an object")
                    metadata.setdefault(_TENANT_META, tenant_id)
                    metadata.setdefault(_OWNER_META, owner_id)
                    chunk = RagChunk(**raw_chunk)
                    raw_tokens = raw_chunk.get("tokens")
                    chunk_tokens = (
                        tuple(str(token) for token in raw_tokens)
                        if isinstance(raw_tokens, list)
                        else tuple(_tokenize_cn_en(chunk.text))
                    )
                    validated_chunks += 1
                    if validated_chunks > self._settings.rag_index_max_chunks_per_principal:
                        raise RagError(
                            "RAG principal index exceeds chunk limit: "
                            f"{validated_chunks} > "
                            f"{self._settings.rag_index_max_chunks_per_principal}"
                        )
                    if not _belongs_to_scope(chunk, scope):
                        continue
                    chunks.append(
                        _IndexedChunk(
                            doc_id=chunk.doc_id,
                            doc_title=chunk.doc_title,
                            chunk_id=chunk.chunk_id,
                            text=chunk.text,
                            metadata=tuple(sorted(chunk.metadata.items())),
                            tokens=chunk_tokens,
                        )
                    )
                    loaded += 1
                except RagError:
                    raise
                except Exception as exc:
                    log.warning(
                        "rag chunk schema mismatch in %s, skipping chunk: %s",
                        stored.index_path.name,
                        exc,
                    )
            if loaded == 0 and raw_chunks:
                log.warning(
                    "rag doc %s loaded 0 / %d chunks (全部 schema 失败)",
                    doc_id,
                    len(raw_chunks),
                )
        ambient_fingerprints: set[tuple[str, str, str]] = set()
        for indexed_chunk in chunks:
            metadata = dict(indexed_chunk.metadata)
            if metadata.get("kind") != "ambient":
                continue
            captured_at = _normalize_captured_at(metadata.get("captured_at", ""))
            text = indexed_chunk.text.strip()
            audio_ref = metadata.get("audio_ref", "").strip()
            ambient_fingerprints.add((captured_at, text, audio_ref))
            # Retention may clear audio_ref from the authoritative DB after a
            # legacy chunk was indexed; captured_at + text remains a valid
            # reconciliation key for that bounded migration path.
            ambient_fingerprints.add((captured_at, text, ""))
        return _ScopeIndexSnapshot(
            scope=scope,
            revision=int(durable_snapshot.revision),
            chunks=tuple(chunks),
            ambient_fingerprints=frozenset(ambient_fingerprints),
        )

    def _snapshot_for_scope(
        self,
        scope: tuple[str, str] | None = None,
        *,
        force: bool = False,
    ) -> _ScopeIndexSnapshot:
        """Return one local immutable snapshot, reloading on global revision change."""

        resolved_scope = scope or _scope()
        cached = self._cached_snapshot(resolved_scope)
        if not force and cached is not None and self._store.current_revision() == cached.revision:
            self._touch_snapshot(cached)
            return cached

        with self._reload_lock:
            cached = self._cached_snapshot(resolved_scope)
            if (
                not force
                and cached is not None
                and self._store.current_revision() == cached.revision
            ):
                self._touch_snapshot(cached)
                return cached
            durable_snapshot = self._store.snapshot(
                tenant_id=resolved_scope[0],
                owner_id=resolved_scope[1],
            )
            if cached is not None and cached.revision == durable_snapshot.revision:
                self._touch_snapshot(cached)
                return cached
            loaded = self._build_scope_snapshot(resolved_scope, durable_snapshot)
            self._remember_snapshot(loaded)
            return loaded

    def _load_index(
        self,
        *,
        force: bool = False,
        scope: tuple[str, str] | None = None,
    ) -> bool:
        resolved_scope = scope or _scope()
        before = self._cached_snapshot(resolved_scope)
        after = self._snapshot_for_scope(resolved_scope, force=force)
        return before is not after

    def _reload_if_stale(self, scope: tuple[str, str] | None = None) -> bool:
        resolved_scope = scope or _scope()
        before = self._cached_snapshot(resolved_scope)
        after = self._snapshot_for_scope(resolved_scope)
        return before is not after

    def _index_file(self, doc_id: str, scope: tuple[str, str] | None = None) -> Path:
        tenant_id, owner_id = scope or _scope()
        if (tenant_id, owner_id) == (LEGACY_TENANT_ID, LEGACY_OWNER_ID):
            return self._index_dir / f"{doc_id}.json"
        scope_hash = hashlib.sha256(f"{tenant_id}\0{owner_id}".encode()).hexdigest()[:16]
        return self._index_dir / f"{scope_hash}--{doc_id}.json"

    def _persist_doc(
        self,
        doc_id: str,
        doc_title: str,
        chunks: list[RagChunk],
        *,
        projection_generation: int | None = None,
    ) -> None:
        principal = current_principal()
        tenant_id, owner_id = principal.tenant_id, principal.owner_id
        payload: dict[str, Any] = {
            "doc_id": doc_id,
            "doc_title": doc_title,
            "tenant_id": tenant_id,
            "owner_id": owner_id,
            "device_id": principal.device_id,
            "chunks": [{**c.model_dump(), "tokens": _tokenize_cn_en(c.text)} for c in chunks],
        }
        if projection_generation is not None:
            payload["projection_generation"] = projection_generation
        target = self._index_file(doc_id)
        if projection_generation is None:
            self._store.replace_document(payload, target)
        else:
            self._store.mutate_document(
                tenant_id,
                owner_id,
                doc_id,
                target,
                lambda _existing: payload,
                projection_generation=projection_generation,
                projection_operation="index",
            )
        self._load_index(force=True, scope=(tenant_id, owner_id))

    @staticmethod
    def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
        out: list[str] = []
        text = re.sub(r"\s+", " ", text).strip()
        i = 0
        while i < len(text):
            sub = text[i : i + size]
            if sub.strip():
                out.append(sub)
            if i + size >= len(text):
                break
            i += size - overlap
        return out

    async def ingest_pdf(
        self,
        file_path: str,
        doc_title: str | None = None,
        *,
        operation_id: str | None = None,
    ) -> str:
        async with self._lock:
            return await asyncio.to_thread(
                self._ingest_pdf_sync,
                file_path,
                doc_title,
                operation_id,
                "upload",
                None,
            )

    async def ingest_file(
        self,
        file_path: str,
        doc_title: str | None = None,
        *,
        source: str = "upload",
        source_path: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """通用文档入库（任意 markitdown 支持的格式 + 文本类）。

        - PDF 走 ingest_pdf（保留页码 metadata）
        - 其他格式走 parsers.parse_to_text → chunk
        - source: "upload"（用户拖入）/ "workspace"（授权工作区扫描）
        - source_path: 工作区扫描时记录原始绝对路径，便于增量去重
        """
        path = Path(file_path).expanduser()
        if not path.exists():
            raise RagError(f"file not found: {file_path}")
        if path.suffix.lower() == ".pdf":
            async with self._lock:
                return await asyncio.to_thread(
                    self._ingest_pdf_sync,
                    str(path),
                    doc_title,
                    operation_id,
                    source,
                    source_path,
                )
        async with self._lock:
            return await asyncio.to_thread(
                self._ingest_generic_sync,
                str(path),
                doc_title,
                source,
                source_path,
                operation_id,
            )

    def _ingest_generic_sync(
        self,
        file_path: str,
        doc_title: str | None,
        source: str,
        source_path: str | None,
        operation_id: str | None,
    ) -> str:
        from app.adapters.rag.parsers import ParseError, parse_to_text

        self._reload_if_stale()
        path = Path(file_path).expanduser()
        title = doc_title or path.stem
        try:
            text = parse_to_text(path)
        except ParseError as e:
            raise RagError(str(e)) from e

        ext = path.suffix.lower().lstrip(".")
        stable_id = (
            hashlib.sha256(operation_id.encode()).hexdigest()[:20]
            if operation_id
            else uuid.uuid4().hex[:12]
        )
        doc_id = f"{ext or 'doc'}-{stable_id}"
        sub_chunks = self._chunk_text(text, self._chunk_size, self._chunk_overlap)
        if not sub_chunks:
            raise RagError(f"parsed but empty content: {path.name}")
        chunks: list[RagChunk] = []
        for j, sub in enumerate(sub_chunks):
            meta: dict[str, str] = _scoped_metadata({"kind": ext or "text", "source": source})
            if source_path:
                meta["source_path"] = source_path
            chunks.append(
                RagChunk(
                    doc_id=doc_id,
                    doc_title=title,
                    chunk_id=f"{doc_id}-c{j:04d}",
                    text=sub,
                    metadata=meta,
                )
            )
        self._persist_doc(doc_id, title, chunks)
        return doc_id

    def _ingest_pdf_sync(
        self,
        file_path: str,
        doc_title: str | None,
        operation_id: str | None,
        source: str,
        source_path: str | None,
    ) -> str:
        self._reload_if_stale()
        try:
            import pdfplumber
        except ImportError as e:
            raise RagError("pdfplumber not installed; pip install pdfplumber") from e

        path = Path(file_path).expanduser()
        if not path.exists():
            raise RagError(f"PDF not found: {file_path}")
        title = doc_title or path.stem
        stable_id = (
            hashlib.sha256(operation_id.encode()).hexdigest()[:20]
            if operation_id
            else uuid.uuid4().hex[:12]
        )
        doc_id = f"pdf-{stable_id}"

        chunks: list[RagChunk] = []
        with pdfplumber.open(str(path)) as pdf:
            for page_idx, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                for j, sub in enumerate(
                    self._chunk_text(text, self._chunk_size, self._chunk_overlap)
                ):
                    metadata = _scoped_metadata(
                        {"page": str(page_idx), "kind": "pdf", "source": source}
                    )
                    if source_path:
                        metadata["source_path"] = source_path
                    chunks.append(
                        RagChunk(
                            doc_id=doc_id,
                            doc_title=title,
                            chunk_id=f"{doc_id}-p{page_idx:03d}-c{j:04d}",
                            text=sub,
                            metadata=metadata,
                        )
                    )

        if not chunks:
            raise RagError(f"PDF parsed but no content: {file_path}")

        self._persist_doc(doc_id, title, chunks)
        return doc_id

    async def ingest_meeting(
        self,
        meeting_id: str,
        transcript: str,
        title: str,
        *,
        projection_generation: int | None = None,
    ) -> str:
        async with self._lock:
            return await asyncio.to_thread(
                self._ingest_meeting_sync,
                meeting_id,
                transcript,
                title,
                projection_generation,
            )

    def _ingest_meeting_sync(
        self,
        meeting_id: str,
        transcript: str,
        title: str,
        projection_generation: int | None = None,
    ) -> str:
        self._reload_if_stale()
        doc_id = f"meeting-{meeting_id}"
        chunks: list[RagChunk] = []
        for j, sub in enumerate(
            self._chunk_text(transcript, self._chunk_size, self._chunk_overlap)
        ):
            metadata = {"kind": "meeting", "meeting_id": meeting_id}
            if projection_generation is not None:
                metadata["projection_generation"] = str(projection_generation)
            chunks.append(
                RagChunk(
                    doc_id=doc_id,
                    doc_title=title,
                    chunk_id=f"{doc_id}-c{j:04d}",
                    text=sub,
                    metadata=_scoped_metadata(metadata),
                )
            )

        if not chunks:
            raise RagError(f"meeting transcript empty: {meeting_id}")

        self._persist_doc(
            doc_id,
            title,
            chunks,
            projection_generation=projection_generation,
        )
        return doc_id

    async def ingest_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        async with self._lock:
            return await asyncio.to_thread(
                self._ingest_ambient_segment_sync,
                text,
                captured_at,
                audio_ref,
                speaker_id,
                speaker_label,
                operation_id,
            )

    def _ingest_ambient_segment_sync(
        self,
        text: str,
        captured_at: str,
        audio_ref: str,
        speaker_id: str | None = None,
        speaker_label: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """按日追加 ambient STT 段（主链路记忆层）。"""
        self._reload_if_stale()
        principal = current_principal()
        day = captured_at[:10].replace("-", "")
        doc_id = f"ambient-{day}"
        title = f"Ambient {captured_at[:10]}"

        def append(payload: dict[str, Any] | None) -> dict[str, Any]:
            raw_chunks: list[Any] = []
            if payload is not None and isinstance(payload.get("chunks"), list):
                raw_chunks = list(payload["chunks"])
            if operation_id is not None:
                raw_chunks = [
                    item
                    for item in raw_chunks
                    if not (
                        isinstance(item, dict)
                        and isinstance(item.get("metadata"), dict)
                        and item["metadata"].get("operation_id") == operation_id
                    )
                ]
            seq = len(raw_chunks)
            metadata: dict[str, str] = {
                "kind": "ambient",
                "source": "ambient",
                "captured_at": captured_at,
                "audio_ref": audio_ref,
                _TENANT_META: principal.tenant_id,
                _OWNER_META: principal.owner_id,
            }
            if speaker_id is not None:
                metadata["speaker_id"] = speaker_id
            if speaker_label is not None:
                metadata["speaker_label"] = speaker_label
            if operation_id is not None:
                metadata["operation_id"] = operation_id
            chunk_suffix = (
                hashlib.sha256(operation_id.encode()).hexdigest()[:16]
                if operation_id is not None
                else f"{seq:04d}"
            )
            chunk = RagChunk(
                doc_id=doc_id,
                doc_title=title,
                chunk_id=f"{doc_id}-c{chunk_suffix}",
                text=text.strip(),
                metadata=metadata,
            )
            raw_chunks.append({**chunk.model_dump(), "tokens": _tokenize_cn_en(chunk.text)})
            return {
                "doc_id": doc_id,
                "doc_title": title,
                "tenant_id": principal.tenant_id,
                "owner_id": principal.owner_id,
                "device_id": principal.device_id,
                "chunks": raw_chunks,
            }

        self._store.mutate_document(
            principal.tenant_id,
            principal.owner_id,
            doc_id,
            self._index_file(doc_id),
            append,
        )
        self._load_index(
            force=True,
            scope=(principal.tenant_id, principal.owner_id),
        )
        return doc_id

    async def contains_ambient_segment(
        self,
        text: str,
        *,
        captured_at: str,
        audio_ref: str,
    ) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._contains_ambient_segment_sync,
                text,
                captured_at,
                audio_ref,
            )

    def _contains_ambient_segment_sync(
        self,
        text: str,
        captured_at: str,
        audio_ref: str,
    ) -> bool:
        """Reconcile v37 rows using fields written by every legacy ambient ingest."""

        expected_text = text.strip()
        expected_audio_ref = audio_ref.strip()
        expected_captured_at = _normalize_captured_at(captured_at)
        snapshot = self._snapshot_for_scope(_scope())
        return (
            expected_captured_at,
            expected_text,
            expected_audio_ref,
        ) in snapshot.ambient_fingerprints

    async def query(self, query: str, *, top_k: int = 5) -> list[RagChunk]:
        async with self._lock:
            return await asyncio.to_thread(self._query_sync, query, top_k)

    def _visible_chunks_for_scope(
        self,
        snapshot: _ScopeIndexSnapshot,
        scope: tuple[str, str],
    ) -> tuple[_IndexedChunk, ...]:
        """Apply the durable meeting projection authority to every read view."""

        meeting_generations: dict[str, int | None] = {}
        for chunk in snapshot.chunks:
            metadata = dict(chunk.metadata)
            if metadata.get("kind") != "meeting":
                continue
            raw_generation = metadata.get("projection_generation")
            if raw_generation is None:
                meeting_generations[chunk.doc_id] = None
            else:
                try:
                    meeting_generations[chunk.doc_id] = int(raw_generation)
                except (TypeError, ValueError):
                    meeting_generations[chunk.doc_id] = -1
        visible_meetings = self._store.visible_meeting_documents(
            scope[0],
            scope[1],
            meeting_generations,
        )
        return tuple(
            chunk
            for chunk in snapshot.chunks
            if dict(chunk.metadata).get("kind") != "meeting" or chunk.doc_id in visible_meetings
        )

    def _query_sync(self, query: str, top_k: int) -> list[RagChunk]:
        scope = _scope()
        snapshot = self._snapshot_for_scope(scope)
        chunks = self._visible_chunks_for_scope(snapshot, scope)
        if not chunks:
            return []
        tokens = _tokenize_cn_en(query)
        if not tokens:
            return []
        from rank_bm25 import BM25Okapi

        scores = BM25Okapi([chunk.tokens for chunk in chunks]).get_scores(tokens)
        # 小语料下 BM25 idf 可能给负权重，但 ranking 仍然有意义 → 不过滤分数
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[: top_k or self._top_k]
        out: list[RagChunk] = []
        for idx, score in ranked:
            chunk = chunks[idx]
            out.append(
                RagChunk(
                    doc_id=chunk.doc_id,
                    doc_title=chunk.doc_title,
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    score=float(score),
                    metadata=dict(chunk.metadata),
                )
            )
        return out

    async def delete(
        self,
        doc_id: str,
        *,
        projection_generation: int | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, doc_id, projection_generation)

    def _delete_sync(self, doc_id: str, projection_generation: int | None = None) -> None:
        self._reload_if_stale()
        tenant_id, owner_id = _scope()
        self._store.mutate_document(
            tenant_id,
            owner_id,
            doc_id,
            self._index_file(doc_id, (tenant_id, owner_id)),
            lambda _payload: None,
            projection_generation=projection_generation,
            projection_operation="delete" if projection_generation is not None else None,
        )
        self._load_index(force=True, scope=(tenant_id, owner_id))

    def stats(self) -> dict[str, Any]:
        """诊断用。"""
        scope = _scope()
        snapshot = self._snapshot_for_scope(scope)
        chunks = self._visible_chunks_for_scope(snapshot, scope)
        doc_ids = {chunk.doc_id for chunk in chunks}
        return {
            "n_chunks": len(chunks),
            "n_docs": len(doc_ids),
            "revision": snapshot.revision,
            "ts": time.time(),
        }

    async def list_docs(self) -> list[dict[str, object]]:
        """所有 doc 摘要：{doc_id, title, source, source_path, kind, n_chunks}。"""
        async with self._lock:
            return await asyncio.to_thread(self._list_docs_sync)

    def _list_docs_sync(self) -> list[dict[str, object]]:
        scope = _scope()
        snapshot = self._snapshot_for_scope(scope)
        agg: dict[str, dict[str, Any]] = {}
        for chunk in self._visible_chunks_for_scope(snapshot, scope):
            metadata = dict(chunk.metadata)
            doc = agg.setdefault(
                chunk.doc_id,
                {
                    "doc_id": chunk.doc_id,
                    "title": chunk.doc_title,
                    "kind": metadata.get("kind", ""),
                    "source": metadata.get("source", "unknown"),
                    "source_path": metadata.get("source_path"),
                    "n_chunks": 0,
                },
            )
            doc["n_chunks"] = int(doc["n_chunks"]) + 1
        return list(agg.values())

    async def find_by_source_path(self, source_path: str) -> str | None:
        """根据 source_path 找 doc_id（workspace 增量去重用）。"""
        async with self._lock:
            return await asyncio.to_thread(self._find_by_source_path_sync, source_path)

    def _find_by_source_path_sync(self, source_path: str) -> str | None:
        snapshot = self._snapshot_for_scope(_scope())
        for chunk in snapshot.chunks:
            if dict(chunk.metadata).get("source_path") == source_path:
                return chunk.doc_id
        return None
