"""TextPunctuator Port：屏蔽具体的"加标点"后端（LLM / 规则引擎 / future SentencePiece）。

为什么单独抽 Port：use_cases.ambient_capture 走 ports & adapters 严格分层
（Fitness function `test_use_cases_layer_is_clean`），不能直接 import adapter。
本 port 让 ambient pipeline 只依赖一个抽象，方便：
- 测试时注入 fake punctuator（已有 LLMPunctuator 内部失败降级，但单测可以更直接）
- 未来切换到本地 SentencePiece / rule-based 标点引擎不动业务层
- 关闭 punctuator 时直接传 None（pipeline 已支持）
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.meeting import TranscriptSegment


@runtime_checkable
class TextPunctuatorPort(Protocol):
    """对 STT 输出的转写段做"只加标点 + 分段、不改字"的后处理。

    实现契约：
    - **fail-soft**：任何内部异常 / 超时 / 校验拒绝 → 必须退回输入 segments，
      不得抛异常（ambient 主链路不可阻塞）。
    - **id 不变**：返回列表长度与顺序与入参完全一致；只允许重写 `.text`。
    - 由 `enabled` 决定是否真的处理（关时直接 noop 返回原列表）。
    """

    @property
    def enabled(self) -> bool: ...

    async def punctuate(
        self,
        segments: list[TranscriptSegment],
    ) -> list[TranscriptSegment]: ...


__all__ = ["TextPunctuatorPort"]
