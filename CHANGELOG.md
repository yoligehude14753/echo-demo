# Changelog

EchoDesk 桌面端的用户可见变更（User-Facing Changes）。

格式宽松遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，版本号语义化 ([SemVer](https://semver.org/lang/zh-CN/))。

> 仅记录会改变交互、可观察行为或配置形态的变更。纯重构 / 测试 / CI / 内部文档不列出。

---

## [Unreleased]

### 新增（P4.1 M4 · 产物预览）

- **7 类产物 in-app 预览**
  点击 ArtifactPanel 任一卡片直接在应用内 Modal 预览，不必再下载：
  - `html` / `pdf` → `<iframe>`（浏览器原生 PDF viewer）
  - `markdown` → `react-markdown` + `remark-gfm`（GFM 表格 / 代码块）
  - `txt` → `<pre>` 等宽字体
  - `word` / `docx` → `mammoth` 解析 → 隔离 `<iframe srcDoc>` 渲染（CSS 不污染主应用）
  - `xlsx` → SheetJS 解析 + sheet tab 切换（动态 import，避免拖累主 bundle）
  - `pptx` → 浏览器无法原生渲染，调 Electron `shell.openPath` 用 macOS Keynote 打开
- **ArtifactPanel 顶栏「清空 outputs」按钮**
  历史卡片堆积时一键清空（保留失败卡片 + 文件本身仍在磁盘）；走 `Modal.confirm`
  二次确认避免误清。
- **单条 hover「×」删除按钮**
  跟 `Download` 按钮一样仅在 hover 时显示；删错代价低（仅从面板移除引用，
  不删磁盘文件）所以不二次确认。
- **列表展示 title 主、artifact_id 副**
  M3 引入的 `title` 字段（如 `FY26 Outlook 摘要`）作为卡片主标题；UUID 退化为
  14 字符截断的 mono 副文本 + tooltip 含完整 ID。Title 缺失时退回完整 UUID。

### 修复

- 旧 `artifact-generate` e2e / `s04_meeting_and_artifact` 适配新 ArtifactPanel：
  artifact_id 不再完整渲染在卡片上，测试用 `data-artifact-id` selector 锚定。
- `TranscriptStream` 顺手清理一条 pre-existing eslint warning（复合表达式 dep
  提取为变量，行为等价）。

### 配置变更

- Electron preload 新增 `window.echo.openArtifactInSystem(filePath)` IPC bridge；
  主进程暴露 `echo:open-artifact-in-system` 调 `shell.openPath`，仅用于产物预览。

### 计划中

- P3.6 应用图标 + dmg 背景刷一刷
- P3.7 自动更新检查（仅检查 latest release，不自动下载）
- P4.2 keychain 集成（API key 不再以明文落 user.json）
- P4.3 macOS Universal Binary（arm64 + x64 合并）

---

## [0.2.3] – 2026-06-23

Smart TV install hotfix：把 v0.2.2 的 Android TV 兼容继续补成面向会议室电视的一键安装交付。

### 新增

- 新增 `EchoDesk-0.2.3-smart-tv.apk`，作为面向 Android / AOSP 智能电视的直接安装包名。
- 新增 `EchoDesk-0.2.3-smart-tv-oneclick.zip`，内含 APK、macOS ADB 安装脚本和 Windows PowerShell 安装脚本。
- 新增 `docs/tv-install.html`，电视浏览器可用遥控器打开大按钮下载 APK 或一键安装包。
- 新增 `docs/TV_INSTALL.md`，明确 Android TV / 国产 Android TV / AOSP TV / 非 Android TV 的兼容边界。
- 新增 TV 安装页 e2e，覆盖 1920x1080 电视视口、下载链接、遥控器焦点和复制安装命令交互。

### 修复

- 安装文档统一到 `0.2.3` TV 资产命名，避免 debug APK、smart TV APK 和 Release 名称不一致。

### 已知问题

- 一键安装依赖电视开启 ADB 网络调试；不支持 ADB 的电视仍需浏览器下载或 U 盘安装。
- Samsung Tizen、LG webOS、Apple TV 不能安装 APK，需要外接 Android 盒子或后续浏览器/PWA 版本。

---

## [0.2.2] – 2026-06-22

TV compatibility hotfix：让 Android 包能在会议室智能电视 / Android TV 上直接安装、出现在电视桌面并用遥控器完成核心操作。

### 新增

- Android manifest 增加 `LEANBACK_LAUNCHER`，电视桌面可直接显示 EchoDesk。
- Android 包增加 TV banner，避免电视应用列表里只出现默认手机图标。
- 声明触摸屏 / 麦克风为非必需硬件，兼容没有触摸屏或没有内置麦克风的会议室电视。
- 新增 1920x1080 TV 视口模拟点击测试，覆盖电视横屏三栏布局、知识库入口、设置入口和遥控器确认键路径。

### 修复

- 大横屏下放大顶部状态、工作区栏、命令输入区和右侧产物/纪要区域，避免电视远距离观看时过密。
- 增加全局 focus-visible 焦点环；电视遥控器移动焦点时能看清当前选中控件。
- 知识库目录 tag 支持键盘 / 遥控器 Enter 打开，不再只支持鼠标点击。

### 配置变更

- 版本号统一到 `0.2.2`，Android APK 使用 `versionCode=202`、`versionName=0.2.2`。

### 已知问题

- TV 包仍是 debug APK，适合会议室内测 / 侧载；正式分发需要 release 签名 APK/AAB。
- 电视端需要能访问 EchoDesk backend。若 backend 在电脑上运行，需在设置里填写电视可访问的局域网地址。

---

## [0.2.1] – 2026-06-18

Demo hotfix：补齐用户反馈的知识库可见性、远场转写诊断、移动端演示包和远端模型迁移。

### 新增

- 工作区 / 知识库面板展示已索引文档、chunk 数、文档来源，并支持单条删除与打开设置。
- 设置面板新增移动端连接配置，Android debug APK 默认连接模拟器宿主机 `10.0.2.2:8769`。
- 捕获状态面板展示最近 RMS、语音帧比例和门控原因，便于定位“离远了声音记录不清楚”是麦克风输入、门控还是 STT 识别问题。

### 修复

- 移动端窄屏布局不再因为 Ant Design sider 样式压成 `width: 0`。
- `WORKSPACE_MAX_FILE_MB` 默认提高到 100MB，避免常见 PDF 被知识库扫描静默跳过。
- “授权工作区”相关文案收敛为“知识库 / 工作区”，避免被误解为激活码；当前 demo 不设激活码门槛。

### 配置变更

- STT / TTS / Fast LLM 默认迁到 eight (`100.76.3.59`)：
  - STT: `http://100.76.3.59:8090`
  - TTS: `http://100.76.3.59:8094`
  - Fast LLM: `http://100.76.3.59:7860/v1`, model `qwen3.5-9b-local`
- `.env.example` 去掉真实 API key 示例，发布源码包只保留空占位。
- 版本号统一到 `0.2.1`，Android debug APK 使用 `versionName=0.2.1`。

### 已知问题

- Android 包是 debug APK，仅用于内部 demo；正式上架需 release 签名 APK/AAB。
- macOS / Windows 包仍未做正式代码签名；首次打开可能需要系统安全确认。

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
  Yunwu / 远端 fast LLM 任一不可用时 backend 自动降级；
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
- **STT / TTS / LLM**：FireRedASR2-AED + Qwen3 TTS +
  Yunwu MiniMax-M2.7（主）+ fast Qwen 通道。
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
