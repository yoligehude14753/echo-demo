"""TTS adapter 集合。"""

from app.adapters.tts.qwen3_tts import CosyVoiceTTS, Qwen3TTS, TTSError

__all__ = ["CosyVoiceTTS", "Qwen3TTS", "TTSError"]
