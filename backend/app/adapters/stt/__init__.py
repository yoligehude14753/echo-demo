"""STT adapter 集合。

echo-demo 部署：STT 走远程 GPU（heyi-bj tailscale）。

当前默认 = **FireRed**（`firered` @ :8090）——见 docs/ARCH-AUDIT.md §2。
SenseVoice 保留作可选 backend（`sensevoice_gpu` @ :8093），但实测 6s ambient
上短碎片 + 日英乱码严重，不推荐。

未来本地化（demo 阶段需要离线运行时）保持 Ports & Adapters 模式以便扩展。
"""

from app.adapters.stt.firered import FireRedSTT
from app.adapters.stt.firered import STTError as _FireRedSTTError
from app.adapters.stt.sensevoice_gpu import SenseVoiceGPUSTT
from app.adapters.stt.sensevoice_gpu import STTError as _SenseVoiceSTTError
from app.config import Settings
from app.ports.stt import STTPort

# 两个 adapter 各自抛同名 STTError；统一对外用 FireRed 那个（运行时它们语义一样）
STTError = _FireRedSTTError
assert _SenseVoiceSTTError.__name__ == "STTError"  # 同名校验


def make_stt(settings: Settings) -> STTPort:
    """按 settings.stt_backend 选 STT 适配器。

    - `firered`（默认）→ FireRedASR2-AED @ heyi :8090
    - `sensevoice_gpu` → SenseVoice GPU ASR @ heyi :8093（保留作 fallback）
    """
    backend = settings.stt_backend.lower().strip()
    if backend == "firered":
        return FireRedSTT(settings)
    if backend in ("sensevoice_gpu", "sensevoice"):
        return SenseVoiceGPUSTT(settings)
    raise ValueError(
        f"unknown stt_backend={settings.stt_backend!r}; "
        "expected one of: firered, sensevoice_gpu"
    )


__all__ = ["STTError", "FireRedSTT", "SenseVoiceGPUSTT", "make_stt"]
