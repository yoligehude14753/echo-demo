"""STT adapter 集合。

echo-demo 部署模型：STT 走远程 GPU（heyi-bj tailscale :8093）。
本地不跑大模型——保持 Ports & Adapters 模式以便未来扩展。
"""

from app.adapters.stt.sensevoice_gpu import SenseVoiceGPUSTT, STTError
from app.config import Settings
from app.ports.stt import STTPort


def make_stt(settings: Settings) -> STTPort:
    """按 settings.stt_backend 选 STT 适配器。当前只支持远程 GPU。"""
    return SenseVoiceGPUSTT(settings)


__all__ = ["STTError", "SenseVoiceGPUSTT", "make_stt"]
