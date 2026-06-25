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
    app_version: str = "0.2.19"

    # ── LLM 主通道（Yunwu / MiniMax-M2.7） ────────────────────────
    llm_main_provider: str = "yunwu"
    llm_main_model: str = "MiniMax-M2.7"
    llm_main_base_url: str = "https://yunwu.ai/v1"
    yunwu_open_key: str = ""
    llm_fallback_1: str = "GLM-4.6"
    llm_fallback_2: str = "Kimi-K2.6"
    llm_main_max_tokens: int = 80_000
    # 会议纪要是结构化 JSON，不应复用 MAIN/skill 的 80k 长推理预算。
    # public demo 可把主模型临时切到 eight 的 qwen3.5-9b-local；该模型
    # max_model_len=16384，80k 会直接 400。12k 给 JSON 纪要足够，同时
    # 留出 prompt 余量。
    minutes_max_tokens: int = 12_000

    # ── LLM 快速通道（qwen3.5-9b-local on eight） ─────────────────
    llm_fast_provider: str = "eight-local"
    llm_fast_model: str = "qwen3.5-9b-local"
    llm_fast_base_url: str = "http://100.76.3.59:7860/v1"
    llm_local_api_key: str = "EMPTY"
    llm_fast_max_tokens: int = 512

    # ── STT ───────────────────────────────────────────────────────
    # 当前**唯一** = firered（@ eight :8090，判别式无幻觉、中文强）；
    # echo 实战路径 Deepgram → FireRed → faster-whisper → SenseVoice → 回 FireRed。
    # SenseVoice GPU 在 PR `echodesk-remove-sensevoice` 删除（理由：多 backend
    # 选项老让人误判"换 backend 能修 X"）。详见 docs/ARCH-AUDIT.md §2。
    # 保留 stt_backend 字段供未来扩展（如本地化离线 STT）。
    stt_backend: str = "firered"
    stt_firered_url: str = "http://100.76.3.59:8090"
    stt_language: str = "zh"
    stt_llm_correct: bool = False

    # ── STT 后处理：LLM 补标点 + 分段 ──────────────────────────────
    # 用户痛点（2026-05-28）：FireRedASR2 :8090 OpenAPI 只接受
    # file/model/language/response_format/timestamp_granularities，**没有 punc 开关**；
    # 6s ambient chunk 出来是一气呵成 30+ 字无标点的整行（截图 m-bdd1da4e7e21），
    # 用户读不下去。STT 服务端无法直出标点 → ambient 主链路加 qwen3.5-9b-local (LLM_FAST)
    # 后处理批量加标点 + 自然分段。
    # 详见 `app/adapters/stt/llm_punctuator.py` 文件头。
    ambient_llm_punctuate: bool = True
    # 单次 batch（一个 chunk 1-3 段）超时上限；超时 → 退回原文本不阻塞主链路。
    # qwen3.5-9b-local p50 < 700ms，2s 是宽松上限。
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
    # 单文件上限。20 → 100：2026-05-28 用户痛点 —— 实测 102MB 文件夹里两个 30-40MB
    # PDF 被 size 过滤静默丢弃（scanner 报 added=N，但 N 已含 size-skip 数差），
    # markitdown/pdfplumber 对 100MB 以内的中文营销/技术 PDF 都能秒级抽取，
    # 把默认放宽到 100MB，更贴合"我授权一个文件夹，常见 PDF 都会进 RAG"的直觉。
    # 仍可通过 ECHO_WORKSPACE_MAX_FILE_MB env 覆盖。
    workspace_max_file_mb: float = 100.0
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
    web_arbitration_model: str = "qwen3.5-9b-local"

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

    # ── phase4-doc-skills：HTML/PPT 高质量 skill 灰度开关 ───────────
    # 默认 false：HTML 走 Kami warm-parchment one-pager；PPT 走 ib_master 14 页
    # 投行风（LLM 出 27 字段 JSON → node render.mjs 渲染）。
    # 设 true 可回滚到旧版 prompt（HTML=Tailwind dark，PPT=LLM 直写 pptxgenjs js）—
    # 用于灰度对比 / 紧急止血。详见 `prompts.LEGACY_SKILL_PROMPTS`。
    use_legacy_html_pptx: bool = False

    # ── DB ────────────────────────────────────────────────────────
    db_path: Path = Field(default=Path("~/.echodesk/echodesk.db").expanduser())
    storage_dir: Path = Field(default=Path("~/.echodesk/storage").expanduser())

    # ── Security ──────────────────────────────────────────────────
    allowed_origins: str = (
        "app://.,capacitor://localhost,https://localhost,http://localhost,"
        "http://localhost:5173,http://localhost:8769"
    )
    public_demo_mode: bool = False
    debug_token: str = ""
    # Electron 会把 backend 绑定到 0.0.0.0 以支持手机/电视扫码保存。
    # 默认只允许局域网访问 share/minutes/download 等只读保存端点；如需让
    # Android/TV 调试完整本机后端，显式设置 ECHO_LAN_FULL_API_ENABLED=true。
    lan_full_api_enabled: bool = False

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
