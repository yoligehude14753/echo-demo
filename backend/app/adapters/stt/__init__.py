"""STT adapter 集合。"""

from app.adapters.stt.sensevoice_gpu import SenseVoiceGPUSTT, STTError

__all__ = ["STTError", "SenseVoiceGPUSTT"]
