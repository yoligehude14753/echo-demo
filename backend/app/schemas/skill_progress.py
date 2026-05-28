"""Skill 生成过程的进度事件 schema（流式输出过程性内容用）。

生成产物经常一次 5-15 分钟（LLM 出 6000+ chars HTML / 27 字段 JSON），
旧 ``POST /artifacts/generate`` 阻塞接口让前端只能看 spinner，看不到 LLM
实时进度。新 ``POST /artifacts/generate/stream`` SSE 端点把整个过程拆成
若干 ``SkillProgress`` 事件流式推给前端：

| stage              | 触发时机                                | payload 关键字段     |
|--------------------|----------------------------------------|---------------------|
| prompt_build       | 选完 skill 路径准备 system prompt 时    | msg                 |
| llm_stream_start   | 即将开 SSE 连 LLM 时                    | msg                 |
| llm_chunk          | 累积每 ~200 chars 推一次（避免太密）     | text / total_chars  |
| llm_stream_done    | LLM SSE 完整收到（或抛错前）            | total_chars / latency_ms |
| invariants_check   | HTML invariants / JSON 字段校验阶段     | msg                 |
| executor_run       | 调 node render.mjs / python_executor 等 | msg / tool          |
| saved              | 产物已落盘                              | artifact_id         |
| done               | 整个流程成功完成                        | artifact            |
| error              | 任一阶段失败                            | msg                 |

前端 ``CommandBar`` 按 stage 类型分别更新 Echo 气泡：phase 改文案 + 保持
``status="pending"``；llm_chunk 在文案里追加"已收到 N 字符"；done 切
``status="done"``；error 切 ``status="failed"``。

设计要点：
- ``llm_chunk`` 的 ``text`` 是「目前累积的全部内容」而不是增量 delta —— 前端只
  需要展示尾部 ~300 字给用户预览，不需要拼接（避免前后端字符串增长不同步）。
- 所有 stage 都可缺省 msg / text / total_chars / latency_ms / artifact / tool。
  pydantic optional 字段空时不进 SSE 序列化（exclude_none=True）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.artifact import GeneratedArtifact

SkillProgressStage = Literal[
    "prompt_build",
    "llm_stream_start",
    "llm_chunk",
    "llm_stream_done",
    "invariants_check",
    "executor_run",
    "saved",
    "done",
    "error",
]


class SkillProgress(BaseModel):
    """SkillExecutor.generate_stream 产出的单条进度事件。

    SSE 序列化时使用 ``model_dump(mode='json', exclude_none=True)``，确保
    空字段不污染前端 patch（前端按 key 是否存在判断要不要更新对应文案）。
    """

    stage: SkillProgressStage
    msg: str | None = None
    """phase 类阶段的中文短描述（用于前端气泡 text 字段）。"""

    text: str | None = None
    """llm_chunk 阶段的累积全文（前端只取尾部展示）。"""

    total_chars: int | None = None
    """llm_chunk / llm_stream_done 阶段累积字符数（用于"已收到 N 字符"）。"""

    latency_ms: float | None = None
    """llm_stream_done 阶段 LLM 调用的 wall-clock 耗时。"""

    tool: str | None = None
    """executor_run 阶段实际调用的工具名（exec_node_to_artifact / exec_python_to_artifact 等）。"""

    artifact: GeneratedArtifact | None = None
    """done 阶段最终产物（与 GeneratedArtifact 结构一致）。"""

    error: str | None = None
    """error 阶段的人类可读错误信息。"""

    model_config = {"extra": "forbid"}


class SkillProgressEnvelope(BaseModel):
    """SSE 帧的薄封装（在 endpoint 层把 stage 单独提到 event:; data 携带其他）。

    保留为后续可能的扩展用；目前 endpoint 直接序列化 ``SkillProgress`` 即可。
    """

    event: str = Field(description="SSE event 类型，与 stage 对齐")
    data: SkillProgress
