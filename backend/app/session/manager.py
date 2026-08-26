"""
Session Manager — tracks conversation turns and maintains Session Memory Notes.

Session Memory Notes (SMN) is a lightweight rolling markdown document that
captures key facts, tool calls, and open threads from the current session.
It is injected into the LLM system prompt so the model maintains continuity
even as the raw conversation exceeds the context window.

Promotion to Memory Graph happens asynchronously after session end (Phase 2).
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from loguru import logger

from app.config import get_config
from app.db import get_db


class Turn:
    __slots__ = ("role", "content", "timestamp")

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content
        self.timestamp = datetime.now(timezone.utc)


class SessionManager:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.session_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self.turns: list[Turn] = []
        self.notes = ""
        self.tool_call_count = 0
        self._token_estimate = 0

    def add_turn(self, role: str, content: str) -> None:
        self.turns.append(Turn(role, content))
        self._token_estimate += len(content) // 4

    async def maybe_update_notes(self) -> None:
        cfg = get_config()
        if (
            self._token_estimate >= cfg.SESSION_NOTES_TOKEN_THRESHOLD
            or self.tool_call_count >= cfg.SESSION_NOTES_TOOL_CALL_THRESHOLD
        ):
            await self._compress_to_notes()

    async def _compress_to_notes(self) -> None:
        """
        Summarise recent turns into Session Memory Notes.
        Uses nano model — this is pure compression, quality matters less than cost.
        """
        if len(self.turns) < 2:
            return

        from app.llm import complete_nano

        recent_text = "\n".join(
            f"{t.role.upper()}: {t.content}" for t in self.turns[-20:]
        )
        prompt = (
            "以下是最近的对话片段，请提取关键信息更新会话笔记。\n\n"
            f"【现有笔记】\n{self.notes or '（空）'}\n\n"
            f"【最近对话】\n{recent_text}\n\n"
            "请用简洁的 Markdown 输出更新后的笔记（不超过500字），包含：\n"
            "- 用户提到的重要事实（人名、地名、日期、偏好等）\n"
            "- 正在进行的任务或待办事项\n"
            "- 未解决的问题或话题"
        )

        try:
            new_notes = await complete_nano(
                messages=[{"role": "user", "content": prompt}],
            )
            self.notes = new_notes.strip()
            self._token_estimate = 0
            logger.debug(f"[{self.session_id}] SMN updated ({len(self.notes)} chars)")
        except Exception as e:
            logger.warning(f"[{self.session_id}] SMN update failed: {e}")

    def build_messages(self, system_prompt: str, max_turns: int = 20) -> list[dict]:
        combined_system = system_prompt
        if self.notes:
            combined_system += f"\n\n【会话笔记】\n{self.notes}"

        messages: list[dict] = [{"role": "system", "content": combined_system}]
        for t in self.turns[-max_turns:]:
            # 只有 OpenAI 标准 role 才进入 LLM 上下文；ambient 等内部 role 跳过
            if t.role not in ("user", "assistant", "system", "function", "tool"):
                continue
            messages.append({"role": t.role, "content": t.content})
        return messages

    async def save(self) -> None:
        ended_at = datetime.now(timezone.utc).isoformat()
        async with get_db() as conn:
            await conn.execute(
                """
                INSERT INTO sessions
                    (session_id, device_id, started_at, ended_at, turn_count, notes, consolidated)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(session_id) DO UPDATE SET
                    ended_at=excluded.ended_at,
                    turn_count=excluded.turn_count,
                    notes=excluded.notes
                """,
                (
                    self.session_id,
                    self.device_id,
                    self.started_at.isoformat(),
                    ended_at,
                    len(self.turns),
                    self.notes,
                ),
            )
            await conn.commit()
        logger.info(f"Session saved: {self.session_id} ({len(self.turns)} turns)")
