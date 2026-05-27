# Changelog

EchoDesk 桌面端的用户可见变更（User-Facing Changes）。

格式宽松遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，版本号语义化 ([SemVer](https://semver.org/lang/zh-CN/))。

> 仅记录会改变交互、可观察行为或配置形态的变更。纯重构 / 测试 / CI / 内部文档不列出。

---

## [Unreleased]

### 计划中

- P3.6 应用图标 + dmg 背景刷一刷
- P3.7 自动更新检查（仅检查 latest release，不自动下载）
- P4.1 keychain 集成（API key 不再以明文落 user.json）
- P4.2 macOS Universal Binary（arm64 + x64 合并）

---

## [0.2.0] – 2026-05-28

P2 / P3 阶段集中迭代：可视化诊断、远端服务可配置、首次启动引导。

### 新增

- **首次启动 3 步引导**（P3.1）
  双击 `EchoDesk.app` 后首次自动展示：欢迎 → 麦克风授权 → 数据目录确认；
  完成后落 `localStorage` 不再重复弹。设置面板里有「回放引导」按钮便于演示。
- **macOS 麦克风权限补救**（P3.5）
  状态栏 mic pill 在 `denied` 时显示「打开系统设置」按钮，一键深链到
  「系统设置 → 隐私与安全 → 麦克风」。Electron 主进程通过
  `systemPreferences.getMediaAccessStatus("microphone")` 暴露权限态。
- **远端服务可配置**（P3.2）
  设置面板新增「远端服务」section，可直接修改 `llm_main_base_url` /
  `yunwu_open_key` / `llm_fast_base_url` / `stt_firered_url` /
  `tts_qwen3_url` / `tts_qwen3_voice` / `tavily_api_key` 7 项。
  - 后端：`GET /admin/settings/remote` 返回脱敏值 + `source=default|user`；
    `PATCH /admin/settings/remote` 合并写入 `~/.echodesk/config.json`，
    任何非白名单 key 一律 422 整体拒绝（不部分写）。
  - 保存后弹「需重启 backend 生效」按钮，调 Electron `manualRestartBackend` IPC。
- **关于对话框**（P3.3）
  顶栏 `v0.2` 徽章可点，展示前后端版本、`/healthz/full` 简要、
  CHANGELOG 链接、INSTALL.md 链接。
- **状态栏诊断 pill**（P2.1）
  顶栏新增 4 个 pill：mic / db / remote / backend，
  鼠标悬停看明细，红/黄/绿 5s 内反映 `/healthz/full` 状态。
- **`@生成` 失败保护**（P2.2）
  LLM / Skill 失败时前端弹错误 toast，textarea 不再卡死；后端推送
  `artifact.failed` 事件，含 `reason` + `intent`。
- **远端降级链路**（P2.3）
  Yunwu / heyi-bj 任一不可用时 backend 自动切到本地 Qwen3-1.7B；
  顶栏 remote pill 显示「降级中」并附理由。
- **DB migration 框架**（P2.4）
  SQLite schema 改动统一走 `backend/app/adapters/repo/migrations/`，
  启动时自动执行；旧 DDL 内联代码移除。
- **管理 API**（P2.5）
  `GET /admin/data-dir` 暴露 `~/.echodesk` 解析结果与可读子目录；
  `POST /admin/open-data-dir` 在 Electron 模式打开 Finder。
- **诊断打包导出**（P2.6）
  设置面板「下载诊断包」一键打包近 7 天 log + healthz 快照 + 版本信息为 zip，
  方便上报问题。**不**包含数据库 / 录音 / config（避免泄露 key）。

### 修复

- `artifact-generate` e2e 适配 P2.2 新 `@生成` 命令式流程（不再点 ArtifactPanel
  按钮，改输入命令）。
- backend CI 缺 `aiosqlite==0.20.0` 导致 typecheck / unit 红：补依赖；
  `try-except-pass` 统一改用 `contextlib.suppress`；架构 fitness 测试白名单
  显式标记 `ambient_capture → audio_gate` 的暂留依赖（TODO 1 行）。
- `desktop / e2e (playwright)` 在仅改 `ci.yml` 时误触发：paths-filter
  排除 `.github/workflows/**`，e2e 只对 `desktop/**` 真改动跑。

### 配置变更

- `~/.echodesk/config.json` 新增 7 个白名单字段可被 PATCH 覆盖（见 P3.2）。
- 麦克风权限不再依赖第一次录音才请求，可由引导主动触发（macOS only）。

### 已知问题

- 后端 settings 是进程级单例，PATCH `/admin/settings/remote` 后必须重启
  backend 才能生效（前端已显式提示，但操作多 1 步）。
- API key 在 GET 时脱敏，PATCH body 仍是明文走 HTTP；本地 backend 仅监听
  127.0.0.1，公网泄露面 = 0。Keychain 集成留到 0.3.x。

---

## [0.1.0] – 2026-05-20

EchoDesk Phase 1 最小可用版（M1–M4 合并）。

### 新增

- **持续监听 + 会议控制**：常驻 `ambient_capture`，按下「开始会议」时把当前
  缓冲音段绑进 meeting；停止时调用 `finalize_meeting` 生成全文 + 纪要。
- **9 类 intent 路由**：`@生成`（HTML/PPT/Word/Excel）、`@查`（联网检索）、
  `@总结`、`@翻译`、`@纪要`、`@问`、`@搜`、`@分析`、`@生成图`，由 `intent/router`
  统一分发到 LLM / Skill / Web search。
- **一键产物**：
  - Word：python-docx + SKILL.md prompt，真 TOC + List style + 上标引用。
  - Excel：openpyxl + Source 列，4 sheet DCF / 126 公式 / 46 跨 sheet / 0 errors。
  - HTML：single-file + Tailwind CDN，66K 字符 / 144 卡片块 / SVG 可视化。
  - PPT：pptxgenjs + Midnight 色板，417 视觉 shapes / notes 772 字/页。
- **多文档 + 会议 RAG**：jieba 分词 + BM25Okapi，9 query 并发 1.28s，
  `doc_cite=100%`。
- **声纹识别**：SpeechBrain ECAPA-TDNN 默认参数，本地 CPU 推理。
- **STT / TTS / LLM**：FireRedASR2-AED + Qwen3 TTS（heyi-bj 100.87.251.9）+
  Yunwu MiniMax-M2.7（主）+ 本地 Qwen3-1.7B（fast）。
- **Web Search 仲裁**：Inspiro 主 + Tavily 备 + DDG 兜底。
- **Electron + React 18 UI**：Ant Design 5 + Tailwind，WebSocket 推送会议状态
  + 笔记；BackendSupervisor 自动 spawn / 监控 / 重启 Python backend。
- **一键安装脚本**：`scripts/install-backend.sh` → 创 `~/.echodesk/`、装 venv、
  smoke test、写默认 `config.json`，支持 `--uninstall` / `--reset-config`。
- **完整 E2E**：88 unit + 4 真服务 integration 全过，ruff / mypy 0 错误。

---

## [0.0.x] – Echo Demo 时代（已归档）

EchoDesk 前身 `echo` 仓库的 v6.7.1 PRD 验证产物，已迁入 `experiments_baseline/` 只读保留。
仅作为技术决策的实测出处，不再单独维护。
