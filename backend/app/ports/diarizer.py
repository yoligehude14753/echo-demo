"""声纹分离 Port：ECAPA-TDNN。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DiarizerPort(Protocol):
    async def identify(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> str | None:
        """返回最匹配的 speaker_id；新说话人则注册并返回新 id。

        向后兼容入口；ambient 主链路用 identify_segments 拿到更细粒度的句级结果。
        """

    async def identify_segments(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int = 16_000,
    ) -> list[Any]:
        """按 VAD 切句 → 返回 [SegmentSpeaker(start_ms, end_ms, speaker_id)]。

        - speaker_id=None 表示该段太短或 embed 失败
        - 整段没 voiced 段（噪声/静音）→ 返回 []
        - PR echodesk-spk-2 引入，修单 chunk 内多人混音 → 注册新人的根因。
        """

    async def reset(self) -> None:
        """重置 enrollment（新会议开始时调用）。"""
