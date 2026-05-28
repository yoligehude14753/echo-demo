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

from pydantic import AliasChoices, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.config_io import JsonConfigSource

# 项目根目录的 .env（dev 期兜底；prod 装机后不强求存在）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILES = (_PROJECT_ROOT / ".env", Path(".env"))


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

    public_ws_url: str = "ws://localhost:8769/ws/echo"
    public_http_url: str = "http://localhost:8769"
    app_version: str = "0.2.0"

    # ── LLM 主通道（Yunwu / MiniMax-M2.7） ────────────────────────
    llm_main_provider: str = "yunwu"
    llm_main_model: str = "MiniMax-M2.7"
    llm_main_base_url: str = "https://yunwu.ai/v1"
    yunwu_open_key: str = ""
    llm_fallback_1: str = "GLM-4.6"
    llm_fallback_2: str = "Kimi-K2.6"
    llm_main_max_tokens: int = 80_000

    # ── LLM 快速通道（Qwen3-1.7B on heyi-bj） ────────────────────
    llm_fast_provider: str = "heyi-local"
    llm_fast_model: str = "Qwen3-1.7B"
    llm_fast_base_url: str = "http://100.87.251.9:7860/v1"
    llm_local_api_key: str = "EMPTY"
    llm_fast_max_tokens: int = 512

    # ── STT ───────────────────────────────────────────────────────
    # 当前**唯一** = firered（@ heyi :8090，判别式无幻觉、中文强）；
    # echo 实战路径 Deepgram → FireRed → faster-whisper → SenseVoice → 回 FireRed。
    # SenseVoice GPU 在 PR `echodesk-remove-sensevoice` 删除（理由：多 backend
    # 选项老让人误判"换 backend 能修 X"）。详见 docs/ARCH-AUDIT.md §2。
    # 保留 stt_backend 字段供未来扩展（如本地化离线 STT）。
    stt_backend: str = "firered"
    stt_firered_url: str = "http://100.87.251.9:8090"
    stt_language: str = "zh"
    stt_llm_correct: bool = False

    # ── TTS ───────────────────────────────────────────────────────
    # 实际跑的是 faster-qwen3-tts 1.7B CustomVoice @ heyi-bj :8094
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
        default="http://100.87.251.9:8094",
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

    # ── Speaker Diarization ──────────────────────────────────────
    diarizer_enabled: bool = True
    diarizer_backend: str = "ecapa"
    # 0.70 对齐 echo `config.py:306`（实测推荐）；之前 0.65 在 6s 含噪 chunk 上
    # 过严 → 同人被切成多人。详见 docs/ARCH-AUDIT.md §4 root #3。
    diarizer_match_threshold: float = 0.70
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
    # spk-6 保守预设：0.50 → 0.60。trade-off：短段更难"回退已知人"，多段被丢
    # （丢段无害，文本仍由 STT 出，只是没 speaker_label） vs 拉飘 centroid（污染）。
    # 仍 < diarizer_match_threshold (0.70) 维持"短段比正常段更严"语义。
    diarizer_outlier_match_threshold: float = 0.60

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
    # 12 → 10：中文正常 4-8 cps，>10 大概率复读/幻觉（仅对 ≥3s 且 ≥12 chars 文本生效）
    ambient_max_cps: float = 10.0
    # STT 输出最短字符数（小于此值丢弃，防止单字幻觉污染 RAG/speaker registry）
    # 4 → 5：echo 路由层用 3（router）+ 下游 dream consolidator 用 8 双重过滤；
    # echo-demo 单点过滤，取中位数 5 → 拦截 "嗯。" / "Yeah" / "ですね" 等短幻觉
    ambient_min_stt_chars: int = 5

    # ── 会议自动检测（与 PRD §自动开会/自动结束 对齐） ──────────
    # 检测窗口；distinct speakers 需 ≥ min_distinct 且总语音 ≥ min_active_s
    automeet_window_s: float = 30.0
    automeet_min_distinct_speakers: int = 2
    automeet_min_active_seconds: float = 6.0
    # 静默 X 秒 → 自动 end（含 finalize 纪要）
    automeet_silence_timeout_s: float = 30.0
    # 自动结束后多久内不再触发新会议（防抖）
    automeet_cooldown_s: float = 60.0
    # 单个 auto-meeting 的硬上限（兜底）：超过 X 秒一律 force-end，
    # 防止持续环境音 / 单人独白让会议永不结束（2026-05 「会议中 9h+」bug 的结构性修复）。
    # hydrate 也会用这个值清理跨重启遗留的过期 auto-meeting。
    automeet_max_meeting_duration_s: float = 1800.0

    # ── RAG ───────────────────────────────────────────────────────
    rag_index_dir: Path = Field(default=Path("~/.echodesk/rag_index").expanduser())
    rag_top_k: int = 5
    rag_pdf_chunk_tokens: int = 600
    rag_pdf_chunk_overlap: int = 100

    # ── 授权工作区（M6：用户配置可索引的目录范围） ────────────
    # 多个目录用逗号分隔，例如 ECHO_WORKSPACE_DIRS=~/Documents/work,~/Notes
    workspace_dirs: str = ""
    workspace_max_file_mb: float = 20.0
    workspace_scan_on_startup: bool = True
    workspace_state_file: Path = Field(
        default=Path("~/.echodesk/workspace_state.json").expanduser()
    )

    @property
    def workspace_dirs_list(self) -> list[Path]:
        return [Path(d.strip()).expanduser() for d in self.workspace_dirs.split(",") if d.strip()]

    # 用户拖入的最大上传大小（用户拖入；workspace 配置走 workspace_max_file_mb）
    upload_max_file_mb: float = 50.0

    # ── Web Search（Tavily 主 + DDG 兜底，2026-05-26 用户决策） ──
    web_search_enabled: bool = True
    web_search_top_n: int = 5
    tavily_api_key: str = ""
    web_arbitration_model: str = "Qwen3-1.7B"

    # ── Skill 执行器 ──────────────────────────────────────────────
    skill_ppt_tool: str = "pptxgenjs"
    skill_word_tool: str = "python-docx"
    skill_xlsx_tool: str = "openpyxl"
    skill_xlsx_recalc: str = "libreoffice"
    skill_html_tool: str = "single-file-tailwind-cdn"
    skill_fix_max_retries: int = 3
    skill_node_bin: str = "node"
    skill_executor_build_dir: Path = Field(default=Path("~/.echodesk/skill_build").expanduser())
    skill_executor_timeout_s: int = 300
    skill_executor_max_tokens: int = 80_000

    # ── DB ────────────────────────────────────────────────────────
    db_path: Path = Field(default=Path("~/.echodesk/echodesk.db").expanduser())
    storage_dir: Path = Field(default=Path("~/.echodesk/storage").expanduser())

    # ── Security ──────────────────────────────────────────────────
    allowed_origins: str = "app://.,http://localhost:5173,http://localhost:8769"
    debug_token: str = ""

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
