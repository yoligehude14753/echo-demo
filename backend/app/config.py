"""集中式配置：所有外部依赖与运行参数走这里，业务层只读 Settings。

源码层级：infra（最底层），可被任何层 import；不得反向 import 任何上层模块。

P1.2（独立产品 Phase 1）：配置三层化
  优先级 高→低：env > ~/.echodesk/config.json (user) > <repo>/.env (dev) > code default
  打包到 /Applications/EchoDesk.app 后，cwd 找不到 .env 也能跑 —— 用户配置走
  ~/.echodesk/config.json（由 install-backend.sh 写入默认值，UI 设置面板可改）。
  详见 app/config_io.py。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app import __version__
from app.config_io import JsonConfigSource, user_config_dir

# 项目根目录的 .env（dev 期兜底；prod 装机后不强求存在）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILES = (_PROJECT_ROOT / ".env", Path(".env"))
OFFICIAL_ELECTRON_ORIGIN = "echodesk://app"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """注入 ~/.echodesk/config.json 作为第二优先级 source。

        顺序：init kwargs > env > user.json > .env (dev) > file secrets > code default
        """
        return (
            init_settings,
            env_settings,
            JsonConfigSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    # ── Server ────────────────────────────────────────────────────
    # P1.1（独立产品 Phase 1）：canonical port = 8769。
    # 历史上 backend default 是 8765、Electron main.cjs / runtime.ts 是 8769，
    # 两者通过 .env / shell 命令 --port 对齐 —— 拼凑、易出错。统一到 8769。
    # 改 default 不影响显式传 --port 的部署。
    port: int = 8769
    log_level: str = "INFO"

    # Transactional outbox fan-out: each backend instance replays only a bounded
    # recent window, while rows that were globally unpublished at registration
    # are snapshotted separately and always recovered.
    workflow_outbox_replay_window_rows: int = Field(default=500, ge=0, le=100_000)
    workflow_outbox_consumer_ttl_s: float = Field(default=120.0, gt=0)
    workflow_outbox_retention_s: float = Field(default=24 * 60 * 60, gt=0)
    workflow_outbox_max_rows: int = Field(default=10_000, ge=1)
    workflow_outbox_cleanup_interval_s: float = Field(default=60.0, gt=0)

    public_ws_url: str = "ws://localhost:8769/ws/echo"
    public_http_url: str = "http://localhost:8769"
    app_version: str = __version__

    # Hub sync runs inside the existing backend lifecycle.  The development
    # env template opts in explicitly; installed clients remain disconnected
    # until Hub is enabled and a base URL is configured.
    hub_enabled: bool = False
    hub_base_url: str = ""
    hub_sync_interval_s: float = Field(default=15.0, gt=1.0, le=300.0)
    hub_request_timeout_s: float = Field(default=15.0, gt=1.0, le=120.0)
    hub_state_file: Path = Field(
        default_factory=lambda: user_config_dir() / "hub_state.json"
    )

    @field_validator("app_version", mode="after")
    @classmethod
    def _use_code_version(cls, _configured_version: str) -> str:
        """Product version is immutable build metadata, not user configuration."""
        return __version__

    # ── LLM 主通道（任意 OpenAI-compatible provider） ─────────────
    llm_main_provider: str = "yunwu"
    llm_main_model: str = "deepseek-v4-flash"
    llm_main_base_url: str = "https://yunwu.ai/v1"
    llm_main_api_key: str = Field(default="", repr=False)
    # 0.2 compatibility only. New config/UI writes ``llm_main_api_key``.
    yunwu_open_key: str = Field(default="", repr=False)
    llm_fallback_1: str = "GLM-4.6"
    llm_fallback_2: str = "Kimi-K2.6"
    llm_main_max_tokens: int = 80_000
    # 会议纪要是结构化 JSON，不应复用 MAIN/skill 的 80k 长推理预算。
    # public demo 可把主模型临时切到 eight 的 fast/VL 本地模型。当前线上
    # served model max_model_len=8192，public .env 必须把 MINUTES_MAX_TOKENS
    # / LLM_MAIN_MAX_TOKENS 降到 4096；这里的默认值保留给更长上下文的私有部署。
    minutes_max_tokens: int = 12_000

    @property
    def resolved_llm_main_api_key(self) -> str:
        """Generic main-provider credential with the 0.2 Yunwu fallback."""

        return self.llm_main_api_key.strip() or self.yunwu_open_key.strip() or "EMPTY"

    # ── LLM 快速通道 ────────────────────────────────────────────────
    # 默认跟随 Yunwu 主通道，避免私有 fast LLM 未启动时影响源码安装；
    # 私有部署可在设置页改为自己的 vLLM / OpenAI-compatible 端点。
    llm_fast_provider: str = "yunwu"
    llm_fast_model: str = "gpt-5.4-nano"
    llm_fast_base_url: str = "https://yunwu.ai/v1"
    # 展示名与真实 provider model 故意分离：UI/诊断如需展示快速模型，
    # 只读这个字段；LLM adapter 绝不得用它发起调用。
    llm_fast_display_name: str = "qwen3 8b"
    # intent route / RAG arbitration 的 fast 通道只允许短时尝试；
    # 超时立即改用 Yunwu MAIN，不再累加 15s + 30s 失败等待。
    llm_fast_classification_timeout_s: float = Field(default=2.0, ge=1.0, le=3.0)
    llm_local_api_key: str = Field(default="EMPTY", repr=False)
    llm_fast_max_tokens: int = 512

    # ── Memory（L0 工作 / L1 情景 / L2 语义 / L3 明确配置）────────
    memory_enabled: bool = True
    # 关联与抽取只允许快速模型短时参与；失败后使用确定性排序/静默跳过抽取，
    # 绝不让 memory 阻塞 Echo AI 主回答。
    memory_small_model_timeout_s: float = Field(default=2.0, ge=1.0, le=3.0)
    # 抽取需要读取完整的结构化 JSON；与 recall 的短路预算分离，但仍保持
    # 明确且有界的单次请求 deadline，避免完整抽取因 2s association deadline 被截断。
    memory_extraction_timeout_s: float = Field(default=8.0, ge=3.0, le=30.0)
    memory_small_model_candidate_limit: int = Field(default=8, ge=4, le=36)
    memory_working_ttl_s: int = Field(default=30 * 60, ge=60, le=24 * 60 * 60)
    memory_working_max_items: int = Field(default=24, ge=4, le=200)
    memory_working_max_chars: int = Field(default=24_000, ge=1_000, le=200_000)
    memory_current_meeting_window_s: int = Field(
        default=30 * 60,
        ge=60,
        le=24 * 60 * 60,
    )
    memory_current_meeting_max_segments: int = Field(default=24, ge=1, le=200)
    memory_episodic_candidates_per_kind: int = Field(default=60, ge=1, le=500)
    memory_semantic_candidate_limit: int = Field(default=120, ge=1, le=1_000)
    memory_recall_prefilter_limit: int = Field(default=36, ge=4, le=200)
    memory_recall_limit: int = Field(default=6, ge=1, le=20)
    memory_extraction_min_chars: int = Field(default=8, ge=1, le=500)
    memory_extraction_existing_limit: int = Field(default=60, ge=1, le=500)
    memory_extraction_max_items: int = Field(default=5, ge=1, le=20)
    memory_min_confidence: float = Field(default=0.72, ge=0.0, le=1.0)
    memory_recognized_text_enabled: bool = True
    memory_proactive_cooldown_s: float = Field(default=30.0, ge=5.0, le=600.0)
    memory_proactive_min_score: float = Field(default=0.62, ge=0.0, le=1.0)

    # STT/TTS/本地模型网关共享 token。历史 eight 裸服务不需要鉴权，adapter 会回退到
    # Bearer x；新部署走网关时用该字段或服务专用 api key 覆盖。
    heyi_gateway_token: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "heyi_gateway_token",
            "HEYI_GATEWAY_TOKEN",
            "model_gateway_api_key",
            "MODEL_GATEWAY_API_KEY",
        ),
    )

    @property
    def llm_fast_api_key(self) -> str:
        if self.llm_fast_base_url.rstrip("/") == self.llm_main_base_url.rstrip("/"):
            return self.resolved_llm_main_api_key
        local_key = self.llm_local_api_key.strip()
        if local_key and local_key.upper() != "EMPTY":
            return local_key
        return self.heyi_gateway_token or "EMPTY"

    # ── STT ───────────────────────────────────────────────────────
    # 当前**唯一** = firered（@ eight :8090，判别式无幻觉、中文强）；
    # echo 实战路径 Deepgram → FireRed → faster-whisper → SenseVoice → 回 FireRed。
    # SenseVoice GPU 在 PR `echodesk-remove-sensevoice` 删除（理由：多 backend
    # 选项老让人误判"换 backend 能修 X"）。详见 docs/ARCH-AUDIT.md §2。
    # 保留 stt_backend 字段供未来扩展（如本地化离线 STT）。
    stt_backend: str = "firered"
    stt_firered_url: str = "http://100.76.3.59:8090"
    stt_firered_api_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "stt_firered_api_key",
            "STT_FIRERED_API_KEY",
            "stt_api_key",
            "STT_API_KEY",
        ),
    )
    stt_language: str = "zh"
    stt_llm_correct: bool = False

    # ── ASR scheduler / capability routing ───────────────────────
    # Rollout is explicitly off by default so existing FireRed call sites
    # remain compatible until the ASR-owned integration candidate is enabled.
    asr_scheduler_enabled: bool = False
    asr_eligible_providers: tuple[str, ...] = ("firered",)
    asr_provider_weights: dict[str, float] = Field(
        default_factory=lambda: {"firered": 1.0},
    )
    asr_provider_concurrency: dict[str, int] = Field(
        default_factory=lambda: {"firered": 1},
    )
    asr_scheduler_max_concurrency: int = Field(default=4, ge=1, le=64)
    asr_scheduler_queue_size: int = Field(default=16, ge=0, le=4096)
    asr_job_deadline_s: float = Field(default=30.0, gt=0.0, le=300.0)
    asr_max_attempts: int = Field(default=2, ge=1, le=5)
    asr_circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    asr_circuit_cooldown_s: float = Field(default=15.0, gt=0.0, le=600.0)
    asr_scope_max_concurrency: int = Field(default=2, ge=1, le=64)
    asr_scope_rate_limit_per_minute: int = Field(default=60, ge=0, le=100_000)
    asr_readiness_stale_after_s: float = Field(default=30.0, gt=0.0, le=3600.0)

    asr_stepfun_enabled: bool = False
    asr_stepfun_transport: Literal["sse_one_shot", "websocket_stream"] = "sse_one_shot"
    asr_stepfun_api_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "asr_stepfun_api_key",
            "ASR_STEPFUN_API_KEY",
            "stepfun_api_key",
            "STEPFUN_API_KEY",
        ),
    )
    asr_stepfun_sse_url: str = "https://api.stepfun.com/v1/audio/asr/sse"
    asr_stepfun_ws_url: str = "wss://api.stepfun.com/v1/realtime/asr/stream"
    asr_stepfun_sse_model: str = "stepaudio-2.5-asr"
    asr_stepfun_ws_model: str = "stepaudio-2.5-asr-stream"
    asr_stepfun_sse_concurrency: int = Field(default=4, ge=1, le=64)
    asr_stepfun_ws_max_sessions: int = Field(default=4, ge=1, le=64)
    asr_stepfun_ws_send_queue_size: int = Field(default=8, ge=1, le=256)
    asr_stepfun_ws_idle_timeout_s: float = Field(default=10.0, gt=0.0, le=300.0)
    asr_stepfun_ws_max_duration_s: float = Field(default=120.0, gt=0.0, le=1800.0)

    asr_local_enabled: bool = False
    asr_local_model_path: str = ""
    asr_local_device: str = "cpu"
    asr_local_compute_type: str = "int8"
    asr_local_worker_count: int = Field(default=1, ge=1, le=1)

    # ── Privacy-safe production telemetry ────────────────────────
    # Disabled by default; when enabled the independent SQLite sink must be
    # fully configured before application startup can succeed.
    telemetry_enabled: bool = False
    telemetry_db_path: Path | None = Field(
        default_factory=lambda: user_config_dir() / "telemetry.sqlite3",
        validation_alias=AliasChoices("telemetry_db_path", "TELEMETRY_DB_PATH"),
    )
    telemetry_hmac_key_ring: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices(
            "telemetry_hmac_key_ring",
            "TELEMETRY_HMAC_KEY_RING",
        ),
        repr=False,
    )
    telemetry_hmac_current_key_version: str = Field(
        default="",
        validation_alias=AliasChoices(
            "telemetry_hmac_current_key_version",
            "TELEMETRY_HMAC_CURRENT_KEY_VERSION",
        ),
        repr=False,
    )
    telemetry_retention_s: int = Field(default=30 * 24 * 60 * 60, gt=0)
    telemetry_k_threshold: int = Field(default=5, ge=1, le=100_000)
    telemetry_rotation_period_s: int = Field(default=30 * 24 * 60 * 60, gt=0)

    @field_validator("asr_eligible_providers")
    @classmethod
    def _validate_asr_provider_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(value.strip() for value in values)
        if len(set(names)) != len(names) or any(not name for name in names):
            raise ValueError("ASR eligible providers must be unique and non-empty")
        if any(len(name) > 64 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in name) for name in names):
            raise ValueError("ASR provider names contain unsupported characters")
        return names

    @field_validator("asr_provider_weights")
    @classmethod
    def _validate_asr_provider_weights(cls, values: dict[str, float]) -> dict[str, float]:
        if any(weight <= 0 or weight > 100 for weight in values.values()):
            raise ValueError("ASR provider weights must be in (0, 100]")
        return values

    @field_validator("asr_provider_concurrency")
    @classmethod
    def _validate_asr_provider_concurrency(cls, values: dict[str, int]) -> dict[str, int]:
        if any(limit < 1 or limit > 256 for limit in values.values()):
            raise ValueError("ASR provider concurrency must be in [1, 256]")
        return values

    @model_validator(mode="after")
    def _validate_asr_cross_fields(self) -> Settings:
        eligible = set(self.asr_eligible_providers)
        if self.asr_scheduler_enabled and not eligible:
            raise ValueError("ASR scheduler requires an eligible provider set")
        if not eligible.issubset(self.asr_provider_weights):
            raise ValueError("ASR provider weights must cover every eligible provider")
        if not eligible.issubset(self.asr_provider_concurrency):
            raise ValueError("ASR provider concurrency must cover every eligible provider")
        if self.asr_stepfun_enabled and not self.asr_stepfun_api_key.strip():
            raise ValueError("enabled ASR capability is missing authentication readiness")
        if self.asr_local_enabled and not self.asr_local_model_path.strip():
            raise ValueError("enabled local ASR capability is missing model readiness")
        if "stepfun" in eligible and not self.asr_stepfun_enabled:
            raise ValueError("disabled ASR capability cannot be in the eligible set")
        if "local" in eligible and not self.asr_local_enabled:
            raise ValueError("disabled ASR capability cannot be in the eligible set")
        return self

    # ── STT 后处理：LLM 补标点 + 分段 ──────────────────────────────
    # 用户痛点（2026-05-28）：FireRedASR2 :8090 OpenAPI 只接受
    # file/model/language/response_format/timestamp_granularities，**没有 punc 开关**；
    # 6s ambient chunk 出来是一气呵成 30+ 字无标点的整行（截图 m-bdd1da4e7e21），
    # 用户读不下去。STT 服务端无法直出标点 → ambient 主链路加 LLM_FAST
    # 后处理批量加标点 + 自然分段。
    # 详见 `app/adapters/stt/llm_punctuator.py` 文件头。
    ambient_llm_punctuate: bool = True
    # 单次 batch（一个 chunk 1-3 段）超时上限；超时 → 退回原文本不阻塞主链路。
    # fast/VL 本地模型通常 < 2s，2s 是宽松上限。
    ambient_punctuator_timeout_s: float = 2.0

    # ── TTS ───────────────────────────────────────────────────────
    # 实际跑的是 faster-qwen3-tts 1.7B CustomVoice @ eight :8094
    # （echo commit b065547 切换；echo-demo `cosyvoice` 是历史命名遗留）
    # 详见 docs/ARCH-AUDIT.md §3
    tts_enabled: bool = True
    # provider 字符串当前只支持 "qwen3_tts"（含旧别名 "cosyvoice"，路由到同一 adapter）
    tts_provider: str = Field(
        default="qwen3_tts",
        validation_alias=AliasChoices("tts_provider", "TTS_PROVIDER"),
    )
    # 兼容旧 env：TTS_COSYVOICE_URL / TTS_COSYVOICE_VOICE 仍能正确加载
    tts_qwen3_url: str = Field(
        default="http://100.76.3.59:8094",
        validation_alias=AliasChoices(
            "tts_qwen3_url",
            "TTS_QWEN3_URL",
            "tts_cosyvoice_url",
            "TTS_COSYVOICE_URL",
        ),
    )
    tts_qwen3_voice: str = Field(
        default="aiden",
        validation_alias=AliasChoices(
            "tts_qwen3_voice",
            "TTS_QWEN3_VOICE",
            "tts_cosyvoice_voice",
            "TTS_COSYVOICE_VOICE",
        ),
    )
    tts_qwen3_api_key: str = Field(
        default="",
        repr=False,
        validation_alias=AliasChoices(
            "tts_qwen3_api_key",
            "TTS_QWEN3_API_KEY",
            "tts_api_key",
            "TTS_API_KEY",
            "tts_cosyvoice_api_key",
            "TTS_COSYVOICE_API_KEY",
        ),
    )
    tts_qwen3_timeout_s: float = Field(
        default=30.0,
        validation_alias=AliasChoices(
            "tts_qwen3_timeout_s",
            "TTS_QWEN3_TIMEOUT_S",
            "tts_timeout_s",
            "TTS_TIMEOUT_S",
        ),
    )
    tts_macos_fallback_enabled: bool = False

    # ── Speaker Diarization ──────────────────────────────────────
    diarizer_enabled: bool = True
    diarizer_backend: str = "ecapa"
    # 说话人声纹是否跨会议持久化（用户 2026-05-28 决策：关）。
    # 关闭含义：每个 meeting 维持独立的 speaker counter（从 1 开始），
    # diarizer 不 hydrate 老 speakers 表 / 不 insert 新行。embedding 内存里
    # 用，进程重启就没了。详见 docs/ARCH-AUDIT.md §4 root #11。
    # 用户痛点（截图复现，2026-05-28）：UI 显示「说话人 18 / 19 / 20 / 21」，
    # 编号已经累加到 20+。SpeakerRegistry 走全局 counter（N = repo.speakers
    # 总数 + 1）→ 一开会就接老编号，不是新会议从 1 开始。
    # 复活老行为：env DIARIZER_PERSIST_SPEAKERS=true 即走 legacy 路径
    # （registry hydrate 全局 + repo 持久化、ECAPA hydrate centroid）。
    diarizer_persist_speakers: bool = False
    # ── threshold 演进史 ──────────────────────────────────────────
    # 0.65 (init) → 0.70 (spk-1 抄 echo prod) → 0.55 (text-clarity PR, 本次)
    #
    # 用户痛点（2026-05-28，会议 m-bdd1da4e7e21）：实际 3 个真人说话，后端
    # ECAPA 在一个会议内创建了 14 个 speaker_id（说话人 11-16 跨段乱跳）。
    # 根因（最可能）：0.70 对单声道远场 + 麦克风距离/姿态变化 + 房间混响过严 ——
    # 同一个人在不同 chunk 上的 instantaneous embedding 与其 centroid 的 cos
    # 相似度经常落在 0.55-0.70 区间被判新人；EMA α=0.1 的慢融合也追不上。
    #
    # 把 default 降到 0.55，trade-off：
    #   + 同一说话人跨 chunk 抖动（0.55-0.70 区间）→ 正确合并，speaker 不再爆炸
    #   - 真有 5+ 人的会议里，两个音色相近的人（如同性别同年龄段）可能合并；
    #     ECAPA 上陌生人 cos 普遍 < 0.45，> 0.55 的"误中"概率不高
    # 仍可通过 DIARIZER_MATCH_THRESHOLD env 覆盖。0.65/0.70 留作回退基线。
    #
    # 详见 docs/ARCH-AUDIT.md §4 root #3。
    diarizer_match_threshold: float = 0.55
    # EMA centroid 融合系数（命中匹配时）；α=0.1 抄 echo backend/app/speaker/diarizer.py
    diarizer_centroid_ema_alpha: float = 0.1
    # spk-3：基于 VAD active seconds 决定"段够不够长可以注册新人"。
    # voiced_active_s = duration_ms / 1000 * active_ratio
    # 段内真实活跃语音 < 此值 → 不允许注册新人；尝试回退到最相似已知人
    # （sim 阈值用 diarizer_outlier_match_threshold）。echo 用 4.0s（整段 chunk
    # 时代），spk-2 切句后段长普遍 1-3s。
    # spk-6 保守预设：1.5 → 2.0。trade-off：漏注册短句新人（下次说话还能补）
    # vs 误注册（爆炸）。在拿到真实多人音频回归数据前，保守倾向漏注册。
    diarizer_min_voiced_seconds_for_new_profile: float = 2.0
    # 短段强制回退已知人时的相似度兜底阈值；低于此值即使是最相似的人也不命中
    # （宁可丢这段也不要污染最相似人的 centroid）。
    # text-clarity PR：0.60 → 0.50。配合主阈值 0.55 维持"短段比正常段更宽容"
    # 语义（短段更易回退到已知人，少新增碎片化 ID）；丢段会让 ambient text
    # 没 speaker_label，体验比"算作新人"更友好。
    diarizer_outlier_match_threshold: float = 0.50

    # ── phase4-diar-deep：跨 chunk 活跃说话人 + 短段归并 ──────────
    # 用户痛点（2026-05-28）：实际 3 个人说话，最近 2h `ambient_segments` 表里
    # 出现 40+ unique speaker_id + 57 段 NULL（数据：2026-05-28 03:28 sqlite
    # 查询）。sub_F 把 threshold 从 0.70 降到 0.55 没解决——根因不是阈值松紧，
    # 是匹配空间被历史 stale centroid 污染：`_profiles` 通过 hydrate 把所有曾
    # 注册过的 speaker_N 全部载入（哪怕是上次 explosion 的产物），新 embed 在
    # 几十个 centroid 里挑 best，真实噪音让同一人跟自己 centroid 的 cos 落入
    # 0.40-0.55 区间，而某个无关 stale centroid 恰好 0.50-0.65 → 错认 / 新建。
    #
    # 修法：每个 context（meeting_id 或 "_ambient"）维护「活跃说话人 list」+
    # 时间窗口（默认 60s）。新段先在窗口内的活跃说话人里用宽松阈值（0.35）
    # 找最近匹配 → 命中 / 复用 ID + EMA 更新；没命中再走全局 _profiles（保留
    # 0.55 阈值不放宽，跨会话仍稳健）；仍没命中 + voiced active 够长才注册新人。
    # 等同于"实时聚类"：人在说话的那一阵子，他自己的 centroid 持续被刷新，
    # 后续段在 active list 里就是绝对的"最像自己"。
    #
    # 历史 stale centroid 仍在 _profiles 不被删，但**不参与活跃匹配**（除非
    # 全局阶段命中，那就是用户真的回来说话了，应该复用历史 ID）。
    diarizer_active_window_s: float = 60.0
    # 活跃 list 内的匹配阈值（比 global 松，因为 active centroid 是"刚刚说过话
    # 的人"，几乎只可能是同一人在抖动；0.35 ≈ ECAPA 上不同人 cos 大概率 < 0.2
    # 的安全分隔点）。
    diarizer_active_match_threshold: float = 0.35
    # voiced 段短于此值（且 context 有 last_speaker）→ 不调 ECAPA，直接归到
    # 上一 speaker。规避「短噪声段独立 embed → 不像任何人 → 新建」路径。
    # 1500ms 选择理由：
    # - audio_gate.split_into_voiced_segments 默认 min_segment_ms=800 已经把
    #   < 800ms 的段在 VAD 阶段就过滤掉，**所以阈值必须 > 800 才有效**；
    # - ECAPA `_MIN_BYTES_FOR_EMBED = 32_000`（1s），800-1000ms 段以前直接返 None；
    # - 1500ms 跟 spk-6 的 diarizer_min_voiced_seconds_for_new_profile=2.0 协同：
    #   < 2.0 active_s 不允许注册新人，但 < 1.5s 干脆别 embed 直接归并。
    diarizer_short_segment_continuity_ms: int = 1500

    # ── 音频预过滤（防 STT 幻觉 + speaker 编号爆炸；移植自 echo）─────
    # 对齐基线：echo `backend/app/pipeline.py:570-577`（生产值 600 / 400 / 0.05 / 12）。
    # echodesk-spk-4 在 echo 基线之上"再收紧一档"，原因见 docs/ARCH-AUDIT.md §4 root #7：
    #   echo 在 ServerVAD 切句级 + 700ms silence-trim 链路里，5% 帧活跃率配合够用；
    #   echo-demo 的 ambient 链路仍是 6s 定长 chunk（spk-2 才切 VAD），
    #   5% × 6s ≈ 0.3s 偶发噪声就能放行 → 实测大量"嗯。"/英文幻觉漏入。
    # 收紧的预算：宁可漏过低语，也不要让底噪上 RAG / 污染 speaker registry。
    #
    # 整段 RMS 门控：低于此值视为底噪 → 跳过 STT/diarizer，整 chunk 丢弃
    # 600 → 800：远场底噪 30-60、近场底噪 200-400、正常说话 2000-8000+，提高安全余量
    ambient_rms_gate: int = 800
    # 帧级 VAD：20ms 帧统计；帧 RMS > 阈值算"活跃"
    # 400 → 500：把"活跃帧"门槛抬到底噪与正常说话之间更靠近正常说话
    ambient_frame_rms_threshold: int = 500
    # 活跃帧比例 < 此值 → 跳过 STT
    # 0.05 → 0.15：6s chunk 至少 ~0.9s 真正活跃帧；闭合 ARCH-AUDIT §4 root #7
    ambient_min_speech_frame_ratio: float = 0.15
    # STT 后 cps 门控：字符速率 > 此值视为幻觉/复读丢弃
    # 12 → 10：中文正常 4-8 cps，>10 大概率复读/幻觉；ambient 按 VAD 活跃
    # 时长计算，而不是用整块静音稀释字符速率。
    ambient_max_cps: float = 10.0
    # STT 输出最短字符数（小于此值丢弃，防止单字幻觉污染 RAG/speaker registry）
    # 4 → 5：echo 路由层用 3（router）+ 下游 dream consolidator 用 8 双重过滤；
    # echo-demo 单点过滤，取中位数 5 → 拦截 "嗯。" / "Yeah" / "ですね" 等短幻觉
    ambient_min_stt_chars: int = 5
    # 同一规范化文本在短窗口内反复出现时，第二次起不再作为会议活跃证据；
    # 超过允许次数后直接丢弃新副本，避免固定环境音产生的 ASR 复读污染历史。
    ambient_repeat_window_s: float = Field(default=60.0, gt=0, le=3600.0)
    ambient_repeat_drop_after: int = Field(default=2, ge=1, le=20)
    # 通过音质门控的 ambient WAV 最长保留时间。GC 只扫描当前 owner 的物理
    # scope；文本/RAG 生命周期独立，音频删除后 repository 会清空对应 audio_ref。
    ambient_audio_retention_s: float = Field(default=7 * 24 * 60 * 60, gt=0)
    # local-first 也受此 owner 级容量上限保护；public backend 还会同时受
    # quota_storage_bytes 的全持久化预算约束，实际生效值取两道边界中更严格者。
    ambient_audio_owner_max_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)

    # ── 会议自动检测（与 PRD §自动开会/自动结束 对齐） ──────────
    # 检测窗口；distinct speakers 需 ≥ min_distinct 且总语音 ≥ min_active_s
    automeet_window_s: float = 30.0
    automeet_min_distinct_speakers: int = 2
    automeet_min_active_seconds: float = 6.0
    # speaker_id 不可用时默认禁止仅凭 ASR 文本自动开会；显式配置正数才启用
    # fallback，避免噪声幻觉/声纹失败形成重复 auto-meeting。
    automeet_unknown_speaker_min_active_seconds: float | None = Field(default=None, gt=0)
    # 自动开始与会议续命使用比“允许转写入库”更严格的音频证据。这样低语仍可
    # 保留为 ambient，但不能仅凭一段 ASR 文本把会议无限续命。
    automeet_min_valid_speech_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    automeet_min_valid_speech_ms: int = Field(default=800, ge=0, le=60_000)
    # 静默 X 秒 → 自动 end（含 finalize 纪要）
    automeet_silence_timeout_s: float = 30.0
    # 自动结束后多久内不再触发新会议（防抖）
    automeet_cooldown_s: float = 60.0
    # 单个 auto-meeting 的硬上限（兜底）：超过 X 秒一律 force-end，
    # 防止持续环境音 / 单人独白让会议永不结束（2026-05 「会议中 9h+」bug 的结构性修复）。
    # hydrate 也会用这个值清理跨重启遗留的过期 auto-meeting。
    automeet_max_meeting_duration_s: float = 1800.0
    # 手动会议仍以“无有效语音自动结束”为主，4h 仅作最终安全兜底。
    manual_meeting_max_duration_s: float = Field(default=4 * 60 * 60, ge=60.0)
    manual_meeting_inactivity_timeout_s: float = Field(default=15 * 60, ge=30.0)
    # 手动会议允许短时崩溃/重启后续接，但跨天仍 in_meeting 已无法判断用户意图，
    # 必须自动结束，避免顶栏永久累计成数千分钟。
    meeting_recovery_max_age_s: float = 24 * 60 * 60
    meeting_rag_repair_interval_s: float = Field(default=60.0, ge=5.0, le=3600.0)

    # ── RAG ───────────────────────────────────────────────────────
    rag_index_dir: Path = Field(default=Path("~/.echodesk/rag_index").expanduser())
    rag_top_k: int = 5
    rag_pdf_chunk_tokens: int = 600
    rag_pdf_chunk_overlap: int = 100
    # BM25 is an in-process ranker. Bound one principal's durable payload and
    # parsed chunk set so a valid multi-file workload cannot exhaust the server.
    rag_index_max_payload_bytes_per_principal: int = Field(
        default=64 * 1024 * 1024,
        ge=1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    rag_index_max_chunks_per_principal: int = Field(
        default=50_000,
        ge=100,
        le=1_000_000,
    )

    # ── 授权工作区（M6：用户配置可索引的目录范围） ────────────
    # 多个目录用逗号分隔，例如 ECHO_WORKSPACE_DIRS=~/Documents/work,~/Notes
    workspace_dirs: str = ""
    # 单文件上限。20 → 100：2026-05-28 用户痛点 —— 实测 102MB 文件夹里两个 30-40MB
    # PDF 被 size 过滤静默丢弃（scanner 报 added=N，但 N 已含 size-skip 数差），
    # markitdown/pdfplumber 对 100MB 以内的中文营销/技术 PDF 都能秒级抽取，
    # 把默认放宽到 100MB，更贴合"我授权一个文件夹，常见 PDF 都会进 RAG"的直觉。
    # 仍可通过 ECHO_WORKSPACE_MAX_FILE_MB env 覆盖。
    workspace_max_file_mb: float = 100.0
    workspace_scan_on_startup: bool = True
    workspace_state_file: Path = Field(
        default_factory=lambda: user_config_dir() / "workspace_state.json"
    )

    @property
    def workspace_dirs_list(self) -> list[Path]:
        return [Path(d.strip()).expanduser() for d in self.workspace_dirs.split(",") if d.strip()]

    # 用户拖入的最大上传大小（用户拖入；workspace 配置走 workspace_max_file_mb）
    upload_max_file_mb: float = 50.0

    # ── Web Search（Tavily-only；无 key 时联网检索不可用） ──
    web_search_enabled: bool = True
    web_search_top_n: int = 5
    tavily_api_key: str = Field(default="", repr=False)

    # ── Skill 执行器 ──────────────────────────────────────────────
    skill_ppt_tool: str = "pptxgenjs"
    skill_word_tool: str = "python-docx"
    skill_xlsx_tool: str = "openpyxl"
    skill_xlsx_recalc: str = "libreoffice"
    skill_html_tool: str = "single-file-tailwind-cdn"
    skill_fix_max_retries: int = 3
    skill_node_bin: str = "node"
    # Electron 启动 bundled backend 时注入自身可执行文件。对子进程设置
    # ELECTRON_RUN_AS_NODE=1 后，它就是随安装包携带、跨平台匹配的 Node runtime。
    # 源码启动未设置这两个变量时仍使用 PATH 中的 node/npm。
    echodesk_node_runtime: str = ""
    echodesk_node_runtime_is_electron: bool = False
    skill_executor_build_dir: Path = Field(default=Path("~/.echodesk/skill_build").expanduser())
    skill_executor_timeout_s: int = 300
    skill_executor_max_tokens: int = 80_000
    # Startup recovery must never remove another process's in-flight build.
    # Workflow builds are protected by their durable execution lease; this
    # grace only applies to legacy/direct executor directories without one.
    artifact_build_stale_grace_s: float = Field(
        default=60 * 60,
        ge=60,
        le=7 * 24 * 60 * 60,
    )

    @property
    def resolved_skill_node_bin(self) -> str:
        # The Electron-provided runtime is a packaged fallback, not an override
        # for an explicitly configured skill runtime.  Keeping that precedence
        # matters for fail-closed diagnostics as well as administrator policy:
        # setting SKILL_NODE_BIN to a missing/blocked executable must not be
        # silently masked by ECHODESK_NODE_RUNTIME.
        configured = self.skill_node_bin.strip()
        if configured and configured != "node":
            return configured
        return self.echodesk_node_runtime.strip() or configured or "node"

    @property
    def resolved_skill_node_is_electron(self) -> bool:
        return bool(
            self.resolved_skill_node_bin == self.echodesk_node_runtime.strip()
            and self.echodesk_node_runtime.strip()
            and self.echodesk_node_runtime_is_electron
        )

    # ── phase4-doc-skills：HTML/PPT 高质量 skill 灰度开关 ───────────
    # 默认 false：HTML 走 Kami warm-parchment one-pager；PPT 走 ib_master 14 页
    # 投行风（LLM 出 27 字段 JSON → node render.mjs 渲染）。
    # 设 true 可回滚到旧版 prompt（HTML=Tailwind dark，PPT=LLM 直写 pptxgenjs js）—
    # 用于灰度对比 / 紧急止血。详见 `prompts.LEGACY_SKILL_PROMPTS`。
    use_legacy_html_pptx: bool = False

    # ── Agent runner（ADR-012：EchoDesk UI + AgentOS control plane）────
    agent_os_enabled: bool = False
    agent_os_url: str = "http://127.0.0.1:4128"
    agent_task_timeout_s: float = 1800.0
    agent_artifact_proxy_max_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    agent_bridge_lease_ttl_s: float = Field(default=30.0, gt=0, le=300.0)
    agent_bridge_heartbeat_s: float = Field(default=10.0, gt=0, le=60.0)
    agent_bridge_recovery_interval_s: float = Field(default=1.0, gt=0, le=60.0)
    agent_bridge_retry_base_s: float = Field(default=0.5, gt=0, le=60.0)
    agent_bridge_retry_max_s: float = Field(default=15.0, gt=0, le=300.0)
    agent_submit_lease_ttl_s: float = Field(default=90.0, gt=0, le=300.0)
    agent_submit_heartbeat_s: float = Field(default=10.0, gt=0, le=60.0)
    agent_cancel_command_lease_ttl_s: float = Field(default=30.0, gt=0, le=300.0)
    agent_cancel_command_retry_base_s: float = Field(default=0.5, gt=0, le=60.0)
    agent_cancel_command_retry_max_s: float = Field(default=15.0, gt=0, le=300.0)
    agent_cancel_command_max_attempts: int = Field(default=5, ge=1, le=20)

    # ── DB ────────────────────────────────────────────────────────
    db_path: Path = Field(default=Path("~/.echodesk/echodesk.db").expanduser())
    storage_dir: Path = Field(default=Path("~/.echodesk/storage").expanduser())

    # ── Security ──────────────────────────────────────────────────
    allowed_origins: str = (
        f"{OFFICIAL_ELECTRON_ORIGIN},app://.,capacitor://localhost,"
        "https://localhost,http://localhost,"
        "http://localhost:5173,http://localhost:8769,"
        "http://localhost:5174,http://127.0.0.1:5174,"
        "https://localhost:5174,https://127.0.0.1:5174"
    )
    # 旧版 packaged renderer 会发送 ``Origin: file://``。仅保留为显式开启的
    # loopback-only 升级兼容；当前桌面包固定使用 OFFICIAL_ELECTRON_ORIGIN。
    electron_file_origin_enabled: bool = False
    trusted_hosts: str = ""
    public_demo_mode: bool = False
    debug_token: str = Field(default="", repr=False)

    # Public transport admission runs before bearer/resource-ticket validation.
    # It is process-local by design: global limits bound one backend process,
    # while peer limits prevent one source from monopolizing those slots.
    preauth_window_s: float = Field(default=60.0, gt=0, le=3600)
    preauth_max_peers: int = Field(default=4096, ge=1)
    preauth_http_global_concurrent: int = Field(default=64, ge=1)
    preauth_http_peer_concurrent: int = Field(default=8, ge=1)
    preauth_http_global_requests_per_window: int = Field(default=6000, ge=1)
    preauth_http_peer_requests_per_window: int = Field(default=600, ge=1)
    preauth_ws_global_concurrent: int = Field(default=64, ge=1)
    preauth_ws_peer_concurrent: int = Field(default=8, ge=1)
    preauth_ws_global_attempts_per_window: int = Field(default=600, ge=1)
    preauth_ws_peer_attempts_per_window: int = Field(default=60, ge=1)

    # Durable public identity creation admission. Unlike the process-local
    # request limiter, these bounds survive restarts and serialize across every
    # backend process sharing SQLite. Existing enrollment retries and renewals
    # do not consume this new-identity budget.
    enrollment_admission_window_s: float = Field(default=60 * 60, gt=0)
    enrollment_admission_peer_max_per_window: int = Field(default=12, ge=1)
    enrollment_admission_global_max_per_window: int = Field(default=1_000, ge=1)
    enrollment_admission_peer_max_per_day: int = Field(default=64, ge=1)
    enrollment_admission_global_max_per_day: int = Field(default=10_000, ge=1)
    enrollment_admission_total_active_max: int = Field(default=10_000, ge=1)
    enrollment_admission_cleanup_batch_size: int = Field(default=100, ge=1, le=10_000)

    # ── Public backend resource governor ─────────────────────────
    # 累计预算按服务端 principal scope 记入 SQLite；并发/WS 是进程内租约。
    # local-first principal 不受这些 public 多租户护栏影响。
    quota_requests_per_minute: int = Field(default=240, ge=1)
    quota_concurrent_requests: int = Field(default=8, ge=1)
    quota_concurrent_expensive_tasks: int = Field(default=2, ge=1)
    quota_websocket_connections: int = Field(default=3, ge=1)
    quota_upload_bytes_per_day: int = Field(default=512 * 1024 * 1024, ge=1)
    quota_storage_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    quota_llm_tokens_per_day: int = Field(default=500_000, ge=1)

    # Low-level transcript injection is intentionally available to public/TV
    # clients for offline replay.  Keep one logical segment small enough that
    # the bounded WS replay buffer cannot be amplified by a single JSON value.
    meeting_inject_segment_max_bytes: int = Field(
        default=16 * 1024,
        ge=1024,
        le=1024 * 1024,
    )

    # principal-scoped Python runtimes and event streams must remain bounded.
    runtime_scope_max_entries: int = Field(default=256, ge=1)
    runtime_scope_idle_ttl_s: float = Field(default=30 * 60, gt=0)
    runtime_scope_janitor_interval_s: float = Field(default=60.0, gt=0)
    ws_scope_max_streams: int = Field(default=512, ge=1)
    # 全局 scope 满载时只允许有限个 distinct principal 候补，并在短窗口内
    # 按到达顺序接替释放的 slot；重复 scope 不重复占队列。
    ws_admission_queue_size: int = Field(default=128, ge=1, le=4096)
    ws_admission_wait_timeout_s: float = Field(default=2.0, gt=0, le=30.0)
    ws_subscriber_queue_size: int = Field(default=256, ge=1)
    ws_replay_buffer_size: int = Field(default=200, ge=1)
    ws_send_timeout_s: float = Field(default=5.0, gt=0, le=60.0)
    ws_auth_revalidate_interval_s: float = Field(default=15.0, gt=0, le=300)
    ws_client_frames_per_second: int = Field(default=20, ge=1, le=1000)
    execution_lease_ttl_s: float = Field(default=30.0, ge=5.0, le=300.0)
    execution_lease_heartbeat_s: float = Field(default=5.0, ge=1.0, le=60.0)
    # 所有可携带 body 的 HTTP 路由都在 Starlette 解析 JSON / multipart 前受保护。
    # 普通 JSON 保持较小上限；上传路由继续使用各自更大的专用上限。
    request_body_max_bytes: int = Field(
        default=1024 * 1024,
        ge=16 * 1024,
        le=64 * 1024 * 1024,
    )
    request_body_timeout_s: float = Field(default=30.0, gt=0, le=300.0)
    upload_read_chunk_bytes: int = Field(default=64 * 1024, ge=1024, le=4 * 1024 * 1024)
    upload_multipart_overhead_bytes: int = Field(default=1024 * 1024, ge=64 * 1024)
    # 为兼容现有部署保留历史字段名；这两个容量边界现在覆盖所有受保护的请求体，
    # 不再只覆盖 multipart 上传。
    upload_global_concurrent_requests: int = Field(default=16, ge=1)
    upload_global_inflight_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024)
    upload_body_timeout_s: float = Field(default=120.0, gt=0, le=900)
    # Public production cutovers create this owner-only token file before a
    # target process starts.  While it exists, business HTTP/WS is fail-closed
    # and only the local isolation smoke can bypass it with the token header.
    deployment_gate_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "deployment_gate_file",
            "DEPLOYMENT_GATE_FILE",
            "ECHODESK_DEPLOYMENT_GATE_FILE",
        ),
    )
    # Electron 会把 backend 绑定到 0.0.0.0 以支持手机/电视扫码保存。
    # 默认只允许局域网访问 share/minutes/download 等只读保存端点；如需让
    # Android/TV 调试完整本机后端，显式设置 ECHO_LAN_FULL_API_ENABLED=true。
    lan_full_api_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "lan_full_api_enabled",
            "LAN_FULL_API_ENABLED",
            "ECHO_LAN_FULL_API_ENABLED",
        ),
    )

    @property
    def allowed_origins_list(self) -> list[str]:
        configured = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        return list(dict.fromkeys((*configured, OFFICIAL_ELECTRON_ORIGIN)))

    @property
    def trusted_hosts_list(self) -> list[str]:
        configured = [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]
        if configured:
            return list(dict.fromkeys(configured))
        if not self.public_demo_mode:
            return ["*"]
        canonical_host = urlsplit(self.public_http_url).hostname
        return list(
            dict.fromkeys(
                host
                for host in (
                    canonical_host,
                    "echodesk.yoliyoli.uk",
                    "localhost",
                    "127.0.0.1",
                    "::1",
                    "testserver",
                )
                if host
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
