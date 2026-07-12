"""BM25Rag adapter 单测：tokenize / chunk / 持久化 / query。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
import threading
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from app.adapters.rag import BM25Rag, RagError
from app.adapters.rag.bm25 import _IndexedChunk, _ScopeIndexSnapshot, _tokenize_cn_en
from app.adapters.rag.index_store import BM25IndexStoreError
from app.adapters.repo.migrator import run_migrations
from app.config import Settings
from app.security import Principal
from app.security.context import bind_principal, reset_principal


class _PdfPage:
    def extract_text(self) -> str:
        return "atomic PDF workspace content"


class _PdfDocument:
    def __init__(self) -> None:
        self.pages = [_PdfPage()]

    def __enter__(self) -> _PdfDocument:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(rag_index_dir=tmp_path)


def _process_ingest_meeting(
    db_path: str,
    index_dir: str,
    meeting_id: str,
    transcript: str,
) -> None:
    settings = Settings(
        db_path=Path(db_path),
        rag_index_dir=Path(index_dir),
        _env_file=None,  # type: ignore[call-arg]
    )
    asyncio.run(BM25Rag(settings).ingest_meeting(meeting_id, transcript, meeting_id))


@pytest.mark.unit
def test_tokenize_handles_chinese_and_english() -> None:
    tokens = _tokenize_cn_en("ChatGPT 是 OpenAI 的产品")
    joined = " ".join(tokens)
    assert "chatgpt" in joined
    assert "openai" in joined
    assert "产品" in joined or "产" in joined


@pytest.mark.unit
def test_tokenize_handles_numbers_and_units() -> None:
    tokens = _tokenize_cn_en("Nvidia 8x H100 集群")
    assert "8x" in tokens or "8" in tokens
    assert "h100" in tokens or "nvidia" in tokens or "h" in tokens


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_meeting_and_query(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    transcript = (
        "今天讨论了 Echo 项目的 demo 计划。会议纪要功能已经接通。"
        "下一步计划接入跨会议 RAG 检索。Nvidia H100 集群在 heyi-bj。"
    )
    doc_id = await rag.ingest_meeting("m001", transcript, "demo 计划会")
    assert doc_id.startswith("meeting-")

    hits = await rag.query("Nvidia H100", top_k=3)
    assert hits, "should find at least one hit"
    assert any("H100" in h.text or "h100" in h.text.lower() for h in hits)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_query_empty_index_returns_empty(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    assert await rag.query("anything") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_removes_chunks(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    doc_id = await rag.ingest_meeting("m001", "测试内容。MeetMe 是一个产品。", "t")
    assert rag.stats()["n_chunks"] >= 1
    await rag.delete(doc_id)
    assert rag.stats()["n_chunks"] == 0
    assert await rag.query("MeetMe") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_meeting_projection_generation_fences_both_race_directions(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id,
                rag_projection_state, rag_projection_generation)
               VALUES ('race', 'finalized', '2026-07-12T00:00:00+00:00',
                       'tenant-a', 'device-a', 'owner-a', 'indexed', 2)"""
        )

    rag = BM25Rag(settings)
    token = bind_principal(principal)
    try:
        await rag.ingest_meeting(
            "race",
            "new generation survives stale delete",
            "generation 2",
            projection_generation=2,
        )
        await rag.delete("meeting-race", projection_generation=1)
        assert [doc["doc_id"] for doc in await rag.list_docs()] == ["meeting-race"]
        assert [hit.text for hit in await rag.query("survives")] == [
            "new generation survives stale delete"
        ]

        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                """UPDATE meetings
                   SET rag_projection_state = 'delete_pending',
                       rag_projection_generation = 3
                   WHERE id = 'race' AND tenant_id = 'tenant-a' AND owner_id = 'owner-a'"""
            )
        await rag.delete("meeting-race", projection_generation=3)
        await rag.ingest_meeting(
            "race",
            "stale finalize must not resurrect",
            "generation 2 stale",
            projection_generation=2,
        )
        assert await rag.list_docs() == []
        assert await rag.query("resurrect") == []
    finally:
        reset_principal(token)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failed_meeting_delete_is_immediately_query_invisible_and_scope_safe(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")
    with sqlite3.connect(settings.db_path) as conn:
        conn.executemany(
            """INSERT INTO meetings
               (id, state, started_at, tenant_id, device_id, owner_id,
                rag_projection_state, rag_projection_generation)
               VALUES ('shared', 'finalized', '2026-07-12T00:00:00+00:00',
                       ?, ?, ?, 'indexed', 1)""",
            [
                (principal_a.tenant_id, principal_a.device_id, principal_a.owner_id),
                (principal_b.tenant_id, principal_b.device_id, principal_b.owner_id),
            ],
        )

    rag = BM25Rag(settings)
    token_a = bind_principal(principal_a)
    try:
        await rag.ingest_meeting(
            "shared",
            "alpha meeting secret",
            "A",
            projection_generation=1,
        )
    finally:
        reset_principal(token_a)
    token_b = bind_principal(principal_b)
    try:
        await rag.ingest_meeting(
            "shared",
            "beta meeting evidence",
            "B",
            projection_generation=1,
        )
    finally:
        reset_principal(token_b)

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            """UPDATE meetings
               SET rag_projection_state = 'delete_failed',
                   rag_projection_generation = 2,
                   rag_projection_error = 'disk unavailable'
               WHERE id = 'shared' AND tenant_id = 'tenant-a' AND owner_id = 'owner-a'"""
        )

    token_a = bind_principal(principal_a)
    try:
        # The generation-1 file still exists physically, but the committed
        # delete intent is the read authority and therefore fails closed.
        assert await rag.list_docs() == []
        assert rag.stats()["n_docs"] == 0
        assert rag.stats()["n_chunks"] == 0
        assert await rag.query("alpha secret") == []
    finally:
        reset_principal(token_a)
    token_b = bind_principal(principal_b)
    try:
        assert [doc["doc_id"] for doc in await rag.list_docs()] == ["meeting-shared"]
        assert rag.stats()["n_docs"] == 1
        assert [hit.text for hit in await rag.query("beta evidence")] == ["beta meeting evidence"]
    finally:
        reset_principal(token_b)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_malformed_meeting_projection_generation_fails_closed_on_every_read(
    tmp_path: Path,
) -> None:
    rag = BM25Rag(_settings(tmp_path))
    scope = ("legacy-local", "legacy-local")
    snapshot = _ScopeIndexSnapshot(
        scope=scope,
        revision=1,
        chunks=(
            _IndexedChunk(
                doc_id="meeting-corrupt",
                doc_title="corrupt",
                chunk_id="meeting-corrupt-c0000",
                text="must remain invisible",
                metadata=(
                    ("kind", "meeting"),
                    ("projection_generation", cast(str, ["not", "scalar"])),
                ),
                tokens=("must", "remain", "invisible"),
            ),
        ),
        ambient_fingerprints=frozenset(),
    )
    with (
        patch.object(rag, "_snapshot_for_scope", return_value=snapshot),
        patch.object(rag._store, "visible_meeting_documents", return_value=set()),
    ):
        assert await rag.query("invisible") == []
        assert await rag.list_docs() == []
        assert rag.stats()["n_docs"] == 0
        assert rag.stats()["n_chunks"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_direct_bm25_adapter_before_migrations_keeps_upgrade_chain_valid(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "echo.db",
        rag_index_dir=tmp_path / "rag",
        _env_file=None,  # type: ignore[call-arg]
    )
    BM25Rag(settings)

    result = await run_migrations(settings.db_path)

    assert result.errors == []
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 38
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'bm25_document_projection_fences'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reingest_meeting_replaces_old_chunks(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    await rag.ingest_meeting("m001", "version A content", "t")
    await rag.ingest_meeting("m001", "version B different stuff", "t")
    # 只应保留 version B 的内容
    hits_a = await rag.query("version A content")
    hits_b = await rag.query("version B different stuff")
    assert not hits_a or all("version A" not in h.text for h in hits_a)
    assert hits_b


@pytest.mark.asyncio
@pytest.mark.unit
async def test_reload_from_disk_preserves_index(tmp_path: Path) -> None:
    rag1 = BM25Rag(_settings(tmp_path))
    await rag1.ingest_meeting("m001", "持久化测试 content X", "t")
    assert rag1.stats()["n_chunks"] >= 1

    # 新实例读同目录
    rag2 = BM25Rag(_settings(tmp_path))
    hits = await rag2.query("持久化")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_nonexistent_pdf_raises(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    with pytest.raises(RagError):
        await rag.ingest_pdf("/nonexistent/path.pdf")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_ambient_segment_appends_by_day(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    doc_id = await rag.ingest_ambient_segment(
        "刚才讨论了 Q3 预算",
        captured_at="2026-05-27T10:00:00+00:00",
        audio_ref="/tmp/a.wav",
    )
    assert doc_id == "ambient-20260527"
    doc_id2 = await rag.ingest_ambient_segment(
        "补充一句关于 Nvidia",
        captured_at="2026-05-27T10:06:00+00:00",
        audio_ref="/tmp/b.wav",
    )
    assert doc_id2 == doc_id
    hits = await rag.query("Nvidia 预算")
    assert hits
    assert hits[0].metadata.get("kind") == "ambient"

    assert await rag.contains_ambient_segment(
        "刚才讨论了 Q3 预算",
        captured_at="2026-05-27T10:00:00Z",
        audio_ref="/tmp/a.wav",
    )
    # Retention-cleared rows intentionally match without rescanning every
    # chunk; the immutable snapshot owns an O(1) reconciliation index.
    assert await rag.contains_ambient_segment(
        "刚才讨论了 Q3 预算",
        captured_at="2026-05-27T10:00:00+00:00",
        audio_ref="",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_ambient_segment_replay_is_idempotent_by_operation_id(
    tmp_path: Path,
) -> None:
    rag = BM25Rag(_settings(tmp_path))
    kwargs = {
        "captured_at": "2026-05-27T10:00:00+00:00",
        "audio_ref": "/tmp/replay.wav",
        "operation_id": "ambient-segment:42",
    }

    await rag.ingest_ambient_segment("旧的孔雀石内容", **kwargs)
    await rag.ingest_ambient_segment("新的孔雀石内容", **kwargs)

    ambient = next(doc for doc in await rag.list_docs() if doc["doc_id"] == "ambient-20260527")
    assert ambient["n_chunks"] == 1
    hits = await rag.query("孔雀石", top_k=5)
    assert [hit.text for hit in hits] == ["新的孔雀石内容"]
    assert hits[0].metadata["operation_id"] == "ambient-segment:42"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_single_rag_instance_isolates_same_doc_ids_between_principals(tmp_path: Path) -> None:
    rag = BM25Rag(_settings(tmp_path))
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")

    token_a = bind_principal(principal_a)
    try:
        doc_a = await rag.ingest_meeting("shared", "alpha confidential roadmap", "A")
        assert [hit.text for hit in await rag.query("alpha confidential")] == [
            "alpha confidential roadmap"
        ]
    finally:
        reset_principal(token_a)

    token_b = bind_principal(principal_b)
    try:
        doc_b = await rag.ingest_meeting("shared", "beta private budget", "B")
        assert doc_b == doc_a == "meeting-shared"
        snapshot_b = rag._scope_snapshots[("tenant-b", "owner-b")]
        assert {chunk.doc_title for chunk in snapshot_b.chunks} == {"B"}
        hits = await rag.query("alpha confidential")
        assert all("alpha" not in hit.text for hit in hits)
        assert [doc["title"] for doc in await rag.list_docs()] == ["B"]
        assert len(list(tmp_path.glob("*--meeting-shared.json"))) == 2
        await rag.delete(doc_b)
        assert await rag.list_docs() == []
    finally:
        reset_principal(token_b)

    token_a = bind_principal(principal_a)
    try:
        assert [doc["title"] for doc in await rag.list_docs()] == ["A"]
        snapshot_a = rag._scope_snapshots[("tenant-a", "owner-a")]
        assert {chunk.doc_title for chunk in snapshot_a.chunks} == {"A"}
        assert [hit.text for hit in await rag.query("alpha confidential")] == [
            "alpha confidential roadmap"
        ]
    finally:
        reset_principal(token_a)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_stats_cannot_swap_scope_between_query_reload_and_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        runtime_scope_max_entries=1,
        _env_file=None,  # type: ignore[call-arg]
    )
    rag = BM25Rag(settings)
    principal_a = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    principal_b = Principal("tenant-b", "device-b", "owner-b", "session-b", "public")
    scope_a = (principal_a.tenant_id, principal_a.owner_id)
    scope_b = (principal_b.tenant_id, principal_b.owner_id)

    token_a = bind_principal(principal_a)
    try:
        await rag.ingest_meeting("a", "alpha phoenix evidence", "A")
    finally:
        reset_principal(token_a)
    token_b = bind_principal(principal_b)
    try:
        await rag.ingest_meeting("b", "beta tiger evidence", "B")
    finally:
        reset_principal(token_b)

    original_snapshot_for_scope = rag._snapshot_for_scope
    query_snapshot_ready = threading.Event()
    resume_query = threading.Event()

    def interleaved_snapshot(
        scope: tuple[str, str] | None = None,
        *,
        force: bool = False,
    ) -> _ScopeIndexSnapshot:
        snapshot = original_snapshot_for_scope(scope, force=force)
        if snapshot.scope == scope_a:
            query_snapshot_ready.set()
            if not resume_query.wait(5):
                raise TimeoutError("query snapshot interleave timed out")
        return snapshot

    monkeypatch.setattr(rag, "_snapshot_for_scope", interleaved_snapshot)

    token_a = bind_principal(principal_a)
    try:
        query_task = asyncio.create_task(rag.query("phoenix"))
    finally:
        reset_principal(token_a)

    try:
        assert await asyncio.to_thread(query_snapshot_ready.wait, 5)
        token_b = bind_principal(principal_b)
        try:
            stats_b = rag.stats()
        finally:
            reset_principal(token_b)
        assert tuple(rag._scope_snapshots) == (scope_b,)
    finally:
        resume_query.set()
        hits_a = await asyncio.wait_for(query_task, timeout=2)

    assert stats_b["n_docs"] == 1
    assert [hit.text for hit in hits_a] == ["alpha phoenix evidence"]


@pytest.mark.unit
def test_principal_payload_budget_rejects_before_manifest_or_cache_commit(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        rag_index_max_payload_bytes_per_principal=1024 * 1024,
        _env_file=None,  # type: ignore[call-arg]
    )
    rag = BM25Rag(settings)
    payload = {
        "doc_id": "oversize",
        "doc_title": "Oversize",
        "tenant_id": "legacy-local",
        "owner_id": "legacy-local",
        "device_id": "legacy-local",
        "chunks": [
            {
                "doc_id": "oversize",
                "doc_title": "Oversize",
                "chunk_id": "oversize-c0000",
                "text": "x" * (1024 * 1024),
                "metadata": {},
                "tokens": [],
            }
        ],
    }

    with pytest.raises(BM25IndexStoreError, match="payload limit"):
        rag._store.replace_document(payload, rag._index_file("oversize"))

    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bm25_index_documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 0
    assert list(settings.rag_index_dir.glob("*.json")) == []


@pytest.mark.unit
def test_principal_chunk_budget_fails_closed_without_swapping_partial_memory(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        rag_index_max_chunks_per_principal=100,
        _env_file=None,  # type: ignore[call-arg]
    )
    rag = BM25Rag(settings)
    payload = {
        "doc_id": "too-many-chunks",
        "doc_title": "Too many chunks",
        "tenant_id": "legacy-local",
        "owner_id": "legacy-local",
        "device_id": "legacy-local",
        "chunks": [
            {
                "doc_id": "too-many-chunks",
                "doc_title": "Too many chunks",
                "chunk_id": f"too-many-chunks-c{index:04d}",
                "text": f"chunk {index}",
                "metadata": {},
                "tokens": ["chunk", str(index)],
            }
            for index in range(101)
        ],
    }
    rag._store.replace_document(payload, rag._index_file("too-many-chunks"))
    scope = ("legacy-local", "legacy-local")
    committed_empty = rag._scope_snapshots[scope]

    with pytest.raises(RagError, match="chunk limit"):
        rag.stats()

    assert rag._scope_snapshots[scope] is committed_empty
    assert committed_empty.chunks == ()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rag_manifest_is_owner_scoped_and_reconciles_from_atomic_index(
    tmp_path: Path,
) -> None:
    settings = Settings(rag_index_dir=tmp_path / "rag", db_path=tmp_path / "echo.db")
    assert (await run_migrations(settings.db_path)).errors == []
    rag = BM25Rag(settings)
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    token = bind_principal(principal)
    try:
        doc_id = await rag.ingest_meeting("manifest", "durable searchable content", "Manifest")
    finally:
        reset_principal(token)

    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            """SELECT tenant_id, device_id, owner_id, doc_id, status, index_path, content_hash
               FROM rag_documents"""
        ).fetchone()
    assert row is not None
    assert row[:5] == ("tenant-a", "device-a", "owner-a", doc_id, "ready")
    assert Path(row[5]).is_file()
    assert row[6] == hashlib.sha256(Path(row[5]).read_bytes()).hexdigest()

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("DELETE FROM rag_documents")
    token = bind_principal(principal)
    try:
        BM25Rag(settings)
    finally:
        reset_principal(token)
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pdf_source_metadata_is_written_in_initial_atomic_commit(tmp_path: Path) -> None:
    settings = Settings(rag_index_dir=tmp_path / "rag", db_path=tmp_path / "echo.db")
    assert (await run_migrations(settings.db_path)).errors == []
    rag = BM25Rag(settings)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF synthetic")
    principal = Principal("tenant-a", "device-a", "owner-a", "session-a", "public")
    token = bind_principal(principal)
    try:
        with (
            patch("pdfplumber.open", return_value=_PdfDocument()),
            patch.object(
                rag._store,
                "replace_document",
                wraps=rag._store.replace_document,
            ) as replace_document,
            patch.object(
                rag._store,
                "mutate_document",
                wraps=rag._store.mutate_document,
            ) as mutate_document,
        ):
            doc_id = await rag.ingest_file(
                str(pdf),
                source="workspace",
                source_path="/safe/workspace/source.pdf",
                operation_id="pdf-source-atomic",
            )
        index_path = rag._index_file(doc_id)
    finally:
        reset_principal(token)

    assert replace_document.call_count == 1
    # replace_document delegates to exactly one transactional store mutation;
    # the removed post-commit source-tag path would have made this count two.
    assert mutate_document.call_count == 1
    assert not index_path.with_suffix(index_path.suffix + ".tmp").exists()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert {
        (chunk["metadata"]["source"], chunk["metadata"]["source_path"])
        for chunk in payload["chunks"]
    } == {("workspace", "/safe/workspace/source.pdf")}
    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            """SELECT source, source_path, content_hash FROM rag_documents
               WHERE tenant_id = ? AND owner_id = ? AND doc_id = ?""",
            ("tenant-a", "owner-a", doc_id),
        ).fetchone()
    assert row == (
        "workspace",
        "/safe/workspace/source.pdf",
        hashlib.sha256(index_path.read_bytes()).hexdigest(),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_pdf_initial_commit_failure_leaves_no_index_or_manifest_orphan(
    tmp_path: Path,
) -> None:
    settings = Settings(rag_index_dir=tmp_path / "rag", db_path=tmp_path / "echo.db")
    assert (await run_migrations(settings.db_path)).errors == []
    rag = BM25Rag(settings)
    pdf = tmp_path / "failed.pdf"
    pdf.write_bytes(b"%PDF synthetic")

    with (
        patch("pdfplumber.open", return_value=_PdfDocument()),
        patch.object(
            rag._store,
            "_set_revision",
            side_effect=RuntimeError("simulated manifest commit failure"),
        ),
        pytest.raises(RuntimeError, match="simulated manifest commit failure"),
    ):
        await rag.ingest_file(
            str(pdf),
            source="workspace",
            source_path=str(pdf.resolve()),
            operation_id="pdf-failed-atomic",
        )

    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bm25_index_documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0] == 0
    assert list(settings.rag_index_dir.glob("*.json")) == []
    assert rag.stats()["n_docs"] == 0
    assert rag.stats()["n_chunks"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_initialized_instance_hot_reloads_write_from_spawned_process(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    reader = BM25Rag(settings)
    initial_revision = int(reader.stats()["revision"])

    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_process_ingest_meeting,
        args=(
            str(settings.db_path),
            str(settings.rag_index_dir),
            "process-write",
            "cross process phoenix knowledge",
        ),
    )
    process.start()
    try:
        await asyncio.to_thread(process.join, 20)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)

    hits = await reader.query("phoenix knowledge")
    assert [hit.doc_id for hit in hits] == ["meeting-process-write"]
    assert int(reader.stats()["revision"]) > initial_revision


@pytest.mark.asyncio
@pytest.mark.unit
async def test_two_spawned_writers_are_serialized_without_lost_documents(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    reader = BM25Rag(settings)
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_process_ingest_meeting,
            args=(
                str(settings.db_path),
                str(settings.rag_index_dir),
                f"parallel-{index}",
                f"parallel process document {index}",
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        await asyncio.gather(*(asyncio.to_thread(process.join, 20) for process in processes))
        assert [process.exitcode for process in processes] == [0, 0]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)

    docs = await reader.list_docs()
    assert {str(doc["doc_id"]) for doc in docs} == {
        "meeting-parallel-0",
        "meeting-parallel-1",
    }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_concurrent_instances_keep_all_documents_and_ambient_appends(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    first = BM25Rag(settings)
    second = BM25Rag(settings)

    await asyncio.gather(
        first.ingest_meeting("writer-a", "alpha concurrent document", "A"),
        second.ingest_meeting("writer-b", "beta concurrent document", "B"),
    )
    docs = await first.list_docs()
    assert {str(doc["doc_id"]) for doc in docs} == {
        "meeting-writer-a",
        "meeting-writer-b",
    }

    await asyncio.gather(
        first.ingest_ambient_segment(
            "first ambient append",
            captured_at="2026-07-12T10:00:00+00:00",
            audio_ref="/tmp/first.wav",
        ),
        second.ingest_ambient_segment(
            "second ambient append",
            captured_at="2026-07-12T10:01:00+00:00",
            audio_ref="/tmp/second.wav",
        ),
    )
    ambient = next(doc for doc in await first.list_docs() if doc["doc_id"] == "ambient-20260712")
    assert ambient["n_chunks"] == 2
    texts = {hit.text for hit in await first.query("ambient append", top_k=5)}
    assert {"first ambient append", "second ambient append"}.issubset(texts)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_delete_is_visible_to_already_initialized_peer(tmp_path: Path) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    first = BM25Rag(settings)
    second = BM25Rag(settings)
    doc_id = await first.ingest_meeting("delete-shared", "vanishing zebra", "Delete")
    assert await second.query("vanishing zebra")
    before_delete = int(second.stats()["revision"])

    await first.delete(doc_id)

    assert await second.query("vanishing zebra") == []
    assert all(doc["doc_id"] != doc_id for doc in await second.list_docs())
    assert int(second.stats()["revision"]) > before_delete


@pytest.mark.asyncio
@pytest.mark.unit
async def test_external_projection_cleanup_is_not_resurrected_from_payload_manifest(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    writer = BM25Rag(settings)
    doc_id = await writer.ingest_meeting("external-delete", "external cleanup", "Delete")
    peer = BM25Rag(settings)
    index_path = writer._index_file(doc_id)
    index_path.unlink()
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            """DELETE FROM rag_documents
               WHERE tenant_id = 'legacy-local' AND owner_id = 'legacy-local'
                 AND doc_id = ?""",
            (doc_id,),
        )

    reconciler = BM25Rag(settings)
    assert await reconciler.query("external cleanup") == []
    assert await peer.query("external cleanup") == []
    with sqlite3.connect(settings.db_path) as conn:
        count = conn.execute(
            """SELECT COUNT(*) FROM bm25_index_documents
               WHERE index_key = ? AND doc_id = ?""",
            (str(settings.rag_index_dir.resolve()), doc_id),
        ).fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failed_commit_repairs_json_cache_to_last_committed_revision(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    rag = BM25Rag(settings)
    await rag.ingest_meeting("atomic", "committed old content", "Atomic")
    committed_revision = int(rag.stats()["revision"])

    with (
        patch.object(rag._store, "_set_revision", side_effect=RuntimeError("commit stopped")),
        pytest.raises(RuntimeError, match="commit stopped"),
    ):
        await rag.ingest_meeting("atomic", "uncommitted replacement", "Atomic")

    restarted = BM25Rag(settings)
    old_hits = await restarted.query("committed old content")
    new_hits = await restarted.query("uncommitted replacement")
    assert old_hits and all("uncommitted" not in hit.text for hit in old_hits)
    assert not new_hits or all("uncommitted" not in hit.text for hit in new_hits)
    assert int(restarted.stats()["revision"]) == committed_revision
    assert not list(settings.rag_index_dir.glob(".bm25-*.tmp"))

    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            """SELECT revision FROM bm25_index_state
               WHERE index_key = ?""",
            (str(settings.rag_index_dir.resolve()),),
        ).fetchone()
    assert row == (committed_revision,)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_manifest_rejection_rolls_back_revision_payload_manifest_and_cache(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert (await run_migrations(settings.db_path)).errors == []
    rag = BM25Rag(settings)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("committed first document", encoding="utf-8")
    second.write_text("uncommitted second document", encoding="utf-8")
    shared_source_path = "/workspace/shared-source.txt"
    await rag.ingest_file(
        str(first),
        source="workspace",
        source_path=shared_source_path,
        operation_id="manifest-first",
    )

    def durable_state() -> tuple[object, object, object, object]:
        with sqlite3.connect(settings.db_path) as conn:
            revision = conn.execute(
                "SELECT revision FROM bm25_index_state WHERE index_key = ?",
                (str(settings.rag_index_dir.resolve()),),
            ).fetchall()
            payloads = conn.execute(
                """SELECT tenant_id, owner_id, doc_id, payload_json, content_hash
                   FROM bm25_index_documents WHERE index_key = ? ORDER BY doc_id""",
                (str(settings.rag_index_dir.resolve()),),
            ).fetchall()
            manifests = conn.execute(
                """SELECT tenant_id, owner_id, doc_id, source_path, content_hash
                   FROM rag_documents ORDER BY doc_id"""
            ).fetchall()
        caches = tuple(
            (path.name, path.read_bytes()) for path in sorted(settings.rag_index_dir.glob("*.json"))
        )
        return revision, payloads, manifests, caches

    committed = durable_state()
    with pytest.raises(BM25IndexStoreError, match="manifest rejected"):
        await rag.ingest_file(
            str(second),
            source="workspace",
            source_path=shared_source_path,
            operation_id="manifest-second",
        )
    assert durable_state() == committed
    assert all(
        "uncommitted second" not in hit.text for hit in await rag.query("uncommitted second")
    )


@pytest.mark.unit
def test_legacy_bootstrap_savepoint_never_leaves_manifestless_payload(
    tmp_path: Path,
) -> None:
    settings = Settings(
        rag_index_dir=tmp_path / "rag",
        db_path=tmp_path / "echo.db",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert asyncio.run(run_migrations(settings.db_path)).errors == []
    settings.rag_index_dir.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        payload = {
            "doc_id": f"legacy-{index}",
            "doc_title": f"Legacy {index}",
            "chunks": [
                {
                    "doc_id": f"legacy-{index}",
                    "doc_title": f"Legacy {index}",
                    "chunk_id": f"legacy-{index}-c0000",
                    "text": f"legacy text {index}",
                    "metadata": {
                        "source": "workspace",
                        "source_path": "/workspace/duplicate.txt",
                    },
                }
            ],
        }
        (settings.rag_index_dir / f"legacy-{index}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    rag = BM25Rag(settings)
    assert rag.stats()["n_docs"] == 1
    with sqlite3.connect(settings.db_path) as conn:
        payload_ids = conn.execute(
            """SELECT doc_id FROM bm25_index_documents
               WHERE index_key = ? ORDER BY doc_id""",
            (str(settings.rag_index_dir.resolve()),),
        ).fetchall()
        manifest_ids = conn.execute("SELECT doc_id FROM rag_documents ORDER BY doc_id").fetchall()
    assert payload_ids == manifest_ids == [("legacy-0",)]
