"""
Dream Memory Consolidation — five-phase idle-time process.

Phases:
  1. Orient         — scan unprocessed sessions + ambient_transcripts
  2a. Personal mine — HIGH-FIDELITY extraction from personal conversations (priority=2)
                      Uses dedicated personal prompt, source_confidence=0.9
  2b. Ambient mine  — CONSERVATIVE extraction from ambient background (priority<2)
                      Uses standard prompt, source_confidence=0.4
  3. Gather         — retrieve related nodes from graph per session
  4. Consolidate    — LLM: merge duplicate nodes, extract semantic patterns
  5. Prune & Index  — promote hot nodes, prune cold nodes, update heat

Phase 2 is split because personal and ambient transcripts require
fundamentally different extraction strategies:
- Personal: user explicitly participated → interpersonal context, plans,
  emotional signals → high-confidence nodes
- Ambient: overheard → facts, background info → conservative, lower-confidence nodes

personal rows are processed FIRST (priority=2 > priority=0) so that
any entity context from personal conversations is in the graph before
we process potentially conflicting ambient overheard content.

Triggered by:
  a. APScheduler cron (default 03:00 daily) — legacy scheduled run
  b. Idle detection — fires DREAM_IDLE_MINUTES after last transcript arrives

Gate conditions: ≥ DREAM_MIN_EPISODIC unprocessed sessions OR
                 ≥ 1 unprocessed ambient_transcripts row.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from loguru import logger

from app.config import get_config
from app.db import get_db
from app.memory.graph import MemoryGraph
from app.llm import complete_nano as complete  # dream is bulk batch work, nano model is sufficient


async def _get_unprocessed_by_priority(
    device_id: str,
    min_priority: int,
    max_priority: int,
    limit: int | None = None,
) -> list[dict]:
    """
    Fetch unprocessed ambient_transcripts within a priority range.
    Ordering: priority DESC (highest first), then recorded_at ASC (oldest first).
    """
    cfg = get_config()
    batch = limit or cfg.DREAM_AMBIENT_BATCH
    async with get_db() as conn:
        cursor = await conn.execute(
            """SELECT id, text, speaker_label, speaker_uuid, source, router_action,
                      recorded_at, priority
               FROM ambient_transcripts
               WHERE device_id=? AND processed_for_memory=0
                 AND priority BETWEEN ? AND ?
               ORDER BY priority DESC, recorded_at ASC LIMIT ?""",
            (device_id, min_priority, max_priority, batch),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _get_unprocessed_ambient(device_id: str, limit: int | None = None) -> list[dict]:
    """
    Fetch ALL unprocessed ambient_transcripts, ordered by priority DESC then time ASC.
    Used for gate-check and backward-compatible callers.
    """
    cfg = get_config()
    batch = limit or cfg.DREAM_AMBIENT_BATCH
    async with get_db() as conn:
        cursor = await conn.execute(
            """SELECT id, text, speaker_label, speaker_uuid, source, router_action,
                      recorded_at, priority
               FROM ambient_transcripts
               WHERE device_id=? AND processed_for_memory=0
               ORDER BY priority DESC, recorded_at ASC LIMIT ?""",
            (device_id, batch),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _mark_ambient_processed(ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE ambient_transcripts SET processed_for_memory=1 WHERE id IN ({placeholders})",
            ids,
        )
        await conn.commit()


async def _mine_rows_to_graph(
    graph: MemoryGraph,
    rows: list[dict],
    session_id: str,
    mode: str = "ambient",  # "personal" | "ambient"
) -> int:
    """
    LLM-based entity & fact extraction from ambient_transcripts rows.

    mode="personal": Uses extract_from_personal_conversation() with a richer
      prompt focused on interpersonal context.  source_confidence=0.9.
    mode="ambient":  Uses extract_from_conversation() with broadcast/pleasantry
      filters.  source_confidence=0.4.

    Returns number of rows processed (regardless of extraction outcome).
    """
    if not rows:
        return 0

    from app.memory.extractor import (
        extract_from_conversation,
        extract_from_personal_conversation,
        save_extraction,
    )
    from app.nodes.memory import action_to_source_confidence

    # Batch size: personal uses smaller batches (richer prompt, larger context)
    BATCH = 8 if mode == "personal" else 20
    source_confidence = 0.9 if mode == "personal" else 0.4

    processed = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]

        if mode == "personal":
            # For personal rows, preserve speaker attribution in content
            turns = []
            for r in batch:
                if not r.get("text"):
                    continue
                speaker = r.get("speaker_label") or "用户"
                turns.append({"role": "user", "content": f"[{speaker}] {r['text']}"})

            # Infer speaker_info for context: unique speaker labels in this batch
            speakers = {
                r.get("speaker_label") for r in batch
                if r.get("speaker_label") and r.get("speaker_label") != "SPEAKER_0"
            }
            speaker_info = "、".join(speakers) if speakers else None

            if not turns:
                processed += len(batch)
                continue
            try:
                extracted = await extract_from_personal_conversation(turns, speaker_info)
                await save_extraction(extracted, session_id, graph, source_confidence=source_confidence)
            except Exception as exc:
                logger.warning(f"[Dream] personal mine LLM failed: {exc}")
        else:
            turns = [
                {"role": "user", "content": r["text"]}
                for r in batch
                if r.get("text")
            ]
            if not turns:
                processed += len(batch)
                continue
            try:
                extracted = await extract_from_conversation(turns)
                await save_extraction(extracted, session_id, graph, source_confidence=source_confidence)
            except Exception as exc:
                logger.warning(f"[Dream] ambient mine LLM failed: {exc}")

        processed += len(batch)

    return processed


# Backward-compatible alias
async def _mine_ambient_to_graph(
    graph: MemoryGraph,
    rows: list[dict],
    session_id: str,
) -> int:
    return await _mine_rows_to_graph(graph, rows, session_id, mode="ambient")


async def _get_unprocessed_sessions(device_id: str, limit: int = 50) -> list[dict]:
    async with get_db() as conn:
        cursor = await conn.execute(
            """SELECT session_id, notes, turn_count, started_at
               FROM sessions
               WHERE device_id=? AND consolidated=0
               ORDER BY started_at ASC LIMIT ?""",
            (device_id, limit),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def _mark_sessions_consolidated(session_ids: list[str]) -> None:
    if not session_ids:
        return
    placeholders = ",".join("?" * len(session_ids))
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE sessions SET consolidated=1 WHERE session_id IN ({placeholders})",
            session_ids,
        )
        await conn.commit()


async def _consolidate_nodes(graph: MemoryGraph, node_names: list[str]) -> int:
    """
    Ask LLM to identify duplicate/related nodes and merge their descriptions.
    Returns count of merged nodes.
    """
    if len(node_names) < 2:
        return 0

    # Gather full node data
    nodes_data = []
    for name in node_names[:30]:  # limit to 30 for prompt size
        results = await graph.search_by_name(name, top_k=1)
        if results:
            nodes_data.append(results[0])

    if not nodes_data:
        return 0

    nodes_summary = "\n".join(
        f"- [{n['dimension']}] {n['name']}: {n['description']}"
        for n in nodes_data
    )

    prompt = (
        "以下是从对话中提取的知识图谱节点。请识别重复或可合并的节点，"
        "输出合并建议（JSON格式）：\n\n"
        f"{nodes_summary}\n\n"
        "输出格式：\n"
        '[{"keep": "节点名称", "merge": ["被合并节点名称1", ...], "new_description": "合并后描述"}]\n'
        "如果没有需要合并的，输出 []"
    )

    try:
        import json
        raw = await complete(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            stream=False,
        )
        raw = raw.strip().strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        merges = json.loads(raw)
        merged = 0
        for merge_op in merges:
            keep_name = merge_op.get("keep")
            to_merge = merge_op.get("merge", [])
            new_desc = merge_op.get("new_description", "")

            # Find keep node
            keep_results = await graph.search_by_name(keep_name, top_k=1)
            if not keep_results:
                continue
            keep_id = keep_results[0]["node_id"]

            # Update description
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE memory_nodes SET description=? WHERE node_id=?",
                    (new_desc, keep_id),
                )
                await conn.commit()

            # Invalidate merged nodes
            for merge_name in to_merge:
                merge_results = await graph.search_by_name(merge_name, top_k=1)
                for mr in merge_results:
                    if mr["node_id"] != keep_id:
                        await graph.invalidate_node(mr["node_id"])
                        merged += 1

        return merged
    except Exception as e:
        logger.warning(f"Dream consolidation LLM call failed: {e}")
        return 0


class DreamConsolidator:
    """
    Orchestrates the four-phase Dream consolidation for a device.
    """

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.graph = MemoryGraph()

    async def should_run(self) -> bool:
        """
        Gate check: run if any of the following is true:
          - ≥ DREAM_MIN_EPISODIC unprocessed sessions (and enough time elapsed)
          - ≥ 1 unprocessed ambient_transcripts row (no time gate for raw mining)
        """
        cfg = get_config()

        # Always mine pending ambient transcripts when they exist
        ambient_rows = await _get_unprocessed_ambient(self.device_id, limit=1)
        if ambient_rows:
            return True

        # Classic gate: sessions + time interval
        sessions = await _get_unprocessed_sessions(self.device_id)
        if len(sessions) < cfg.DREAM_MIN_EPISODIC:
            logger.debug(
                f"Dream skipped: {len(sessions)} sessions (need {cfg.DREAM_MIN_EPISODIC}), "
                f"no pending ambient rows"
            )
            return False

        async with get_db() as conn:
            cursor = await conn.execute(
                """SELECT started_at FROM dream_logs
                   WHERE device_id=? AND status='done'
                   ORDER BY started_at DESC LIMIT 1""",
                (self.device_id,),
            )
            row = await cursor.fetchone()

        if row:
            last_run = datetime.fromisoformat(row["started_at"])
            elapsed_hours = (
                datetime.now(timezone.utc) - last_run
            ).total_seconds() / 3600
            if elapsed_hours < cfg.DREAM_INTERVAL_HOURS:
                logger.debug(f"Dream skipped: only {elapsed_hours:.1f}h since last run")
                return False

        return True

    async def run(self) -> dict:
        """Execute five-phase Dream consolidation. Returns summary dict."""
        log_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()

        async with get_db() as conn:
            await conn.execute(
                """INSERT INTO dream_logs (log_id, device_id, started_at, status)
                   VALUES (?, ?, ?, 'running')""",
                (log_id, self.device_id, started_at),
            )
            await conn.commit()

        stats = {
            "processed": 0,
            "personal_mined": 0, "ambient_mined": 0,
            "promoted": 0, "pruned": 0, "merged": 0,
        }

        try:
            # ── Phase 1: Orient ──────────────────────────────────
            logger.info(f"[Dream] Phase 1: Orient — {self.device_id}")
            sessions = await _get_unprocessed_sessions(self.device_id)
            # Fetch separately so we can report counts
            personal_rows = await _get_unprocessed_by_priority(
                self.device_id, min_priority=2, max_priority=2
            )
            ambient_rows = await _get_unprocessed_by_priority(
                self.device_id, min_priority=0, max_priority=1
            )
            stats["processed"] = len(sessions)
            logger.info(
                f"[Dream] {len(sessions)} sessions, "
                f"{len(personal_rows)} personal rows, "
                f"{len(ambient_rows)} ambient rows to process"
            )

            dream_session = f"dream-{log_id[:8]}"

            # ── Phase 2a: Personal mining (high-fidelity) ─────────
            if personal_rows:
                logger.info(
                    f"[Dream] Phase 2a: Mine {len(personal_rows)} personal transcripts "
                    f"(source_confidence=0.9, richer prompt)"
                )
                stats["personal_mined"] = await _mine_rows_to_graph(
                    self.graph, personal_rows, dream_session, mode="personal"
                )
                await _mark_ambient_processed([r["id"] for r in personal_rows])
                logger.info(f"[Dream] Personal mining done: {stats['personal_mined']} rows")
            else:
                logger.info("[Dream] Phase 2a: No pending personal transcripts")

            # ── Phase 2b: Ambient mining (conservative) ───────────
            if ambient_rows:
                logger.info(
                    f"[Dream] Phase 2b: Mine {len(ambient_rows)} ambient transcripts "
                    f"(source_confidence=0.4, standard prompt)"
                )
                stats["ambient_mined"] = await _mine_rows_to_graph(
                    self.graph, ambient_rows, dream_session, mode="ambient"
                )
                await _mark_ambient_processed([r["id"] for r in ambient_rows])
                logger.info(f"[Dream] Ambient mining done: {stats['ambient_mined']} rows")
            else:
                logger.info("[Dream] Phase 2b: No pending ambient transcripts")

            # ── Phase 3: Gather ───────────────────────────────────
            logger.info(f"[Dream] Phase 3: Gather — {len(sessions)} sessions")
            all_node_names: list[str] = []
            for session in sessions:
                notes = session.get("notes", "")
                if notes:
                    import re
                    names = re.findall(r"\*\*([^*]+)\*\*", notes)
                    all_node_names.extend(names)

            # ── Phase 4: Consolidate ──────────────────────────────
            logger.info(f"[Dream] Phase 4: Consolidate — {len(all_node_names)} entities")
            if all_node_names:
                stats["merged"] = await _consolidate_nodes(self.graph, all_node_names)

            # ── Phase 5: Prune & Index ────────────────────────────
            logger.info("[Dream] Phase 5: Prune & Index")
            stats["promoted"] = await self.graph.promote_hot_nodes()
            stats["pruned"] = await self.graph.prune_cold_nodes()

            session_ids = [s["session_id"] for s in sessions]
            await _mark_sessions_consolidated(session_ids)

            async with get_db() as conn:
                await conn.execute(
                    """UPDATE dream_logs SET
                           completed_at=?, status='done',
                           processed_count=?, promoted_count=?,
                           pruned_count=?, merged_count=?,
                           summary=?
                       WHERE log_id=?""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        stats["processed"], stats["promoted"],
                        stats["pruned"], stats["merged"],
                        (
                            f"Sessions={stats['processed']}, "
                            f"personal_mined={stats['personal_mined']}, "
                            f"ambient_mined={stats['ambient_mined']}, "
                            f"merged={stats['merged']}, "
                            f"promoted={stats['promoted']}, "
                            f"pruned={stats['pruned']}"
                        ),
                        log_id,
                    ),
                )
                await conn.commit()

            logger.info(f"[Dream] Complete: {stats}")
            return stats

        except Exception as e:
            logger.exception(f"[Dream] Failed: {e}")
            async with get_db() as conn:
                await conn.execute(
                    "UPDATE dream_logs SET status='failed', completed_at=? WHERE log_id=?",
                    (datetime.now(timezone.utc).isoformat(), log_id),
                )
                await conn.commit()
            return stats
