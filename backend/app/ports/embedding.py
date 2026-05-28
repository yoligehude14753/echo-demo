"""Embedding Port：屏蔽 bge-m3-local / yunwu / heyi-qwen3 等供应商差异。

2026-05-28 spike（``docs/rag_embedding_spike_2026-05-28.md``）结论：
- 主路 = 本地 bge-m3（query ~50ms、50k chunks 回填 ~7 min、零边际成本）
- fallback = 云雾 ``text-embedding-3-large``（仅 cold-start / 回填，不参与在线 query）
- heyi-bj 远端**暂时不可用**（zero embedding 服务，所有 sglang 跑 chat），
  待运维起 ``--is-embedding`` 后通过 ``HeyiQwen3Embedding`` 接入即可。

Port 只暴露最小接口，路由 + 重试 + 降级由 adapter 层 / Router adapter 完成；
``ports/`` 强制不允许 import 任何第三方 SDK（架构 Fitness Function 检查）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    """Dense embedding 统一接口。

    约定：
    - 入参始终是 ``Sequence[str]``；单串调用方传 ``[text]`` 即可，避免 adapter
      内部分支判断 batch=1 / batch>1。
    - 返回 ``list[list[float]]``，与入参 1:1 对齐；每个 vector 长度 = ``dim``。
    - 失败由 adapter 内部 retry + 包装为 ``EmbeddingError`` 抛出；调用方决定
      是否走 fallback（典型由 ``EmbeddingRouter`` 统一处理）。
    - ``is_query=True`` 仅对 e5 系列模型需要前缀；bge-m3 query/doc 共用，
      adapter 可忽略此参数（保留 API 面以保前向兼容）。
    """

    @property
    def model_name(self) -> str:
        """用于 vector store header / metrics 标签，模型版本漂移检测。"""
        ...

    @property
    def dim(self) -> int:
        """输出维度（bge-m3=1024 / text-embedding-3-small=1536 / -3-large=3072）。"""
        ...

    @property
    def max_input_tokens(self) -> int:
        """单串最大 token 数；超过者由 adapter 截断或抛错（约定后者）。"""
        ...

    async def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 32,
        timeout_s: float = 60.0,
        is_query: bool = False,
    ) -> list[list[float]]:
        """对一批文本做 dense encoding。"""
        ...

    async def health(self) -> bool:
        """cold-start 阶段轻量探活；返回 False 触发 router 切 fallback。"""
        ...
