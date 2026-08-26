"""任务管理 API：待办事项的查询与完成标记。"""
from __future__ import annotations
from fastapi import APIRouter, Query
from app.db import get_db

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{device_id}")
async def list_tasks(
    device_id: str,
    status: str = Query(default="pending", pattern="^(pending|done|all)$"),
    limit: int = Query(default=20, le=100),
):
    """返回设备任务列表，可按状态筛选。"""
    async with get_db() as conn:
        if status == "all":
            cursor = await conn.execute(
                """SELECT task_id, type, title, description, due_at, status, created_at, last_run_at
                   FROM tasks WHERE device_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (device_id, limit),
            )
        else:
            cursor = await conn.execute(
                """SELECT task_id, type, title, description, due_at, status, created_at, last_run_at
                   FROM tasks WHERE device_id=? AND status=?
                   ORDER BY due_at ASC NULLS LAST LIMIT ?""",
                (device_id, status, limit),
            )
        rows = await cursor.fetchall()
    return {"tasks": [dict(r) for r in rows], "device_id": device_id, "status": status}


@router.delete("/{device_id}/{task_id}")
async def complete_task(device_id: str, task_id: str):
    """将任务标记为完成（软删除）。"""
    async with get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET status='done' WHERE task_id=? AND device_id=?",
            (task_id, device_id),
        )
        await conn.commit()
    return {"status": "ok", "task_id": task_id}
