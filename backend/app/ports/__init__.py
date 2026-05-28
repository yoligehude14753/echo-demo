"""Ports 层：抽象接口，业务 use_cases 仅依赖此处的 Protocol/ABC。

Adapters 实现这些 Protocol 后由 DI 注入到 use_cases。任何 use_case 都不得
直接 import adapters 内的具体实现（架构 Fitness Function 强制检查）。
"""

from app.ports.diarizer import DiarizerPort
from app.ports.embedding import EmbeddingPort
from app.ports.llm import LLMPort
from app.ports.rag import RagPort
from app.ports.skill import SkillExecutorPort
from app.ports.stt import STTPort
from app.ports.tts import TTSPort
from app.ports.web_search import WebSearchPort

__all__ = [
    "DiarizerPort",
    "EmbeddingPort",
    "LLMPort",
    "RagPort",
    "STTPort",
    "SkillExecutorPort",
    "TTSPort",
    "WebSearchPort",
]
