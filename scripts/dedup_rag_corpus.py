"""幂等地清理 RAG 索引里的重复文档。

背景（2026-05-28 实测）：``GET /rag/docs`` 显示同一份 PDF "褐蚁AI工作站产品手册新"
被入库了 13 次（11×unknown + 1×upload + 1×workspace），同时还有 7 组
"workspace + upload 重复"。BM25 视角下 TF 被重复内容线性放大，召回严重失真。

策略
-----
1. 按 ``(kind, 归一化标题)`` 分组：
   - 归一化标题去掉文件扩展名后缀（"X.pdf" 和 "X" 归一为同一 key）
   - 同 kind 才能合并（meeting/pdf/csv 不互相覆盖）
2. 组内挑一个"权威"doc：
   - source 优先级：``workspace > upload > unknown``
   - source 相同再比 ``n_chunks`` 多的（防止 parse 失败的半截入库覆盖完整入库）
   - 仍并列就 ``doc_id`` 字典序最小（决定性，保证幂等）
3. 其余 doc 调 ``DELETE /rag/docs/{doc_id}`` 删除。
4. ``meeting-*`` / ``ambient-*`` 这类带唯一 ID 的 doc 不在重复组里时不会被动到。

模式
-----
- ``--dry-run``（默认）：打印将删除的 doc 列表 + before/after 预估
- ``--apply``：真正执行删除，并打印 before/after 真实统计

跑法
-----
    python scripts/dedup_rag_corpus.py
    python scripts/dedup_rag_corpus.py --apply
    python scripts/dedup_rag_corpus.py --base-url http://localhost:8769 --apply

约束
-----
- 不重启 backend（API 热调）
- 幂等：同一份输入跑多次结果一致
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8769"

SOURCE_PRIORITY: dict[str, int] = {
    "workspace": 0,
    "upload": 1,
    "meeting": 2,
    "ambient": 3,
    "unknown": 9,
}

KNOWN_EXTS = {
    "pdf",
    "docx",
    "doc",
    "pptx",
    "ppt",
    "xlsx",
    "xls",
    "csv",
    "tsv",
    "md",
    "markdown",
    "txt",
    "html",
    "htm",
    "rtf",
    "json",
}


def _http_get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_delete(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def normalize_title(title: str) -> str:
    """去掉一个已知扩展名后缀，全角空白折叠为半角。"""
    t = (title or "").strip()
    lower = t.lower()
    for ext in KNOWN_EXTS:
        suffix = "." + ext
        if lower.endswith(suffix):
            t = t[: -len(suffix)]
            break
    return " ".join(t.split())


def fetch_docs(base_url: str) -> list[dict[str, Any]]:
    data = _http_get_json(f"{base_url}/rag/docs")
    docs = data.get("docs") or []
    if not isinstance(docs, list):
        raise RuntimeError(f"unexpected /rag/docs payload: {data!r}")
    return list(docs)


def summarize(docs: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, int] = defaultdict(int)
    chunks = 0
    for d in docs:
        by_source[str(d.get("source", "unknown"))] += 1
        chunks += int(d.get("n_chunks", 0) or 0)
    return {
        "n_docs": len(docs),
        "n_chunks": chunks,
        "by_source": dict(sorted(by_source.items())),
    }


def _doc_sort_key(d: dict[str, Any]) -> tuple[int, int, str]:
    source = str(d.get("source", "unknown"))
    prio = SOURCE_PRIORITY.get(source, 99)
    n_chunks_neg = -int(d.get("n_chunks", 0) or 0)
    doc_id = str(d.get("doc_id", ""))
    return (prio, n_chunks_neg, doc_id)


def plan_deletions(
    docs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], list[dict[str, Any]]]]]:
    """返回 (待删 docs, 每组 [keeper, [losers]] 报告)。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        kind = str(d.get("kind", "") or "")
        key = (kind, normalize_title(str(d.get("title", "") or "")))
        groups[key].append(d)

    to_delete: list[dict[str, Any]] = []
    group_report: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for key, members in groups.items():
        if len(members) <= 1:
            continue
        kind, _ = key
        # ambient/meeting 同 (kind, title) 也不应跨 doc_id 合并：它们是按时间或会议 ID
        # 切片的，重名只是巧合。我们只对"内容型" kind 去重。
        if kind in {"ambient", "meeting"}:
            continue
        ordered = sorted(members, key=_doc_sort_key)
        keeper = ordered[0]
        losers = ordered[1:]
        to_delete.extend(losers)
        group_report.append((keeper, losers))
    return to_delete, group_report


def print_plan(
    before: dict[str, Any],
    group_report: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    to_delete: list[dict[str, Any]],
) -> None:
    print("=== before ===")
    print(f"  n_docs   = {before['n_docs']}")
    print(f"  n_chunks = {before['n_chunks']}")
    print(f"  by_source= {before['by_source']}")
    print()
    print(f"=== 重复组：{len(group_report)} 组，待删 {len(to_delete)} 个 doc ===")
    for keeper, losers in group_report:
        print(
            f"  [keep] {keeper['doc_id']:30s} "
            f"kind={keeper.get('kind', ''):6s} "
            f"src={keeper.get('source', ''):10s} "
            f"n={keeper.get('n_chunks', 0):4d} "
            f"title={keeper.get('title', '')!r}"
        )
        for d in losers:
            print(
                f"  [del]  {d['doc_id']:30s} "
                f"kind={d.get('kind', ''):6s} "
                f"src={d.get('source', ''):10s} "
                f"n={d.get('n_chunks', 0):4d} "
                f"title={d.get('title', '')!r}"
            )


def apply_deletions(base_url: str, to_delete: list[dict[str, Any]]) -> tuple[int, int]:
    ok = 0
    fail = 0
    for d in to_delete:
        doc_id = str(d.get("doc_id", ""))
        if not doc_id:
            continue
        status, body = _http_delete(f"{base_url}/rag/docs/{doc_id}")
        if 200 <= status < 300:
            ok += 1
            print(f"  DEL {doc_id} -> {status}")
        else:
            fail += 1
            print(f"  DEL {doc_id} -> {status} {body!r}", file=sys.stderr)
    return ok, fail


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--apply", action="store_true", help="真删（默认 dry-run）")
    args = p.parse_args()

    try:
        docs = fetch_docs(args.base_url)
    except Exception as e:
        print(f"fetch /rag/docs failed: {e}", file=sys.stderr)
        return 1

    before = summarize(docs)
    to_delete, group_report = plan_deletions(docs)

    print_plan(before, group_report, to_delete)

    if not to_delete:
        print("\n没有重复，无事可做。")
        return 0

    if not args.apply:
        print("\nDRY RUN — 加 --apply 真删")
        return 0

    print("\n=== 执行删除 ===")
    ok, fail = apply_deletions(args.base_url, to_delete)
    print(f"\n删除完成：{ok} 成功 / {fail} 失败")

    after_docs = fetch_docs(args.base_url)
    after = summarize(after_docs)
    print("\n=== after ===")
    print(f"  n_docs   = {after['n_docs']}")
    print(f"  n_chunks = {after['n_chunks']}")
    print(f"  by_source= {after['by_source']}")
    print(f"\nΔ docs   = {after['n_docs'] - before['n_docs']:+d}")
    print(f"Δ chunks = {after['n_chunks'] - before['n_chunks']:+d}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
