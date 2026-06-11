"""网关配置（全部来自环境变量；真实上游凭证只在此处）。

安全要点：
- ``echo_gw_tokens`` 是发给外部客户端的 token 白名单（逗号分隔）。
- ``yunwu_open_key`` 等上游真实凭证只在网关进程内，绝不下发客户端。
- 任何字段都不写死真实密钥；默认值仅为占位/上游公网隧道地址。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 服务 ───────────────────────────────────────────────
    host: str = "0.0.0.0"  # 容器内监听全网卡，由反代/隧道暴露
    port: int = 8080
    log_level: str = "INFO"

    # ── 客户端鉴权 ─────────────────────────────────────────
    # 发给外部用户的 token 白名单，逗号分隔。空 = 拒绝所有（fail-closed）。
    echo_gw_tokens: str = ""
    # 每个 token 的滑动窗口限流：window_s 内最多 max_requests 次。
    rate_limit_window_s: float = 60.0
    rate_limit_max_requests: int = 120

    # ── 上游：主 LLM（yunwu） ──────────────────────────────
    yunwu_base_url: str = "https://yunwu.ai/v1"
    yunwu_open_key: str = ""
    # 路由到 yunwu 的模型名（其余 chat 模型默认走 heyi fast）。
    yunwu_models: str = "MiniMax-M2.7,GLM-4.6,Kimi-K2.6"

    # ── 上游：fast LLM（heyi-bj，vLLM/sglang，OpenAI 兼容） ──
    heyi_fast_base_url: str = "https://llm-fast.yoliyoli.uk/v1"
    heyi_fast_key: str = "EMPTY"

    # ── 上游：STT（FireRed，heyi-bj） ──────────────────────
    # 不含 /v1；网关在 /v1/audio/transcriptions 转发到 {base}/v1/audio/transcriptions
    heyi_stt_base_url: str = "https://stt.yoliyoli.uk"

    # ── 上游：TTS（Qwen3-TTS，heyi-bj） ────────────────────
    heyi_tts_base_url: str = "https://tts.yoliyoli.uk"

    # ── 超时 ───────────────────────────────────────────────
    upstream_timeout_s: float = 120.0
    upstream_connect_timeout_s: float = 10.0

    # ── CORS（客户端 Electron app:// 与本地调试） ─────────
    allowed_origins: str = "*"

    def token_set(self) -> set[str]:
        return {t.strip() for t in self.echo_gw_tokens.split(",") if t.strip()}

    def yunwu_model_set(self) -> set[str]:
        return {m.strip() for m in self.yunwu_models.split(",") if m.strip()}

    def origins_list(self) -> list[str]:
        raw = self.allowed_origins.strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
