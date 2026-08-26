"""
Task persistence tool — create, list, and complete tasks.

Task types:
  reminder   — time-based reminder
  research   — async research task (web search + summary)
  monitoring — periodic check on a topic
  periodic   — recurring action
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from loguru import logger
from app.db import get_db

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "创建一个任务或提醒。当用户说'帮我提醒'、'记住要'、'以后定期'等时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题"},
                "type": {
                    "type": "string",
                    "enum": ["reminder", "research", "monitoring", "periodic"],
                    "description": "任务类型",
                },
                "description": {"type": "string", "description": "任务详情"},
                "due_at": {
                    "type": "string",
                    "description": "截止时间 ISO-8601（可选）",
                },
            },
            "required": ["title", "type"],
        },
    },
}

LIST_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_tasks",
        "description": "查看当前待处理的任务列表。当用户问'我有什么任务'、'有没有提醒'时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "done", "all"],
                    "default": "pending",
                },
            },
        },
    },
}


async def create_task(
    device_id: str,
    title: str,
    type: str,
    description: str = "",
    due_at: str | None = None,
) -> str:
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO tasks (task_id, device_id, type, title, description,
                                  due_at, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (task_id, device_id, type, title, description, due_at, now),
        )
        await conn.commit()

    logger.info(f"Task created: {title} [{type}] for {device_id}")
    due_str = f"，截止 {due_at}" if due_at else ""
    return f"好的，已创建任务「{title}」{due_str}。"


async def list_tasks(device_id: str, status: str = "pending") -> str:
    async with get_db() as conn:
        if status == "all":
            cursor = await conn.execute(
                "SELECT title, type, due_at, status FROM tasks WHERE device_id=? ORDER BY created_at DESC LIMIT 20",
                (device_id,),
            )
        else:
            cursor = await conn.execute(
                "SELECT title, type, due_at, status FROM tasks WHERE device_id=? AND status=? ORDER BY created_at DESC LIMIT 10",
                (device_id, status),
            )
        rows = await cursor.fetchall()

    if not rows:
        return "当前没有待处理的任务。"

    lines = ["你的任务列表：\n"]
    for row in rows:
        due = f"（截止：{row['due_at'][:10]}）" if row["due_at"] else ""
        status_emoji = "✓" if row["status"] == "done" else "○"
        lines.append(f"{status_emoji} [{row['type']}] {row['title']}{due}")

    return "\n".join(lines)


async def complete_task(device_id: str, task_title: str) -> str:
    """
    Mark a task as done. Periodic tasks are automatically rescheduled
    based on their interval_hours field.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    async with get_db() as conn:
        cursor = await conn.execute(
            """SELECT task_id, type, interval_hours FROM tasks
               WHERE device_id=? AND title LIKE ? AND status='pending'""",
            (device_id, f"%{task_title}%"),
        )
        tasks = await cursor.fetchall()
        if not tasks:
            return f"没有找到待处理的任务「{task_title}」。"

        for task in tasks:
            if task["type"] == "periodic" and task["interval_hours"]:
                # Reschedule: reset to pending with new due_at
                next_due = (now + timedelta(hours=task["interval_hours"])).isoformat()
                await conn.execute(
                    """UPDATE tasks SET status='pending', due_at=?, last_run_at=?
                       WHERE task_id=?""",
                    (next_due, now.isoformat(), task["task_id"]),
                )
            else:
                await conn.execute(
                    "UPDATE tasks SET status='done', last_run_at=? WHERE task_id=?",
                    (now.isoformat(), task["task_id"]),
                )
        await conn.commit()

    return f"已完成任务「{task_title}」。"
