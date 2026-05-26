"""TTS adapter 集合。"""

from app.adapters.tts.cosyvoice import CosyVoiceTTS, TTSError

__all__ = ["CosyVoiceTTS", "TTSError"]
