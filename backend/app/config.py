"""集中式配置：所有外部依赖与运行参数走这里，业务层只读 Settings。

源码层级：infra（最底层），可被任何层 import；不得反向 import 任何上层模块。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录的 .env（无论从 backend/ 还是项目根启动都能找到）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILES = (_PROJECT_ROOT / ".env", Path(".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Server ────────────────────────────────────────────────────
    port: int = 8765
    log_level: str = "INFO"

    public_ws_url: str = "ws://localhost:8765/ws/echo"
    public_http_url: str = "http://localhost:8765"
    app_version: str = "demo-0.1.0"

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
    stt_backend: str = "sensevoice_gpu"
    stt_sensevoice_gpu_url: str = "http://100.87.251.9:8093"
    stt_sensevoice_device: str = "cpu"
    stt_language: str = "zh"
    stt_llm_correct: bool = False

    # ── TTS ───────────────────────────────────────────────────────
    tts_enabled: bool = True
    tts_provider: str = "cosyvoice"
    tts_cosyvoice_url: str = "http://100.87.251.9:8094"
    tts_cosyvoice_voice: str = "aiden"

    # ── Speaker Diarization ──────────────────────────────────────
    diarizer_enabled: bool = True
    diarizer_backend: str = "ecapa"
    diarizer_match_threshold: float = 0.65
    diarizer_min_audio_bytes: int = 16_000

    # ── 音频预过滤（防 STT 幻觉 + speaker 编号爆炸；移植自 echo）─────
    # 整段 RMS 门控：低于此值视为底噪 → 跳过 STT/diarizer，整 chunk 丢弃
    ambient_rms_gate: int = 600
    # 帧级 VAD：20ms 帧统计；帧 RMS > 阈值算"活跃"
    ambient_frame_rms_threshold: int = 400
    # 活跃帧比例 < 此值 → 跳过 STT
    ambient_min_speech_frame_ratio: float = 0.05
    # STT 后 cps 门控：字符速率 > 此值视为幻觉/复读丢弃
    ambient_max_cps: float = 12.0
    # STT 输出最短字符数（小于此值丢弃，防止单字幻觉污染 RAG/speaker registry）
    ambient_min_stt_chars: int = 4

    # ── 会议自动检测（与 PRD §自动开会/自动结束 对齐） ──────────
    # 检测窗口；distinct speakers 需 ≥ min_distinct 且总语音 ≥ min_active_s
    automeet_window_s: float = 30.0
    automeet_min_distinct_speakers: int = 2
    automeet_min_active_seconds: float = 6.0
    # 静默 X 秒 → 自动 end（含 finalize 纪要）
    automeet_silence_timeout_s: float = 30.0
    # 自动结束后多久内不再触发新会议（防抖）
    automeet_cooldown_s: float = 60.0

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
    allowed_origins: str = "app://.,http://localhost:5173,http://localhost:8765"
    debug_token: str = ""

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
