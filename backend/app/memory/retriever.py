"""
Memory Retriever — builds context for LLM injection from two sources:

  1. memory_nodes (knowledge graph) — structured entities / facts
  2. ambient_transcripts (raw history) — verbatim conversation snippets

Both are searched concurrently; results are merged and formatted as Markdown.

Ranking algorithms:

  Graph nodes: ORDER BY effective_heat DESC
    effective_heat = heat × source_confidence
    personal-sourced nodes (confidence=0.9) rank above ambient (confidence=0.4)
    even with the same raw access count.

  Ambient transcripts: source_weight × time_decay composite score
    source_weight: personal=2.0, activate=1.5, ambient=1.0
    time_decay: 1/(1+days_ago) — recency bonus, but quality beats pure recency
    A personal conversation from yesterday beats an ambient snippet from today.

Secondary capture (检索时的二次捕捉):
  When the user asks about a topic, we not only return structured memory nodes
  but also surface recent raw transcripts that match the query.  This ensures
  fleeting details that haven't been consolidated into the graph yet are still
  reachable at retrieval time.  personal transcripts are surfaced first.
"""
from __future__ import annotations

import asyncio
from loguru import logger
from app.config import get_config
from app.memory.graph import MemoryGraph
from app.db import get_db


# ── Knowledge graph retrieval ─────────────────────────────────────────────────

async def _graph_context(
    query: str,
    graph: MemoryGraph,
    top_k: int,
) -> list[str]:
    """
    Return formatted lines from the knowledge graph matching query.

    graph.search_by_name() now orders by effective_heat = heat × source_confidence.
    This means nodes extracted from personal conversations surface first.
    """
    nodes = await graph.search_by_name(query, top_k=top_k)
    if not nodes:
        return []

    lines: list[str] = []
    seen_ids: set[str] = set()

    for node in nodes:
        nid = node["node_id"]
        if nid in seen_ids:
            continue
        seen_ids.add(nid)

        dim_label = {
            "person": "人物", "event": "事件",
            "logic": "规律", "knowledge": "知识",
        }.get(node["dimension"], "")

        # Annotate high-confidence personal-source nodes for transparency
        confidence = node.get("source_confidence", 0.7)
        tag = " 🔒" if confidence >= 0.85 else ""  # personal/activate sourced

        lines.append(f"- [{dim_label}]{tag} **{node['name']}**: {node['description']}")

        neighbors = await graph.get_neighbors(nid)
        for nb in neighbors[:3]:
            if nb["node_id"] not in seen_ids:
                lines.append(
                    f"  └─ {node['name']} {nb['relation']} **{nb['name']}** ({nb['description']})"
                )
                seen_ids.add(nb["node_id"])

        await graph.record_access(nid)

    return lines


# ── Ambient transcript retrieval (二次捕捉) ────────────────────────────────────

async def _ambient_context(
    query: str,
    device_id: str,
    top_k: int,
) -> list[str]:
    """
    Full-text search over ambient_transcripts for query keywords.

    Ranking: source_weight × time_decay composite score
      source_weight: personal=2.0, activate=1.5, ambient=1.0
      time_decay: 1.0 / (1.0 + days_since_recorded)

    A personal conversation from yesterday scores:   2.0 × (1/2) = 1.0
    An ambient transcript from today scores:         1.0 × (1/1) = 1.0
    A personal conversation from today scores:       2.0 × (1/1) = 2.0  ← wins

    This ensures personal content surfaces first without completely burying
    very recent ambient matches.
    """
    keywords = list({w for w in query.split() if len(w) >= 2})
    if not keywords:
        return []

    like_clauses = " OR ".join(["text LIKE ?" for _ in keywords])
    params: list = [f"%{kw}%" for kw in keywords]
    params += [device_id, top_k]

    # source_weight × time_decay composite: julianday gives float days
    sql = f"""
        SELECT text, speaker_label, speaker_uuid, source, router_action, recorded_at,
               (CASE router_action
                    WHEN 'personal' THEN 2.0
                    WHEN 'activate' THEN 1.5
                    ELSE 1.0
                END
                / (1.0 + (julianday('now') - julianday(recorded_at)))
               ) AS relevance_score
        FROM ambient_transcripts
        WHERE ({like_clauses})
          AND device_id = ?
        ORDER BY relevance_score DESC
        LIMIT ?
    """

    try:
        async with get_db() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
    except Exception as exc:
        logger.warning(f"[Retriever] ambient_transcripts search failed: {exc}")
        return []

    if not rows:
        return []

    lines: list[str] = []
    lines.append("### 相关历史对话片段")
    for row in rows:
        speaker = row["speaker_uuid"] or row["speaker_label"] or "未知"
        ts = (row["recorded_at"] or "")[:16]
        source_label = {"device": "设备", "desktop": "桌面"}.get(row["source"], row["source"])
        # Mark personal transcripts visually so LLM understands provenance
        action = row.get("router_action") or ""
        action_tag = " [私人对话]" if action == "personal" else ""
        lines.append(f"- [{ts} {source_label}]{action_tag} {speaker}：{row['text']}")

    return lines


# ── Public API ────────────────────────────────────────────────────────────────

async def retrieve_context(
    query: str,
    graph: MemoryGraph,
    top_k: int | None = None,
    device_id: str | None = None,
    ambient_top_k: int = 5,
) -> str:
    """
    Return a Markdown context string combining:
      - knowledge graph nodes (structured memory)
      - ambient transcript snippets (二次捕捉, raw history)

    Both are fetched concurrently.  Empty string if nothing found.
    """
    cfg = get_config()
    k = top_k or cfg.RETRIEVAL_TOP_K
    dev_id = device_id or (graph.device_id if hasattr(graph, "device_id") else "default")

    # Parallel fetch
    graph_task = asyncio.create_task(_graph_context(query, graph, k))
    ambient_task = asyncio.create_task(
        _ambient_context(query, dev_id, ambient_top_k)
    )
    graph_lines, ambient_lines = await asyncio.gather(graph_task, ambient_task)

    all_lines: list[str] = []
    if graph_lines:
        all_lines.append("### 记忆图谱")
        all_lines.extend(graph_lines)
    if ambient_lines:
        all_lines.extend(ambient_lines)

    return "\n".join(all_lines) if all_lines else ""


async def get_hot_context(graph: MemoryGraph, top_k: int = 5) -> str:
    """Return context from the highest-heat nodes (for proactive prompting)."""
    nodes = await graph.get_hot_nodes(top_k=top_k)
    if not nodes:
        return ""

    lines = []
    for node in nodes:
        dim_label = {
            "person": "人物", "event": "事件",
            "logic": "规律", "knowledge": "知识",
        }.get(node["dimension"], "")
        lines.append(f"- [{dim_label}] **{node['name']}**: {node['description']}")

    return "\n".join(lines)
