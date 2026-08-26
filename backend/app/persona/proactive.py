"""
Proactive Dialogue Engine — Echo initiates conversation based on:
  1. Reminders / task due-dates
  2. Time-based check-ins (morning / evening)
  3. Memory-based observations (hot nodes that haven't been discussed)

Gate conditions (all must pass):
  - Not in Do Not Disturb window (DND_START_HOUR to DND_END_HOUR)
  - User hasn't spoken in at least PROACTIVE_SILENCE_HOURS
  - Random gate: max daily probability PROACTIVE_MAX_DAILY_PROB
"""
from __future__ import annotations
import random
from datetime import datetime, timezone, timedelta
from loguru import logger
from app.config import get_config
from app.db import get_db


async def _last_interaction_at(device_id: str) -> datetime | None:
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT ended_at FROM sessions WHERE device_id=? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC LIMIT 1",
            (device_id,),
        )
        row = await cursor.fetchone()
    if row and row["ended_at"]:
        return datetime.fromisoformat(row["ended_at"])
    return None


def _is_dnd(now: datetime) -> bool:
    cfg = get_config()
    hour = now.hour
    start = cfg.DND_START_HOUR
    end = cfg.DND_END_HOUR
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


async def should_send_proactive(device_id: str) -> bool:
    """Return True if Echo should initiate a proactive message."""
    cfg = get_config()
    now = datetime.now(timezone.utc)

    if _is_dnd(now):
        return False

    last_at = await _last_interaction_at(device_id)
    if last_at:
        elapsed_hours = (now - last_at).total_seconds() / 3600
        if elapsed_hours < cfg.PROACTIVE_SILENCE_HOURS:
            return False

    # Probability gate
    return random.random() < cfg.PROACTIVE_MAX_DAILY_PROB


async def generate_proactive_message(device_id: str) -> str | None:
    """
    Generate a context-appropriate proactive message.
    Returns None if no good hook found.
    """
    from app.memory.graph import MemoryGraph
    from app.llm import complete

    graph = MemoryGraph()

    # Look for due tasks first (highest priority)
    async with get_db() as conn:
        now_str = datetime.now(timezone.utc).isoformat()
        cursor = await conn.execute(
            """SELECT title FROM tasks
               WHERE device_id=? AND status='pending' AND due_at IS NOT NULL AND due_at <= ?
               LIMIT 1""",
            (device_id, now_str),
        )
        task_row = await cursor.fetchone()

    if task_row:
        return f"对了，你之前让我提醒你：「{task_row['title']}」，现在到时间了哦。"

    # Hot memory hook
    hot_nodes = await graph.get_hot_nodes(top_k=3)
    if not hot_nodes:
        return None

    node = hot_nodes[0]
    hook = f"关于{node['name']}（{node['description'][:40]}）"

    try:
        msg = await complete(
            messages=[
                {
                    "role": "system",
                    "content": "你是 Echo，一个温柔的 AI 伴侣。根据以下话题钩，生成一句自然的主动问候（不超过30字，像朋友发消息一样）",
                },
                {"role": "user", "content": hook},
            ],
            tools=[],
            stream=False,
        )
        return msg.strip()
    except Exception as e:
        logger.warning(f"Proactive message generation failed: {e}")
        return None
