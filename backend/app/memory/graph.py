"""
Knowledge Graph Memory — bi-temporal model + Heat mechanism + Source Confidence.

Node dimensions: person / event / logic / knowledge

Heat = cumulative access_count × conversation_turns (objective, no LLM scoring)
source_confidence = quality of the originating transcript:
  personal=0.9, activate=0.8, ambient=0.4
effective_heat = heat × source_confidence  (used for ranking)

This means a node about "user's friend Alice" extracted from a personal conversation
will rank above a similarly-accessed node about "Alice" overheard from a TV show.

Bi-temporal: valid_at (reality time) + created_at (system learn time)

Each MemoryGraph instance is scoped to one device_id.
All SQL queries include WHERE device_id=self.device_id for isolation.
"""
from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone, timedelta
from loguru import logger

from app.config import get_config
from app.db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _node_id() -> str:
    return str(uuid.uuid4())


class MemoryGraph:
    """
    CRUD + retrieval for the knowledge graph, scoped to one device.
    All methods are async (aiosqlite).
    """

    def __init__(self, device_id: str = "default") -> None:
        self.device_id = device_id

    # ── Nodes ─────────────────────────────────────────────────────

    async def upsert_node(
        self,
        name: str,
        dimension: str,
        description: str,
        valid_at: str | None = None,
        session_id: str | None = None,
        aliases: list[str] | None = None,
        somatic_marker: dict | None = None,
        source_confidence: float = 0.7,
    ) -> str:
        """
        Insert or update a memory node.

        source_confidence (0.0–1.0) encodes origin quality:
          personal=0.9, activate=0.8, ambient=0.4
        On upsert, we keep the HIGHER confidence value so that if a node first
        learned from ambient is later confirmed by a personal conversation, its
        confidence upgrades rather than downgrades.

        Returns node_id.
        """
        vat = valid_at or _now()

        async with get_db() as conn:
            cursor = await conn.execute(
                """SELECT node_id, source_confidence FROM memory_nodes
                   WHERE device_id=? AND name=? AND dimension=? AND invalid_at IS NULL""",
                (self.device_id, name, dimension),
            )
            row = await cursor.fetchone()

            if row:
                node_id = row["node_id"]
                # Take the max confidence so higher-quality sources win
                new_confidence = max(source_confidence, row["source_confidence"] or 0.0)
                await conn.execute(
                    """UPDATE memory_nodes
                       SET description=?, valid_at=?, source_confidence=?
                       WHERE node_id=?""",
                    (description, vat, new_confidence, node_id),
                )
            else:
                node_id = _node_id()
                await conn.execute(
                    """
                    INSERT INTO memory_nodes
                        (node_id, device_id, name, aliases, dimension, description,
                         valid_at, created_at, heat, access_count, layer,
                         session_id, somatic_marker, source_confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'episodic', ?, ?, ?)
                    """,
                    (
                        node_id,
                        self.device_id,
                        name,
                        json.dumps(aliases or [], ensure_ascii=False),
                        dimension,
                        description,
                        vat,
                        _now(),
                        session_id,
                        json.dumps(somatic_marker or {}, ensure_ascii=False),
                        source_confidence,
                    ),
                )
            await conn.commit()
        return node_id

    async def invalidate_node(self, node_id: str) -> None:
        """Mark a node as no longer valid (soft delete, bi-temporal)."""
        async with get_db() as conn:
            await conn.execute(
                "UPDATE memory_nodes SET invalid_at=? WHERE node_id=? AND device_id=?",
                (_now(), node_id, self.device_id),
            )
            await conn.commit()

    async def get_node(self, node_id: str) -> dict | None:
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM memory_nodes WHERE node_id=? AND device_id=?",
                (node_id, self.device_id),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    # ── Edges ─────────────────────────────────────────────────────

    async def upsert_edge(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        valid_at: str | None = None,
        session_id: str | None = None,
    ) -> str:
        vat = valid_at or _now()
        async with get_db() as conn:
            cursor = await conn.execute(
                """SELECT edge_id FROM memory_edges
                   WHERE device_id=? AND from_id=? AND to_id=? AND relation=? AND invalid_at IS NULL""",
                (self.device_id, from_id, to_id, relation),
            )
            row = await cursor.fetchone()
            if row:
                return row["edge_id"]

            edge_id = str(uuid.uuid4())
            await conn.execute(
                """INSERT INTO memory_edges
                       (edge_id, device_id, from_id, to_id, relation, valid_at, created_at, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (edge_id, self.device_id, from_id, to_id, relation, vat, _now(), session_id),
            )
            await conn.commit()
        return edge_id

    # ── Heat mechanism ────────────────────────────────────────────

    async def record_access(self, node_id: str, conversation_turns: int = 1) -> None:
        """
        Increment access_count and recompute heat.
        heat += conversation_turns (cumulative sum)
        """
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT access_count, heat FROM memory_nodes WHERE node_id=? AND device_id=?",
                (node_id, self.device_id),
            )
            row = await cursor.fetchone()
            if not row:
                return

            new_count = row["access_count"] + 1
            new_heat = row["heat"] + conversation_turns

            await conn.execute(
                """UPDATE memory_nodes
                   SET access_count=?, heat=?, last_accessed=?
                   WHERE node_id=? AND device_id=?""",
                (new_count, new_heat, _now(), node_id, self.device_id),
            )
            await conn.commit()

    async def promote_hot_nodes(self) -> int:
        """Promote episodic nodes above HEAT_PROMOTION_THRESHOLD to semantic."""
        async with get_db() as conn:
            cursor = await conn.execute(
                """UPDATE memory_nodes SET layer='semantic'
                   WHERE device_id=? AND layer='episodic' AND heat >= ? AND invalid_at IS NULL
                   RETURNING node_id""",
                (self.device_id, get_config().HEAT_PROMOTION_THRESHOLD),
            )
            promoted = await cursor.fetchall()
            await conn.commit()
        count = len(promoted)
        if count:
            logger.info(f"[{self.device_id}] Promoted {count} nodes to semantic layer")
        return count

    # ── Retrieval ─────────────────────────────────────────────────

    async def search_by_name(self, query: str, top_k: int | None = None) -> list[dict]:
        """
        Full-text search on name and description (SQLite LIKE), scoped to device.

        Ranking: effective_heat = heat × source_confidence
        A node from a personal conversation (confidence=0.9) will rank above
        an equally-accessed node from ambient (confidence=0.4).
        """
        k = top_k or get_config().RETRIEVAL_TOP_K
        pattern = f"%{query}%"
        async with get_db() as conn:
            cursor = await conn.execute(
                """SELECT *, (heat * source_confidence) AS effective_heat
                   FROM memory_nodes
                   WHERE device_id=? AND invalid_at IS NULL
                     AND (name LIKE ? OR description LIKE ?)
                   ORDER BY effective_heat DESC, layer DESC
                   LIMIT ?""",
                (self.device_id, pattern, pattern, k),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_neighbors(self, node_id: str) -> list[dict]:
        """Return all valid edges (+ target node info) for a node."""
        async with get_db() as conn:
            cursor = await conn.execute(
                """SELECT e.relation, n.node_id, n.name, n.dimension, n.description
                   FROM memory_edges e
                   JOIN memory_nodes n ON e.to_id = n.node_id
                   WHERE e.from_id=? AND e.device_id=? AND e.invalid_at IS NULL AND n.invalid_at IS NULL""",
                (node_id, self.device_id),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_hot_nodes(self, top_k: int | None = None) -> list[dict]:
        """Return top-k nodes by effective_heat (heat × source_confidence)."""
        k = top_k or get_config().RETRIEVAL_TOP_K
        async with get_db() as conn:
            cursor = await conn.execute(
                """SELECT *, (heat * source_confidence) AS effective_heat
                   FROM memory_nodes
                   WHERE device_id=? AND invalid_at IS NULL
                   ORDER BY effective_heat DESC LIMIT ?""",
                (self.device_id, k),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Pruning ───────────────────────────────────────────────────

    async def prune_cold_nodes(self) -> int:
        """
        Soft-delete episodic nodes older than COLD_NODE_AGE_DAYS with low heat.
        """
        cfg = get_config()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=cfg.COLD_NODE_AGE_DAYS)
        ).isoformat()

        async with get_db() as conn:
            cursor = await conn.execute(
                """UPDATE memory_nodes SET invalid_at=?
                   WHERE device_id=? AND layer='episodic'
                     AND heat < ? AND created_at < ? AND invalid_at IS NULL
                   RETURNING node_id""",
                (_now(), self.device_id, cfg.HEAT_DECAY_THRESHOLD, cutoff),
            )
            pruned = await cursor.fetchall()
            await conn.commit()

        count = len(pruned)
        if count:
            logger.info(f"[{self.device_id}] Pruned {count} cold episodic nodes")
        return count
