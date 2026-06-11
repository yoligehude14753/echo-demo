"""测试前置：在导入 app 之前注入网关配置环境变量。"""

from __future__ import annotations

import os

os.environ.setdefault("ECHO_GW_TOKENS", "tok-good-1,tok-good-2")
os.environ.setdefault("YUNWU_OPEN_KEY", "sk-test-yunwu")
os.environ.setdefault("YUNWU_MODELS", "MiniMax-M2.7,GLM-4.6")
os.environ.setdefault("HEYI_FAST_BASE_URL", "https://fast.test/v1")
os.environ.setdefault("YUNWU_BASE_URL", "https://yunwu.test/v1")
os.environ.setdefault("HEYI_STT_BASE_URL", "https://stt.test")
os.environ.setdefault("HEYI_TTS_BASE_URL", "https://tts.test")
os.environ.setdefault("RATE_LIMIT_MAX_REQUESTS", "5")
os.environ.setdefault("RATE_LIMIT_WINDOW_S", "60")
