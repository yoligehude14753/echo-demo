"""梦境整合 API：手动触发、历史日志。"""
from __future__ import annotations
from fastapi import APIRouter
from app.db import get_db

router = APIRouter(prefix="/api/dream", tags=["dream"])


@router.post("/trigger")
async def trigger_dream(device_id: str = "default"):
    """手动触发指定设备的梦境整合（记忆巩固）。"""
    from app.dream.consolidator import DreamConsolidator
    consolidator = DreamConsolidator(device_id)
    stats = await consolidator.run()
    return {"status": "ok", "stats": stats}


@router.get("/logs/{device_id}")
async def dream_logs(device_id: str, limit: int = 10):
    """返回设备的梦境整合历史日志。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            """SELECT log_id, started_at, completed_at, processed_count,
                      promoted_count, pruned_count, merged_count, status, summary
               FROM dream_logs WHERE device_id=?
               ORDER BY started_at DESC LIMIT ?""",
            (device_id, limit),
        )
        rows = await cursor.fetchall()
    return {"logs": [dict(r) for r in rows], "device_id": device_id}
