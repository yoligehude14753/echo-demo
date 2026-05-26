"""声纹分离 Port：ECAPA-TDNN。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DiarizerPort(Protocol):
    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> str | None:
        """返回最匹配的 speaker_id；新说话人则注册并返回新 id。"""

    async def reset(self) -> None:
        """重置 enrollment（新会议开始时调用）。"""
