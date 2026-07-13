"""授权工作区扫描器单测：增量 / 删除 / 失败容错。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.rag.workspace_scanner import (
    WorkspaceScanner,
    _fsync_directory,
)
from app.config import Settings


class _PdfPage:
    def extract_text(self) -> str:
        return "retryable PDF workspace content"


class _PdfDocument:
    def __init__(self) -> None:
        self.pages = [_PdfPage()]

    def __enter__(self) -> _PdfDocument:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _make(tmp_path: Path, dirs: list[Path], **kw: object) -> tuple[BM25Rag, WorkspaceScanner]:
    idx = tmp_path / "idx"
    state = tmp_path / "ws_state.json"
    s = Settings(
        rag_index_dir=idx,
        workspace_dirs=",".join(str(d) for d in dirs),
        workspace_state_file=state,
        **kw,  # type: ignore[arg-type]
    )
    rag = BM25Rag(s)
    scanner = WorkspaceScanner(s, rag)
    return rag, scanner


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_empty_dirs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    rag, scanner = _make(tmp_path, [ws])
    r = await scanner.scan()
    assert r.n_total == 0
    assert r.n_added == 0
    assert (await rag.list_docs()) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_transient_iteration_stat_error_preserves_existing_cursor_and_doc(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "temporarily-protected.md"
    source.write_text("authoritative bytes must survive", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    first = await scanner.scan()
    assert first.n_added == 1
    [indexed] = await rag.list_docs()
    indexed_id = str(indexed["doc_id"])

    real_stat = Path.stat

    def transient_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path.name == source.name:
            raise PermissionError("transient workspace permission failure")
        return real_stat(path, *args, **kwargs)

    with patch.object(Path, "stat", transient_stat):
        failed_closed = await scanner.scan()

    assert failed_closed.n_failed == 1
    assert failed_closed.n_removed == 0
    assert source.is_file()
    assert scanner._load_state()[str(source.resolve())].doc_id == indexed_id
    assert [str(doc["doc_id"]) for doc in await rag.list_docs()] == [indexed_id]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_workspace_root_changing_to_regular_file_preserves_cursor_and_doc(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "indexed.md"
    source.write_text("keep the authoritative workspace projection", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    assert (await scanner.scan()).n_added == 1
    [indexed] = await rag.list_docs()
    indexed_id = str(indexed["doc_id"])
    cursor_before = scanner._state_file.read_bytes()

    source.unlink()
    ws.rmdir()
    ws.write_text("transiently not a directory", encoding="utf-8")
    refused = await scanner.scan()

    assert refused.n_failed == 1
    assert refused.n_removed == 0
    assert "root is not a directory" in "\n".join(refused.errors)
    assert scanner._state_file.read_bytes() == cursor_before
    assert [str(doc["doc_id"]) for doc in await rag.list_docs()] == [indexed_id]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_existing_unreadable_state_refuses_scan_without_deleting_index(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "protected-state.md"
    source.write_text("state evidence must survive permission errors", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    assert (await scanner.scan()).n_added == 1
    state_before = scanner._state_file.read_bytes()
    [indexed] = await rag.list_docs()
    real_read_text = Path.read_text

    def deny_state(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == scanner._state_file:
            raise PermissionError("workspace state permission denied")
        return real_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", deny_state):
        refused = await scanner.scan()

    assert refused.n_failed == 1
    assert refused.n_removed == 0
    assert "unreadable" in "\n".join(refused.errors)
    assert scanner._state_file.read_bytes() == state_before
    assert source.is_file()
    assert [doc["doc_id"] for doc in await rag.list_docs()] == [indexed["doc_id"]]


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("corrupt_state", ["{not-json", "[]"])
async def test_existing_corrupt_state_refuses_scan_without_orphan_cleanup(
    tmp_path: Path,
    corrupt_state: str,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "corrupt-state.md"
    source.write_text("authoritative document must remain", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    assert (await scanner.scan()).n_added == 1
    [indexed] = await rag.list_docs()
    scanner._state_file.write_text(corrupt_state, encoding="utf-8")
    corrupt_before = scanner._state_file.read_bytes()

    refused = await scanner.scan()

    assert refused.n_failed == 1
    assert refused.n_removed == 0
    assert scanner._state_file.read_bytes() == corrupt_before
    assert source.is_file()
    assert [doc["doc_id"] for doc in await rag.list_docs()] == [indexed["doc_id"]]


@pytest.mark.unit
@pytest.mark.skipif(os.name == "nt", reason="directory fsync is a POSIX durability fence")
def test_workspace_state_replace_is_followed_by_parent_directory_fsync(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _rag, scanner = _make(tmp_path, [ws])
    events: list[str] = []
    real_replace = os.replace

    def recording_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        real_replace(source, target)
        events.append("replace")

    def recording_directory_fsync(path: Path) -> None:
        assert path == scanner._state_file.parent
        events.append("directory_fsync")

    with (
        patch("app.adapters.rag.workspace_scanner.os.replace", side_effect=recording_replace),
        patch(
            "app.adapters.rag.workspace_scanner._fsync_directory",
            side_effect=recording_directory_fsync,
        ),
    ):
        scanner._save_workspace_state(scanner._load_workspace_state())

    assert events == ["replace", "directory_fsync"]
    assert scanner._state_file.is_file()


@pytest.mark.unit
def test_workspace_directory_fsync_is_a_windows_noop(tmp_path: Path) -> None:
    with (
        patch("app.adapters.rag.workspace_scanner.os.name", "nt"),
        patch("app.adapters.rag.workspace_scanner.os.open") as open_mock,
    ):
        _fsync_directory(tmp_path)

    open_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_indexes_supported_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("alpha workspace 内容", encoding="utf-8")
    (ws / "b.txt").write_text("beta plain text 内容", encoding="utf-8")
    (ws / "c.bin").write_bytes(b"\x00\x01")  # 不支持，应跳过
    sub = ws / "sub"
    sub.mkdir()
    (sub / "d.md").write_text("递归 子目录 gamma", encoding="utf-8")

    rag, scanner = _make(tmp_path, [ws])
    r = await scanner.scan()
    assert r.n_total == 3
    assert r.n_added == 3
    docs = await rag.list_docs()
    assert len(docs) == 3
    assert all(d["source"] == "workspace" for d in docs)
    hits = await rag.query("子目录 gamma")
    assert hits


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_rejects_symlink_that_escapes_authorized_root(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("private content outside the authorized root", encoding="utf-8")
    (ws / "looks-safe.txt").symlink_to(outside)
    rag, scanner = _make(tmp_path, [ws])

    result = await scanner.scan()

    assert result.n_total == 0
    assert result.n_failed == 1
    assert "escapes authorized workspace root" in "\n".join(result.errors)
    assert await rag.list_docs() == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_rejects_file_swapped_to_external_symlink_after_iteration(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "victim.txt"
    source.write_text("authorized bytes", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret must not be indexed", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    canonical = source.resolve()
    real_snapshot = scanner._snapshot_for_ingest
    swapped = False

    def swap_before_open(path: Path) -> tuple[Path, str, int]:
        nonlocal swapped
        if path == canonical and not swapped:
            source.unlink()
            try:
                source.symlink_to(outside)
            except OSError as exc:  # pragma: no cover - Windows without symlink privilege
                pytest.skip(f"symlink unavailable: {exc}")
            swapped = True
        return real_snapshot(path)

    with patch.object(scanner, "_snapshot_for_ingest", side_effect=swap_before_open):
        result = await scanner.scan()

    assert result.n_added == 0
    assert result.n_failed == 1
    assert "snapshot" in "\n".join(result.errors)
    assert await rag.list_docs() == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unknown_workspace_state_schema_fails_closed_without_losing_handles(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    rag, scanner = _make(tmp_path, [ws])
    payload = {
        "schema_version": 999,
        "files": {},
        "pending_cleanup": {
            "doc-must-survive": {
                "doc_id": "doc-must-survive",
                "source_path": "/opaque/future/path",
                "reason": "future",
                "queued_at": 1.0,
            }
        },
    }
    scanner._state_file.write_text(json.dumps(payload), encoding="utf-8")
    before = scanner._state_file.read_bytes()

    refused = await scanner.scan()

    assert refused.n_failed == 1
    assert refused.n_removed == 0
    assert "unsupported workspace state schema" in "\n".join(refused.errors)
    assert scanner._state_file.read_bytes() == before
    assert await rag.list_docs() == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_status_counts_authoritative_workspace_docs_not_stale_scan_state(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "indexed.md"
    source.write_text("authoritative workspace document", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])

    await scanner.scan()
    assert (await scanner.status())["n_indexed"] == 1

    doc_id = str((await rag.list_docs())[0]["doc_id"])
    await rag.delete(doc_id)
    assert (await scanner.status())["n_indexed"] == 0

    repaired = await scanner.scan()
    assert repaired.n_updated == 1
    assert repaired.n_failed == 0
    assert (await scanner.status())["n_indexed"] == 1
    assert [hit.text for hit in await rag.query("authoritative workspace")] == [
        "authoritative workspace document"
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_skips_unchanged_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "stable.md"
    f.write_text("stable content alpha", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])

    r1 = await scanner.scan()
    assert r1.n_added == 1

    with patch.object(
        scanner,
        "_snapshot_for_ingest",
        side_effect=AssertionError("unchanged file must not be copied to a temp snapshot"),
    ):
        r2 = await scanner.scan()
    assert r2.n_added == 0
    assert r2.n_skipped == 1
    assert (await rag.list_docs())[0]["doc_id"] is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_failed_pdf_atomic_commit_does_not_advance_state_and_retries(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    pdf = ws / "retry.pdf"
    pdf.write_bytes(b"%PDF synthetic")
    rag, scanner = _make(tmp_path, [ws])

    with (
        patch("pdfplumber.open", return_value=_PdfDocument()),
        patch.object(
            rag._store,
            "_set_revision",
            side_effect=RuntimeError("simulated manifest commit failure"),
        ),
    ):
        failed = await scanner.scan()

    assert failed.n_added == 0
    assert failed.n_failed == 1
    assert await rag.list_docs() == []
    state_after_failure = scanner._load_state()
    assert str(pdf.resolve()) not in state_after_failure

    with patch("pdfplumber.open", return_value=_PdfDocument()):
        retried = await scanner.scan()

    assert retried.n_added == 1
    assert retried.n_failed == 0
    docs = await rag.list_docs()
    assert len(docs) == 1
    assert docs[0]["source"] == "workspace"
    assert docs[0]["source_path"] == str(pdf.resolve())
    assert str(pdf.resolve()) in scanner._load_state()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_updates_changed_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "evolving.md"
    f.write_text("first version foo", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()

    # 改文件内容（用 mtime 显式设置确保增量逻辑触发，避免 async 中 sleep）
    f.write_text("second version bar baz qux", encoding="utf-8")
    import os

    new_mtime = f.stat().st_mtime + 2.0
    os.utime(f, (new_mtime, new_mtime))

    r = await scanner.scan()
    assert r.n_updated == 1
    docs = await rag.list_docs()
    assert len(docs) == 1
    hits = await rag.query("bar baz")
    assert hits
    hits_old = await rag.query("first version foo")
    assert not any("first version foo" in h.text for h in hits_old)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_removes_deleted_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "ephemeral.md"
    f.write_text("temporary doc 123", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    assert len(await rag.list_docs()) == 1

    f.unlink()
    r = await scanner.scan()
    assert r.n_removed == 1
    assert (await rag.list_docs()) == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_ignores_hidden_dirs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("should be ignored", encoding="utf-8")
    (ws / "visible.md").write_text("visible workspace doc", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    r = await scanner.scan()
    assert r.n_total == 1
    docs = await rag.list_docs()
    assert all(
        "git" not in str(d.get("source_path", "")) or "/.git/" not in str(d.get("source_path", ""))
        for d in docs
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_respects_max_file_size(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    big = ws / "big.txt"
    big.write_text("x" * 200_000, encoding="utf-8")
    small = ws / "small.txt"
    small.write_text("ok small", encoding="utf-8")

    rag, scanner = _make(tmp_path, [ws], workspace_max_file_mb=0.1)  # 100 KB 上限
    r = await scanner.scan()
    assert r.n_total == 1  # 只有 small 通过
    docs = await rag.list_docs()
    assert len(docs) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_scan_status_reports_dirs_and_count(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.md").write_text("alpha", encoding="utf-8")
    _, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    st = await scanner.status()
    assert str(ws) in st["authorized_dirs"]
    assert st["n_indexed"] == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_only_removes_workspace_source(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ws_doc.md").write_text("workspace doc", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()

    # 另外注入一个 upload 来源的 doc
    other = tmp_path / "uploaded.md"
    other.write_text("user uploaded", encoding="utf-8")
    await rag.ingest_file(str(other), source="upload")

    assert len(await rag.list_docs()) == 2
    n = await scanner.clear()
    assert n == 1
    docs = await rag.list_docs()
    assert len(docs) == 1
    assert docs[0]["source"] == "upload"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_removes_authoritative_workspace_orphan_without_state_cursor(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "orphan.md"
    source.write_text("workspace orphan must be clearable", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await rag.ingest_file(
        str(source),
        source="workspace",
        source_path=str(source.resolve()),
        operation_id="workspace-orphan-before-state-save",
    )
    assert not scanner._state_file.exists()

    removed = await scanner.clear()

    assert removed == 1
    assert await rag.list_docs() == []
    state = scanner._load_workspace_state()
    assert state.files == {}
    assert state.pending_cleanup == {}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_legacy_head_sha1_state_upgrades_to_full_digest_schema(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "legacy.md"
    source.write_text("legacy workspace state content", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    key = str(source.resolve())
    stat = source.stat()
    legacy_doc_id = await rag.ingest_file(
        key,
        doc_title=source.stem,
        source="workspace",
        source_path=key,
        operation_id=f"workspace:{key}:legacy-head-sha1:{stat.st_size}",
    )
    scanner._state_file.write_text(
        json.dumps(
            {
                key: {
                    "source_path": key,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "sha1": "legacy-head-sha1",
                    "doc_id": legacy_doc_id,
                    "ingested_at": 1.0,
                }
            }
        ),
        encoding="utf-8",
    )

    result = await scanner.scan()

    assert result.n_updated == 1
    assert result.n_failed == 0
    persisted = json.loads(scanner._state_file.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert persisted["pending_cleanup"] == {}
    assert persisted["files"][key]["digest"].startswith("sha256:")
    assert "sha1" not in persisted["files"][key]
    docs = await rag.list_docs()
    assert len(docs) == 1
    assert docs[0]["doc_id"] != legacy_doc_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_full_digest_detects_tail_only_change_with_same_size_and_mtime(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "tail.txt"
    head = b"x" * (1 << 20)
    source.write_bytes(head + b"TAIL-OLD")
    original_stat = source.stat()
    rag, scanner = _make(tmp_path, [ws])
    ingest = AsyncMock(side_effect=["doc-old", "doc-new"])
    delete = AsyncMock(return_value=None)

    with (
        patch.object(rag, "ingest_file", new=ingest),
        patch.object(rag, "delete", new=delete),
    ):
        first = await scanner.scan()
        source.write_bytes(head + b"TAIL-NEW")
        os.utime(
            source,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        changed = await scanner.scan()

    assert first.n_added == 1
    assert changed.n_updated == 1
    assert changed.n_skipped == 0
    assert changed.n_failed == 0
    assert ingest.await_count == 2
    delete.assert_awaited_once_with("doc-old")
    file_state = scanner._load_workspace_state().files[str(source.resolve())]
    assert file_state.doc_id == "doc-new"
    assert file_state.digest.startswith("sha256:")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_parser_reads_the_same_immutable_snapshot_that_state_hashes(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "race.txt"
    source.write_text("snapshot A", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    real_ingest = rag.ingest_file
    parser_paths: list[Path] = []

    async def mutate_around_parser(file_path: str, *args: Any, **kwargs: Any) -> str:
        parser_path = Path(file_path)
        parser_paths.append(parser_path)
        source.write_text("transient B", encoding="utf-8")
        assert parser_path != source.resolve()
        assert parser_path.read_text(encoding="utf-8") == "snapshot A"
        try:
            return await real_ingest(file_path, *args, **kwargs)
        finally:
            source.write_text("snapshot A", encoding="utf-8")

    with patch.object(rag, "ingest_file", side_effect=mutate_around_parser):
        first = await scanner.scan()
    second = await scanner.scan()

    assert first.n_added == 1 and first.n_failed == 0
    assert second.n_skipped == 1 and second.n_failed == 0
    assert len(parser_paths) == 1
    assert not parser_paths[0].exists()
    hits = await rag.query("snapshot")
    assert [hit.text for hit in hits] == ["snapshot A"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_file_growth_after_iteration_cannot_bypass_snapshot_size_limit(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "growing.txt"
    source.write_text("small", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws], workspace_max_file_mb=0.0001)
    real_snapshot = scanner._snapshot_for_ingest

    def grow_after_iteration(path: Path) -> tuple[Path, str, int]:
        source.write_bytes(b"x" * 1024)
        return real_snapshot(path)

    with patch.object(scanner, "_snapshot_for_ingest", side_effect=grow_after_iteration):
        result = await scanner.scan()

    assert result.n_added == 0
    assert result.n_failed == 1
    assert "grew beyond" in "\n".join(result.errors)
    assert await rag.list_docs() == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_stops_growth_over_limit_without_aborting_other_files(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    growing = ws / "growing.txt"
    growing.write_text("small", encoding="utf-8")
    healthy = ws / "healthy.txt"
    healthy.write_text("healthy", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws], workspace_max_file_mb=0.0001)
    real_snapshot = scanner._snapshot_for_ingest

    def grow_before_snapshot(path: Path) -> tuple[Path, str, int]:
        if path == growing.resolve():
            growing.write_bytes(b"x" * 1024)
        return real_snapshot(path)

    with patch.object(scanner, "_snapshot_for_ingest", side_effect=grow_before_snapshot):
        result = await scanner.scan()

    assert result.n_added == 1
    assert result.n_failed == 1
    assert "during snapshot" in "\n".join(result.errors)
    assert [doc["title"] for doc in await rag.list_docs()] == ["healthy"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_restart_reconciles_orphan_after_ingest_before_state_save_crash(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "crash.txt"
    source.write_text("version before crash", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])

    with patch.object(
        scanner,
        "_save_workspace_state",
        side_effect=OSError("injected final state save crash"),
    ):
        failed = await scanner.scan()
    assert failed.n_added == 1 and failed.n_failed == 1
    assert not scanner._state_file.exists()

    source.write_text("version after restart", encoding="utf-8")
    restarted = WorkspaceScanner(scanner._settings, rag)
    recovered = await restarted.scan()

    assert recovered.n_removed == 1
    assert recovered.n_added == 1
    assert recovered.n_failed == 0
    docs = await rag.list_docs()
    assert len(docs) == 1
    hits = await rag.query("after restart")
    assert [hit.text for hit in hits] == ["version after restart"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_restart_removes_untracked_orphan_when_source_disappeared(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "deleted-after-crash.txt"
    source.write_text("orphaned workspace bytes", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])

    with patch.object(
        scanner,
        "_save_workspace_state",
        side_effect=OSError("injected final state save crash"),
    ):
        failed = await scanner.scan()
    assert failed.n_added == 1 and failed.n_failed == 1
    source.unlink()

    restarted = WorkspaceScanner(scanner._settings, rag)
    recovered = await restarted.scan()

    assert recovered.n_removed == 1
    assert recovered.n_added == 0
    assert recovered.n_failed == 0
    assert await rag.list_docs() == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_missing_file_delete_failure_persists_evidence_and_retries_after_restart(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "missing.md"
    source.write_text("document removed from workspace", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    old_doc_id = str((await rag.list_docs())[0]["doc_id"])
    source.unlink()

    async def fail_after_durable_intent(doc_id: str, **_kwargs: object) -> None:
        persisted = json.loads(scanner._state_file.read_text(encoding="utf-8"))
        assert doc_id in persisted["pending_cleanup"]
        raise RuntimeError("injected missing-file delete failure")

    with patch.object(rag, "delete", side_effect=fail_after_durable_intent):
        failed = await scanner.scan()

    assert failed.n_removed == 0
    assert failed.n_failed == 1
    failed_state = scanner._load_workspace_state()
    assert str(source.resolve()) not in failed_state.files
    cleanup = failed_state.pending_cleanup[old_doc_id]
    assert cleanup.reason == "source_missing"
    assert cleanup.attempts == 1
    assert "injected missing-file delete failure" in cleanup.last_error
    assert len(await rag.list_docs()) == 1

    restarted_rag, restarted_scanner = _make(tmp_path, [ws])
    recovered = await restarted_scanner.scan()

    assert recovered.n_removed == 1
    assert recovered.n_failed == 0
    assert restarted_scanner._load_workspace_state().pending_cleanup == {}
    assert await restarted_rag.list_docs() == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_clear_delete_failure_persists_evidence_and_later_clear_retries(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "clear-me.md"
    source.write_text("workspace document to clear", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    workspace_doc_id = str((await rag.list_docs())[0]["doc_id"])
    upload = tmp_path / "upload.md"
    upload.write_text("independent upload", encoding="utf-8")
    await rag.ingest_file(str(upload), source="upload")

    async def fail_after_durable_intent(doc_id: str, **_kwargs: object) -> None:
        persisted = json.loads(scanner._state_file.read_text(encoding="utf-8"))
        assert doc_id in persisted["pending_cleanup"]
        raise RuntimeError("injected clear delete failure")

    with patch.object(rag, "delete", side_effect=fail_after_durable_intent):
        first_removed = await scanner.clear()

    assert first_removed == 0
    failed_state = scanner._load_workspace_state()
    assert failed_state.files == {}
    cleanup = failed_state.pending_cleanup[workspace_doc_id]
    assert cleanup.reason == "workspace_clear"
    assert cleanup.attempts == 1
    assert "injected clear delete failure" in cleanup.last_error

    second_removed = await scanner.clear()

    assert second_removed == 1
    assert scanner._load_workspace_state().pending_cleanup == {}
    docs = await rag.list_docs()
    assert len(docs) == 1
    assert docs[0]["source"] == "upload"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_update_delete_failure_keeps_old_doc_and_retries_without_orphan(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "update.md"
    source.write_text("old workspace version alpha", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    old_doc_id = str((await rag.list_docs())[0]["doc_id"])
    source.write_text("new workspace version omega", encoding="utf-8")

    async def fail_after_durable_intent(doc_id: str, **_kwargs: object) -> None:
        persisted = json.loads(scanner._state_file.read_text(encoding="utf-8"))
        assert doc_id in persisted["pending_cleanup"]
        raise RuntimeError("injected replacement delete failure")

    with patch.object(rag, "delete", side_effect=fail_after_durable_intent):
        failed = await scanner.scan()

    assert failed.n_updated == 0
    assert failed.n_failed == 1
    failed_state = scanner._load_workspace_state()
    assert failed_state.files[str(source.resolve())].doc_id == old_doc_id
    cleanup = failed_state.pending_cleanup[old_doc_id]
    assert cleanup.reason == "replaced"
    assert cleanup.attempts == 1
    assert "injected replacement delete failure" in cleanup.last_error
    docs_after_failure = await rag.list_docs()
    assert [doc["doc_id"] for doc in docs_after_failure] == [old_doc_id]

    restarted_rag, restarted_scanner = _make(tmp_path, [ws])
    recovered = await restarted_scanner.scan()

    assert recovered.n_updated == 1
    assert recovered.n_failed == 0
    assert restarted_scanner._load_workspace_state().pending_cleanup == {}
    docs = await restarted_rag.list_docs()
    assert len(docs) == 1
    assert docs[0]["doc_id"] != old_doc_id
    assert await restarted_rag.query("version omega")
    old_hits = await restarted_rag.query("version alpha")
    assert not any("version alpha" in hit.text for hit in old_hits)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cancel_during_replacement_delete_always_removes_sensitive_snapshot(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "cancel.txt"
    source.write_text("old private workspace bytes", encoding="utf-8")
    rag, scanner = _make(tmp_path, [ws])
    await scanner.scan()
    source.write_text("new private workspace bytes", encoding="utf-8")

    real_snapshot = scanner._snapshot_for_ingest
    snapshots: list[Path] = []
    delete_entered = asyncio.Event()
    never_release = asyncio.Event()

    def record_snapshot(path: Path) -> tuple[Path, str, int]:
        snapshot = real_snapshot(path)
        snapshots.append(snapshot[0])
        return snapshot

    async def blocking_delete(_doc_id: str, **_kwargs: object) -> None:
        delete_entered.set()
        await never_release.wait()

    with (
        patch.object(scanner, "_snapshot_for_ingest", side_effect=record_snapshot),
        patch.object(rag, "delete", side_effect=blocking_delete),
    ):
        task = asyncio.create_task(scanner.scan())
        await asyncio.wait_for(delete_entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(snapshots) == 1
    assert not snapshots[0].exists()
    persisted = json.loads(scanner._state_file.read_text(encoding="utf-8"))
    assert persisted["pending_cleanup"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_worker_does_not_block_event_loop(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "off-loop.txt"
    source.write_text("snapshot work must stay off the event loop", encoding="utf-8")
    _, scanner = _make(tmp_path, [ws])
    real_snapshot = scanner._snapshot_for_ingest
    worker_entered = threading.Event()
    release_worker = threading.Event()

    def blocking_snapshot(path: Path) -> tuple[Path, str, int]:
        worker_entered.set()
        if not release_worker.wait(timeout=5):
            raise TimeoutError("test did not release snapshot worker")
        return real_snapshot(path)

    with patch.object(scanner, "_snapshot_for_ingest", side_effect=blocking_snapshot):
        scan_task = asyncio.create_task(scanner.scan())
        try:
            assert await asyncio.wait_for(
                asyncio.to_thread(worker_entered.wait),
                timeout=2,
            )
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)

            await asyncio.wait_for(heartbeat.wait(), timeout=1)
            assert not scan_task.done()
        finally:
            release_worker.set()

        result = await asyncio.wait_for(scan_task, timeout=2)

    assert result.n_added == 1
    assert result.n_failed == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cancel_waits_for_snapshot_worker_and_removes_created_temp(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "cancel-before-worker-return.txt"
    source.write_text("private snapshot bytes", encoding="utf-8")
    _, scanner = _make(tmp_path, [ws])
    real_snapshot = scanner._snapshot_for_ingest
    snapshot_created = threading.Event()
    release_worker = threading.Event()
    snapshots: list[Path] = []

    def create_then_block(path: Path) -> tuple[Path, str, int]:
        created = real_snapshot(path)
        snapshots.append(created[0])
        snapshot_created.set()
        if not release_worker.wait(timeout=5):
            raise TimeoutError("test did not release snapshot worker")
        return created

    with patch.object(scanner, "_snapshot_for_ingest", side_effect=create_then_block):
        scan_task = asyncio.create_task(scanner.scan())
        try:
            assert await asyncio.wait_for(
                asyncio.to_thread(snapshot_created.wait),
                timeout=2,
            )
            scan_task.cancel()
            await asyncio.sleep(0)
            assert not scan_task.done()
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await scan_task

    assert len(snapshots) == 1
    assert not snapshots[0].exists()


@pytest.mark.unit
def test_snapshot_cleanup_failure_preserves_original_write_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    source = ws / "write-failure.txt"
    source.write_text("sensitive bytes must not hide the primary failure", encoding="utf-8")
    _, scanner = _make(tmp_path, [ws])
    real_mkstemp = tempfile.mkstemp
    created_temps: list[Path] = []

    def record_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        created_temps.append(Path(name))
        return fd, name

    try:
        with (
            caplog.at_level(logging.WARNING, logger="echodesk.workspace"),
            patch(
                "app.adapters.rag.workspace_scanner.tempfile.mkstemp",
                side_effect=record_mkstemp,
            ),
            patch(
                "app.adapters.rag.workspace_scanner.os.fsync",
                side_effect=OSError("injected snapshot write failure"),
            ),
            patch.object(
                Path,
                "unlink",
                side_effect=OSError("injected snapshot cleanup failure"),
            ),
            pytest.raises(OSError, match="injected snapshot write failure"),
        ):
            scanner._snapshot_for_ingest(source.resolve())
    finally:
        for path in created_temps:
            path.unlink(missing_ok=True)

    assert "workspace snapshot cleanup failed after copy error" in caplog.text
    assert "injected snapshot cleanup failure" in caplog.text
