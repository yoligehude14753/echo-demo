"""扫描器静默漏文件回归测试（fix/phase4-scanner-silent-skip）。

用户痛点（2026-05-28 实测）：

  workspace 配置 /Users/.../heyibalabala（8 个文件，~100MB）；scanner 报
  ``total=6 added=6 failed=0``，但 ``GET /rag/docs`` 只见到此前手动 upload 的
  2 个 PDF —— 4 个 < 20MB 的 PDF/pptx/csv 被静默吞了。

根因方向：scanner / BM25Rag / _tag_source_meta 链路上有若干 ``except: pass`` /
``except: continue`` 把异常吞掉但既不打日志也不递增 ``result.n_failed``，从而
用户视角"扫描全成功，实际不在索引里"。

本测试集合的不变量：

  1. **没有"静默成功"**：只要 ``ingest_file`` 抛任何异常，``result.n_failed``
     必须 +1 且 ``result.errors`` 必须包含该文件路径或文件名；同时 echodesk.workspace
     logger 必须 emit 至少一条 warning，warning 文本必须含该文件名。
  2. **每个失败文件单独记账**：mock 让 N 个文件中 K 个 raise，结果必须
     ``n_failed == K`` 且 ``n_added == N - K``，而不是"任一失败 -> 整批失败"。
  3. **_iter_files 单文件出错不连坐**：让某个文件的 stat() 抛 OSError，
     scanner 必须把这文件计入 failed 并继续处理其余文件。
  4. **磁盘 corrupt JSON 不静默丢**：BM25Rag 启动加载时遇到坏 json，
     必须 emit warning（含坏文件路径），不能空丢。
  5. **workspace_max_file_mb 默认 = 100.0**（从 20 提到 100，让常见 PDF
     默认进 RAG）。

约束：``logging.getLogger("echodesk.workspace")`` 与 ``echodesk.rag`` 的 warning
要稳定 emit；caplog 用 ``logger=`` 参数精确锚定，避免被其它 logger 噪声干扰。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from app.adapters.rag import BM25Rag
from app.adapters.rag.workspace_scanner import WorkspaceScanner
from app.config import Settings


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


# ─────────────────────────────────────────────────────────────────────────
# 1. workspace_max_file_mb 默认 = 100.0
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_workspace_max_file_mb_default_is_100() -> None:
    """用户决策（2026-05-28）：20 → 100 让常见中文营销/技术 PDF 默认能进 RAG。

    若回退到 20.0，要么测试坏要么用户的 30-40MB PDF 又被静默 size-skip。
    """
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.workspace_max_file_mb == 100.0


# ─────────────────────────────────────────────────────────────────────────
# 2. K of N 文件失败 → 精确归账到 n_failed，每个失败有日志 + 文件名
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.unit
async def test_ingest_failures_increment_n_failed_with_visible_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """N=5 个文件，K=2 个让 ingest_file raise。

    断言：n_added=3, n_failed=2, errors 至少 2 条且包含坏文件名，
    echodesk.workspace logger 至少 emit 2 条 warning 各自包含坏文件名。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    files = []
    for i in range(5):
        f = ws / f"doc_{i}.md"
        f.write_text(f"内容 alpha {i}", encoding="utf-8")
        files.append(f)

    rag, scanner = _make(tmp_path, [ws])

    bad_names = {"doc_1.md", "doc_3.md"}
    real_ingest = rag.ingest_file

    async def flaky_ingest(file_path: str, *args: Any, **kw: Any) -> str:
        source_name = Path(str(kw.get("source_path") or file_path)).name
        if source_name in bad_names:
            raise RuntimeError(f"模拟 markitdown OCR 子进程崩溃: {source_name}")
        return await real_ingest(file_path, *args, **kw)

    caplog.set_level(logging.WARNING, logger="echodesk.workspace")
    with patch.object(rag, "ingest_file", side_effect=flaky_ingest):
        r = await scanner.scan()

    assert r.n_total == 5, f"_iter_files 应找到 5 个文件: {r}"
    assert r.n_added == 3, f"非坏文件全部应 ingest 成功 (期望 3): {r}"
    assert r.n_failed == 2, f"坏文件必须计入 n_failed (期望 2): {r}; errors={r.errors}"
    assert len(r.errors) >= 2, f"errors 必须含每个坏文件 entry: {r.errors}"

    error_blob = "\n".join(r.errors)
    for bad in bad_names:
        assert bad in error_blob, f"errors 必须含坏文件名 {bad}: {r.errors}"

    workspace_warnings = [
        rec
        for rec in caplog.records
        if rec.name == "echodesk.workspace" and rec.levelno == logging.WARNING
    ]
    assert workspace_warnings, "至少要 emit 一条 echodesk.workspace warning"
    log_blob = "\n".join(rec.getMessage() for rec in workspace_warnings)
    for bad in bad_names:
        assert bad in log_blob, (
            f"每个失败文件必须在 echodesk.workspace warning 里露名 {bad}: {log_blob}"
        )

    docs = await rag.list_docs()
    good_titles = {f"doc_{i}" for i in (0, 2, 4)}
    bad_titles = {f"doc_{i}" for i in (1, 3)}
    actual_titles = {str(d.get("title")) for d in docs}
    assert good_titles <= actual_titles, f"成功文件应进 RAG: {actual_titles}"
    assert not (bad_titles & actual_titles), f"失败文件不应误进 RAG: {actual_titles}"


# ─────────────────────────────────────────────────────────────────────────
# 3. _iter_files 单文件 stat 失败 → 不连坐 + 计入 failed
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.unit
async def test_iter_files_stat_error_does_not_kill_scan(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """让一个文件的 stat() 在 _iter_files 阶段抛 OSError；其余文件必须正常 ingest。

    实现：mock Path.stat 在指定文件名时抛 OSError。这模拟 macOS 受保护目录 /
    stale symlink 等真实场景。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    ok = ws / "ok.md"
    ok.write_text("ok 内容", encoding="utf-8")
    poisoned = ws / "poisoned.md"
    poisoned.write_text("毒 内容", encoding="utf-8")

    _rag, scanner = _make(tmp_path, [ws])

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: Any, **kw: Any) -> Any:
        if self.name == "poisoned.md":
            raise OSError("Permission denied (模拟 macOS protected file)")
        return real_stat(self, *args, **kw)

    caplog.set_level(logging.WARNING, logger="echodesk.workspace")
    with patch.object(Path, "stat", flaky_stat):
        r = await scanner.scan()

    assert r.n_added == 1, f"ok.md 必须正常 ingest: {r}"
    assert r.n_failed >= 1, f"poisoned.md 必须计入 n_failed: {r}"
    assert any("poisoned.md" in e for e in r.errors), f"errors 必须含坏文件: {r.errors}"
    log_blob = "\n".join(
        rec.getMessage() for rec in caplog.records if rec.name == "echodesk.workspace"
    )
    assert "poisoned.md" in log_blob, f"warning 日志必须含坏文件名: {log_blob}"


# ─────────────────────────────────────────────────────────────────────────
# 4. BM25Rag 启动期 corrupt JSON 必须 emit warning（不静默）
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_bm25_load_index_corrupt_json_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """启动加载时遇到坏 json → 必须 warning 出文件名，不能 silent continue。

    用户原始痛点的"重启后 4 个文件不见了"的可见性兜底：哪怕 _tag_source_meta
    某种原因写坏了 json，下次启动也会在日志里看到"哪个文件加载失败"。
    """
    idx = tmp_path / "idx"
    idx.mkdir()
    good = idx / "pdf-goodgoodgood.json"
    good.write_text(
        '{"doc_id": "pdf-good", "doc_title": "ok", "chunks": [{"doc_id": "pdf-good", '
        '"doc_title": "ok", "chunk_id": "c1", "text": "hello world 内容", '
        '"metadata": {"source": "upload"}}]}',
        encoding="utf-8",
    )
    bad = idx / "pdf-badbadbadbad.json"
    bad.write_text("{not a valid json", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="echodesk.rag")
    s = Settings(rag_index_dir=idx, _env_file=None)  # type: ignore[call-arg]
    rag = BM25Rag(s)

    warnings = [
        rec
        for rec in caplog.records
        if rec.name == "echodesk.rag" and rec.levelno == logging.WARNING
    ]
    assert warnings, "corrupt json 必须 emit echodesk.rag warning"
    blob = "\n".join(rec.getMessage() for rec in warnings)
    assert "pdf-badbadbadbad.json" in blob, f"warning 必须含坏文件路径: {blob}"

    # 好文件仍应加载
    assert rag.stats()["n_chunks"] == 1, "好文件应该正常加载，不受坏文件牵连"


# ─────────────────────────────────────────────────────────────────────────
# 5. 全失败场景：所有文件都坏，n_failed == n_total，n_added == 0
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.unit
async def test_all_files_fail_no_silent_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """退化场景：3 个文件全部 ingest 失败 → n_added=0, n_failed=3，且日志能定位所有 3 个。

    这一条专门防 regression：原来代码若 ``result.n_added += 1`` 错放在 try 外面，
    所有文件都报 added=3 failed=0，但实际 0 个进 RAG。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(3):
        (ws / f"bad_{i}.txt").write_text(f"sad {i}", encoding="utf-8")

    rag, scanner = _make(tmp_path, [ws])

    async def always_fail(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("模拟 BM25Rag 内部 chunk_text 异常")

    caplog.set_level(logging.WARNING, logger="echodesk.workspace")
    with patch.object(rag, "ingest_file", side_effect=always_fail):
        r = await scanner.scan()

    assert r.n_total == 3
    assert r.n_added == 0, f"全失败时 n_added 必须 0: {r}"
    assert r.n_failed == 3, f"全失败时 n_failed 必须 == n_total: {r}"
    assert len(r.errors) == 3
    docs = await rag.list_docs()
    assert docs == [], f"全失败时 RAG 不应有任何 workspace doc: {docs}"
