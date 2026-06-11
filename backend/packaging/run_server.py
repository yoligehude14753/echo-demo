"""PyInstaller 打包入口：把 EchoDesk backend 冻结成自带二进制。

打出来的可执行文件内置 uvicorn + app + 全部 Python 依赖（含 torch/speechbrain），
用户机无需安装 Python / venv。Electron main.cjs 直接 spawn 这个二进制即可。

环境变量：
  ECHO_BACKEND_HOST  默认 127.0.0.1
  ECHO_BACKEND_PORT  默认 8769
  ECHO_LOG_LEVEL     默认 info
"""

from __future__ import annotations

import multiprocessing
import os


def _selftest() -> int:
    """ECHO_SELFTEST=ecapa 时跑：直接 import + 加载 ECAPA，打印真实 traceback。"""
    import traceback

    print("[selftest] importing speechbrain.inference.speaker ...")
    try:
        from speechbrain.inference.speaker import SpeakerRecognition

        print("[selftest] import OK:", SpeakerRecognition)
    except Exception:
        print("[selftest] IMPORT FAILED:")
        traceback.print_exc()
        return 1

    print("[selftest] loading ECAPA model + encoding synthetic audio ...")
    try:
        import numpy as np
        import torch

        enc = SpeakerRecognition.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=".cache/speechbrain/ecapa",
            run_opts={"device": "cpu"},
        )
        # 2s @ 16k 合成正弦
        t = np.linspace(0, 2, 32000, dtype=np.float32)
        wav = (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        emb = enc.encode_batch(torch.from_numpy(wav).unsqueeze(0))
        vec = emb.squeeze().cpu().numpy()
        print(f"[selftest] ECAPA EMBED OK: dim={vec.shape}, norm={float(np.linalg.norm(vec)):.3f}")
    except Exception:
        print("[selftest] ECAPA LOAD/EMBED FAILED:")
        traceback.print_exc()
        return 2
    return 0


def main() -> None:
    # 冻结后子进程（如有）需要 freeze_support，避免 Windows 上递归启动
    multiprocessing.freeze_support()

    if os.environ.get("ECHO_SELFTEST") == "ecapa":
        raise SystemExit(_selftest())

    host = os.environ.get("ECHO_BACKEND_HOST", "127.0.0.1")
    port = int(os.environ.get("ECHO_BACKEND_PORT", "8769"))
    log_level = os.environ.get("ECHO_LOG_LEVEL", "info")

    import uvicorn

    # 传 app 对象而非 import string：冻结环境下没有可重新 import 的模块路径，
    # 且禁用 reload/workers（单进程内运行）。
    from app.main import app

    uvicorn.run(app, host=host, port=port, log_level=log_level, workers=1)


if __name__ == "__main__":
    main()
