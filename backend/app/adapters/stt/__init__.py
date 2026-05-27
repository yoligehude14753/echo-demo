"""STT adapter 集合。

echo-demo 部署：STT 走远程 GPU（heyi-bj tailscale）。

当前**唯一**支持的 STT backend = **FireRed**（`firered` @ :8090）——见
docs/ARCH-AUDIT.md §2。SenseVoice 历史上作为 fallback 存在，已在 PR
`echodesk-remove-sensevoice` 删除，原因：架构判断时多 backend 选项会让人误判
"换 backend 能修 X"——实际所有 speaker explosion / ambient 幻觉问题都是
ambient_capture + diarizer 链路问题，跟 STT 模型无关。

未来本地化（demo 阶段需要离线运行时）保持 Ports & Adapters 模式以便扩展。
"""

from app.adapters.stt.firered import FireRedSTT, STTError
from app.config import Settings
from app.ports.stt import STTPort


def make_stt(settings: Settings) -> STTPort:
    """目前固定返回 FireRedSTT；保留 settings.stt_backend 字段供未来扩展。

    向后兼容：旧 .env 里 `STT_BACKEND=sensevoice_gpu` 会被忽略（不再支持），
    回退到 firered 并打日志（在 adapter 实例化处不显式 warn，以免 boot 时刷屏）。
    """
    backend = (settings.stt_backend or "").lower().strip()
    if backend and backend != "firered":
        # 旧值如 sensevoice_gpu / sensevoice / deepgram / whisper 等 → 统一忽略，
        # 走 firered。不抛错以免老 .env 升级时 backend 启动失败。
        import logging

        logging.getLogger("echodesk.stt").warning(
            "stt_backend=%r 已不再支持，统一回退到 firered（PR remove-sensevoice）",
            settings.stt_backend,
        )
    return FireRedSTT(settings)


__all__ = ["FireRedSTT", "STTError", "make_stt"]
