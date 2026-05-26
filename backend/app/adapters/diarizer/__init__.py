"""声纹识别 adapter。"""

from app.adapters.diarizer.ecapa import DiarizerError, ECAPADiarizer, NullDiarizer, make_diarizer

__all__ = ["DiarizerError", "ECAPADiarizer", "NullDiarizer", "make_diarizer"]
