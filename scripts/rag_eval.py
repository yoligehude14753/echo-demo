#!/usr/bin/env python3
"""RAG 评估脚本.

读 ``tests/rag_eval/queries.yaml`` 里的 50 query, 调当前 backend ``/rag/ask``
拿 citations, 计算 Recall@10 / Recall@30 / MRR, 输出 markdown 报告 +
JSON 结果. 支持 ``--baseline tag`` 保存到 ``tests/rag_eval/results/{tag}.json``,
下次跑可以 ``--compare tag`` 比较.

约定:
- ground-truth ``expected_doc_ids`` 是 *doc* 级别(不是 chunk).
- 系统返回 chunk-level citations -> 我们 dedupe by doc_id, 保留首次出现的 rank.
- Recall@K = |hit ∩ expected| / |expected|, hit 用前 K 个 unique doc_id.
- MRR = 1 / rank(首个相关 doc_id, 1-indexed), 无命中则 0.

用法:
    .venv/bin/python scripts/rag_eval.py --baseline baseline_2026-05-28
    .venv/bin/python scripts/rag_eval.py --print-candidates q001  # 只看一个 query
    .venv/bin/python scripts/rag_eval.py --compare baseline_2026-05-28
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_PATH = REPO_ROOT / "tests" / "rag_eval" / "queries.yaml"
RESULTS_DIR = REPO_ROOT / "tests" / "rag_eval" / "results"
DOCS_REPORT_DIR = REPO_ROOT / "docs"

DEFAULT_ENDPOINT = "http://localhost:8769/rag/ask"
DEFAULT_RAG_TOP_K = 50  # 与 backend `_DEFAULT_RAG_TOP_K` 一致
REQUEST_TIMEOUT_S = 180.0  # backend 可能要做 LLM 答复, 但我们只取 meta 头, 保险给宽


def load_queries() -> list[dict[str, Any]]:
    with QUERIES_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise SystemExit(f"queries.yaml 必须是 list, 实际: {type(data)}")
    for q in data:
        for k in ("id", "query", "expected_doc_ids", "category", "difficulty"):
            if k not in q:
                raise SystemExit(f"query 缺字段 {k}: {q}")
    return data


def ask_rag(endpoint: str, question: str, rag_top_k: int) -> tuple[list[str], list[dict[str, Any]]]:
    """调 ``/rag/ask`` 取第一段 SSE 的 meta.citations.

    返回 ``(unique_doc_ids_in_rank_order, raw_citations)``. 后续 LLM 流式 token
    不消费, 立刻关闭连接节约成本.
    """
    payload = json.dumps({"question": question, "rag_top_k": rag_top_k}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    import http.client

    buf = b""
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            # 第一段 SSE event 形如 "data: {json}\n\n" — 读到第一个空行就够了.
            try:
                while b"\n\n" not in buf:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
            except http.client.IncompleteRead as e:
                # 服务端在我们读完前关闭了 chunked stream; partial 已经够取首段 meta
                buf += e.partial or b""
            # 拿到 meta 就够了, 不消费后续 LLM token (后端流式继续生成,
            # 但客户端关闭后会被 Starlette 取消).
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTPError {e.code}: {e.read().decode('utf-8', 'ignore')[:500]}") from e

    payload_str = buf.decode("utf-8", "ignore").split("\n\n", 1)[0]
    if payload_str.startswith("data:"):
        payload_str = payload_str[5:].strip()
    try:
        first_event = json.loads(payload_str)
    except json.JSONDecodeError as e:
        raise SystemExit(f"解析 SSE 首段失败: {e}; 内容前 500 字符: {payload_str[:500]}") from e
    meta = first_event.get("meta", {})
    citations = meta.get("citations", [])
    rag_citations = [c for c in citations if c.get("kind") == "rag"]
    seen: set[str] = set()
    ordered: list[str] = []
    for c in rag_citations:
        did = c.get("doc_id")
        if did and did not in seen:
            seen.add(did)
            ordered.append(did)
    return ordered, rag_citations


def compute_metrics(
    ordered_doc_ids: list[str], expected: list[str], k_values: tuple[int, ...] = (10, 30)
) -> dict[str, Any]:
    expected_set = set(expected)
    if not expected_set:
        return {"recall": {f"@{k}": 0.0 for k in k_values}, "mrr": 0.0, "first_hit_rank": None}
    metrics: dict[str, Any] = {"recall": {}}
    for k in k_values:
        topk = set(ordered_doc_ids[:k])
        hit = len(topk & expected_set)
        metrics["recall"][f"@{k}"] = hit / len(expected_set)
    first_hit_rank = None
    for idx, did in enumerate(ordered_doc_ids, start=1):
        if did in expected_set:
            first_hit_rank = idx
            break
    metrics["mrr"] = (1.0 / first_hit_rank) if first_hit_rank else 0.0
    metrics["first_hit_rank"] = first_hit_rank
    return metrics


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def aggregate(per_query: list[dict[str, Any]]) -> dict[str, Any]:
    if not per_query:
        return {}
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_diff: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for q in per_query:
        by_cat[q["category"]].append(q)
        by_diff[q["difficulty"]].append(q)

    def _agg(rows: list[dict[str, Any]]) -> dict[str, float | int]:
        r10 = [r["metrics"]["recall"]["@10"] for r in rows]
        r30 = [r["metrics"]["recall"]["@30"] for r in rows]
        mrr = [r["metrics"]["mrr"] for r in rows]
        return {
            "count": len(rows),
            "recall_at_10": statistics.fmean(r10) if r10 else 0.0,
            "recall_at_30": statistics.fmean(r30) if r30 else 0.0,
            "mrr": statistics.fmean(mrr) if mrr else 0.0,
            "zero_recall_count": sum(1 for r in r30 if r == 0.0),
        }

    return {
        "overall": _agg(per_query),
        "by_category": {k: _agg(v) for k, v in by_cat.items()},
        "by_difficulty": {k: _agg(v) for k, v in by_diff.items()},
    }


def write_markdown_report(
    out_path: Path,
    tag: str,
    per_query: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    endpoint: str,
    rag_top_k: int,
    baseline_compare: dict[str, Any] | None = None,
) -> None:
    lines: list[str] = []
    lines.append(f"# RAG 评估报告 · {tag}")
    lines.append("")
    lines.append(f"- **endpoint**: `{endpoint}`")
    lines.append(f"- **rag_top_k**: {rag_top_k}")
    lines.append(f"- **总 query 数**: {summary['overall']['count']}")
    lines.append(f"- **生成时间**: {time.strftime('%Y-%m-%dT%H:%M:%S')}")
    lines.append("")

    lines.append("## 总分")
    lines.append("")
    o = summary["overall"]
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| Recall@10 | {fmt_pct(o['recall_at_10'])} |")
    lines.append(f"| Recall@30 | {fmt_pct(o['recall_at_30'])} |")
    lines.append(f"| MRR | {o['mrr']:.4f} |")
    lines.append(f"| 零召回 (recall@30=0) | {o['zero_recall_count']} / {o['count']} |")
    lines.append("")

    if baseline_compare:
        lines.append("## 与 baseline 对比")
        lines.append("")
        bo = baseline_compare["overall"]
        lines.append("| 指标 | baseline | 当前 | 差值 |")
        lines.append("|---|---|---|---|")
        for key, label in (("recall_at_10", "Recall@10"), ("recall_at_30", "Recall@30"), ("mrr", "MRR")):
            base_v = bo[key]
            now_v = o[key]
            delta = now_v - base_v
            sign = "+" if delta >= 0 else ""
            if "recall" in key:
                lines.append(f"| {label} | {fmt_pct(base_v)} | {fmt_pct(now_v)} | {sign}{fmt_pct(delta)} |")
            else:
                lines.append(f"| {label} | {base_v:.4f} | {now_v:.4f} | {sign}{delta:.4f} |")
        lines.append("")

    lines.append("## 按 category 分项")
    lines.append("")
    lines.append("| category | count | Recall@10 | Recall@30 | MRR | 零召回数 |")
    lines.append("|---|---|---|---|---|---|")
    for cat in sorted(summary["by_category"].keys()):
        s = summary["by_category"][cat]
        lines.append(
            f"| {cat} | {s['count']} | {fmt_pct(s['recall_at_10'])} | "
            f"{fmt_pct(s['recall_at_30'])} | {s['mrr']:.4f} | {s['zero_recall_count']} |"
        )
    lines.append("")

    lines.append("## 按 difficulty 分项")
    lines.append("")
    lines.append("| difficulty | count | Recall@10 | Recall@30 | MRR | 零召回数 |")
    lines.append("|---|---|---|---|---|---|")
    for diff in ("easy", "medium", "hard"):
        if diff not in summary["by_difficulty"]:
            continue
        s = summary["by_difficulty"][diff]
        lines.append(
            f"| {diff} | {s['count']} | {fmt_pct(s['recall_at_10'])} | "
            f"{fmt_pct(s['recall_at_30'])} | {s['mrr']:.4f} | {s['zero_recall_count']} |"
        )
    lines.append("")

    failures = [q for q in per_query if q["metrics"]["recall"]["@30"] == 0.0]
    lines.append(f"## 失败 query (Recall@30 = 0) · 共 {len(failures)} 条")
    lines.append("")
    if not failures:
        lines.append("(无)")
    else:
        for q in failures:
            lines.append(f"### {q['id']} · {q['category']} · {q['difficulty']}")
            lines.append("")
            lines.append(f"- **query**: `{q['query']}`")
            lines.append(f"- **expected_doc_ids**: `{q['expected_doc_ids']}`")
            lines.append(f"- **got top10**: `{q['top_doc_ids'][:10]}`")
            if q.get("notes"):
                lines.append(f"- **notes**: {q['notes']}")
            lines.append("")

    lines.append("## 全部 query 明细 (按 id 排序)")
    lines.append("")
    lines.append("| id | category | difficulty | Recall@10 | Recall@30 | first_hit_rank |")
    lines.append("|---|---|---|---|---|---|")
    for q in per_query:
        m = q["metrics"]
        rank = m.get("first_hit_rank")
        rank_s = str(rank) if rank else "—"
        lines.append(
            f"| {q['id']} | {q['category']} | {q['difficulty']} | "
            f"{fmt_pct(m['recall']['@10'])} | {fmt_pct(m['recall']['@30'])} | {rank_s} |"
        )

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument(
        "--rag-top-k",
        type=int,
        default=DEFAULT_RAG_TOP_K,
        help="传给 /rag/ask 的 rag_top_k, 默认 50 (与 backend 默认一致)",
    )
    p.add_argument("--baseline", default=None, help="存到 results/{tag}.json, 同时写报告 docs/rag_eval_{tag}.md")
    p.add_argument(
        "--compare",
        default=None,
        help="读 results/{tag}.json 作为基线, 报告里增加对比表",
    )
    p.add_argument("--query-id", default=None, help="只跑指定 id 的 query (调试用)")
    p.add_argument(
        "--print-candidates",
        default=None,
        help="只对指定 query_id 打印 top-30 候选 doc_id, 不计算指标 (人工筛 ground-truth)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只跑前 N 个 query, 调试用",
    )
    p.add_argument("--no-report", action="store_true", help="不写 markdown 报告 (仅打印总分)")
    args = p.parse_args()

    queries = load_queries()
    if args.query_id:
        queries = [q for q in queries if q["id"] == args.query_id]
    if args.print_candidates:
        target = next((q for q in queries if q["id"] == args.print_candidates), None)
        if not target:
            print(f"未找到 query id={args.print_candidates}", file=sys.stderr)
            return 1
        doc_ids, raw = ask_rag(args.endpoint, target["query"], args.rag_top_k)
        print(f"[{target['id']}] {target['query']!r}")
        print(f"  期望: {target['expected_doc_ids']}")
        print(f"  系统返回 unique doc_ids (rank 序, 前 30):")
        for i, did in enumerate(doc_ids[:30], start=1):
            print(f"    {i:2d}. {did}")
        return 0
    if args.limit:
        queries = queries[: args.limit]

    print(f"加载 {len(queries)} 个 query, endpoint={args.endpoint}, rag_top_k={args.rag_top_k}")

    per_query: list[dict[str, Any]] = []
    t0 = time.time()
    for i, q in enumerate(queries, start=1):
        ts = time.time()
        try:
            doc_ids, _raw = ask_rag(args.endpoint, q["query"], args.rag_top_k)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i:>2}/{len(queries)}] {q['id']} ✗ {e}")
            doc_ids = []
        elapsed = time.time() - ts
        m = compute_metrics(doc_ids, q["expected_doc_ids"])
        per_query.append(
            {
                "id": q["id"],
                "query": q["query"],
                "category": q["category"],
                "difficulty": q["difficulty"],
                "expected_doc_ids": q["expected_doc_ids"],
                "top_doc_ids": doc_ids[:30],
                "metrics": m,
                "elapsed_s": round(elapsed, 2),
                "notes": q.get("notes"),
            }
        )
        rank = m.get("first_hit_rank")
        print(
            f"  [{i:>2}/{len(queries)}] {q['id']} | r@10={fmt_pct(m['recall']['@10'])} "
            f"r@30={fmt_pct(m['recall']['@30'])} rank1={rank if rank else '—'} ({elapsed:.1f}s)"
        )

    total_elapsed = time.time() - t0
    summary = aggregate(per_query)
    print()
    print(f"== 总用时 {total_elapsed:.1f}s ==")
    o = summary["overall"]
    print(f"Overall Recall@10 = {fmt_pct(o['recall_at_10'])}")
    print(f"Overall Recall@30 = {fmt_pct(o['recall_at_30'])}")
    print(f"Overall MRR       = {o['mrr']:.4f}")
    print(f"零召回 (recall@30=0) = {o['zero_recall_count']} / {o['count']}")
    print()
    print("按 category:")
    for cat in sorted(summary["by_category"]):
        s = summary["by_category"][cat]
        print(
            f"  {cat:14s} n={s['count']:2d} r@10={fmt_pct(s['recall_at_10'])} "
            f"r@30={fmt_pct(s['recall_at_30'])} MRR={s['mrr']:.4f}"
        )
    print("按 difficulty:")
    for diff in ("easy", "medium", "hard"):
        if diff not in summary["by_difficulty"]:
            continue
        s = summary["by_difficulty"][diff]
        print(
            f"  {diff:8s} n={s['count']:2d} r@10={fmt_pct(s['recall_at_10'])} "
            f"r@30={fmt_pct(s['recall_at_30'])} MRR={s['mrr']:.4f}"
        )

    baseline_compare = None
    if args.compare:
        comp_path = RESULTS_DIR / f"{args.compare}.json"
        if not comp_path.exists():
            print(f"WARN: compare baseline not found: {comp_path}", file=sys.stderr)
        else:
            with comp_path.open("r", encoding="utf-8") as f:
                baseline_compare = json.load(f)["summary"]

    if args.baseline:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_json = RESULTS_DIR / f"{args.baseline}.json"
        with out_json.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "tag": args.baseline,
                    "endpoint": args.endpoint,
                    "rag_top_k": args.rag_top_k,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "per_query": per_query,
                    "summary": summary,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n已保存 baseline 到 {out_json}")
        if not args.no_report:
            DOCS_REPORT_DIR.mkdir(parents=True, exist_ok=True)
            md_path = DOCS_REPORT_DIR / f"rag_eval_{args.baseline}.md"
            write_markdown_report(
                md_path,
                args.baseline,
                per_query,
                summary,
                endpoint=args.endpoint,
                rag_top_k=args.rag_top_k,
                baseline_compare=baseline_compare,
            )
            print(f"已写 markdown 报告到 {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
