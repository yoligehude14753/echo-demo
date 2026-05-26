"""LLM adapter 集合。"""

from app.adapters.llm.openai_compatible import LLMError, OpenAICompatibleLLM

__all__ = ["LLMError", "OpenAICompatibleLLM"]
