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

## [0.2.9] – 2026-06-24

智能电视 / public demo hotfix：修复小米 Android 9 TV 上 WebView 白屏、比例错位、
新安装继承公共历史、遥控确认键和设置抽屉不可见的问题。

### 修复

- Vite 生产构建目标下调到 `chrome61`，把 optional chaining / nullish coalescing 等
  现代语法转译掉，兼容会议室电视内置较旧 WebView。
- TV 视口按 MiTV 实测 `960x540` CSS viewport 重新适配，避免把 1920x1080 物理屏
  当桌面大屏导致三栏比例和字体失控。
- 为旧 Android WebView 增加 Ant Drawer fixed fallback，设置/工作区配置抽屉不再渲染到屏幕下方。
- 为 TV 遥控器增加 Enter/Space → click bridge，焦点到按钮后按确认键能打开设置等面板。
- Android / TV public demo 默认隐藏共享 `/meetings` 和 `/capture/recent`，并丢弃公共 backend
  的共享 WebSocket 业务事件；新装设备只显示本机本次 capture 返回的实时转写。
- TV 一键安装脚本默认执行 `pm clear`，清理旧 WebView/localStorage/cache；保留配置升级可设
  `ECHODESK_TV_KEEP_DATA=1`。
- Android manifest 关闭 backup，避免系统备份恢复旧 WebView 数据。
- 已在 `MiTV-ASTP0`（Android 9，IP `10.10.12.25`）通过 ADB 覆盖安装验证：
  v0.2.8 旧包 logcat 报 `Unexpected token ?` 并白屏；新包不再报语法错误，主界面正常显示，
  18 秒 WS replay 窗口后不再出现公共历史会议。
- 实机音频检查确认 `com.echodesk.app` 以 `VOICE_COMMUNICATION` 打开麦克风，
  `1ch 48000Hz PCM_16BIT`；Mac 扬声器测试音没有穿过后端 RMS 门控，stats 仍显示
  `last_gate_reason=rms_too_low`，说明远场输入强度/麦克风位置仍需现场校准。

### 验证

- `npm run build`
- `npm run typecheck`
- `npm run lint`
- `npm run app:dist:android`
- `npm run app:package:tv`
- `npx playwright test tests/e2e/tv-layout.spec.ts tests/e2e/tv-share.spec.ts tests/e2e/public-demo-settings.spec.ts tests/e2e/workspace-knowledge.spec.ts tests/e2e/acceptance-clickthrough.spec.ts`
- `backend/.venv/bin/python -m pytest backend/tests/unit/test_ws_endpoint.py`
- ADB 真机清数据安装并启动，截图确认 EchoDesk v0.2.9 主界面、设置抽屉和无共享历史。

## [0.2.8] – 2026-06-24

自动会议识别 / 顶栏点击区域 hotfix：修复远场或声纹不稳定时，STT 已经持续输出文本但
自动会议迟迟不开的问题；同时扩大会议状态按钮的点击区域并统一 auto 状态文案。

### 修复

- 自动会议检测保留「≥2 个明确 speaker」优先规则；当声纹暂时无法稳定产出
  `speaker_id` 时，连续有效语音累计达到更保守阈值后也会自动开始记录。
- 有效语音但 `speaker_id=None` 时会刷新 last voice time，避免自动会议中途被错误判静默。
- 顶栏会议状态按钮改为 48px 高、最小 112px 宽的整块可点击区域，减少点不到/误点。
- auto 会议文案从「持续监听」改为「自动记录中」，明确已经在记录。
- STT 熔断提示按退避时间自动过期；即使后续暂无成功探测响应，UI 也不会一直挂
  “STT 熔断”红条。
- 采集上传增加请求序号保护，避免旧的 `circuit_open` 响应晚于新成功响应返回后重新污染状态。

### 验证

- 新增 AutoMeetingDetector fallback 单测。
- 扩展 MeetingStatusBar e2e，覆盖 auto 文案和点击区域尺寸。
- 扩展 CaptureStatus e2e，覆盖 STT 熔断退避到期自动清除。

## [0.2.7] – 2026-06-24

扫码保存 / 工作区入口 hotfix：修复手机或电视扫码保存会议资料时拿到 `127.0.0.1`
导致无法打开的问题，同时收口工作区配置入口和底部输入栏排版。

### 新增

- 扫码分享页新增「保存纪要.md」下载入口，手机/电视打开分享页后可直接保存会议纪要。
- Electron 新增 `getShareBackendHost` IPC：二维码优先使用电脑局域网地址，避免把本机
  loopback 地址生成到二维码里。
- 顶部工作区栏新增显式「配置工作区」按钮，新用户不必猜 `1 目录` 标签可点击。

### 修复

- 打包版后端支持监听局域网以服务扫码保存；局域网访问默认只放行分享页、纪要下载、
  产物下载和 healthz，其它管理 / STT / TTS / 上传 / 配置 API 仍限制在本机。
- 分享弹窗增加网络可达性提示，检测到 loopback 链接时明确告知只能本机打开。
- 扫码分享页和产物下载补强 path 校验，避免非法 artifact id 逃逸下载目录。
- 底部对话输入栏统一字体、字号和行高；TV 大屏不再跳到过大的 18px。

### 配置变更

- 桌面 / 后端版本升到 `0.2.7`。
- Android 版本升到 `versionCode=207`、`versionName=0.2.7`。
- 如需让 Android/TV 调试完整本机后端，需显式设置
  `ECHO_LAN_FULL_API_ENABLED=true`；普通扫码保存不需要。

## [0.2.6] – 2026-06-24

STT stability hotfix：修复 eight STT 偶发慢响应时，桌面端误进入分钟级“云端 STT 熔断 · 暂停上传”的问题。

### 修复

- FireRed STT adapter 不再做本地熔断；远端偶发超时按单次失败处理，避免正常有文本输出时仍显示熔断。
- Ambient capture pipeline 增加 STT single-flight 闸：上一条 STT 请求未结束时，新分片快速记为 `failed`，不继续并发打 eight。
- 前端 capture router 对 `circuit_open` 做连续 3 次去抖，且最长退避从 5 分钟缩短到 30 秒，避免短抖动被放大成用户可见长暂停。
- Playwright 增加模拟分片测试：连续 2 次偶发 `circuit_open` 不展示“云端 STT 熔断”红条。

### 配置变更

- 桌面 / 后端版本升到 `0.2.6`。
- Android 版本升到 `versionCode=206`、`versionName=0.2.6`。

---

## [0.2.5] – 2026-06-23

Public demo backend hotfix：让 Android / TV 版本默认连公网 EchoDesk demo backend，
外部用户安装后可直接使用，同时不把模型 key 打进客户端包。

### 新增

- Android / TV 默认后端地址改为 `https://echodesk.yoliyoli.uk`，不再要求用户先在同局域网启动 Mac backend。
- 后端新增 `PUBLIC_DEMO_MODE`：公网 demo 模式下 `/admin/*` 默认禁止访问，避免暴露本机路径、日志和远端 key 配置入口。
- `/admin/*` 在 public demo 模式下仅接受服务端配置的 `DEBUG_TOKEN`，支持 `Authorization: Bearer ...` 或 `X-Echo-Admin-Token`。

### 配置变更

- Android 版本升到 `versionCode=205`、`versionName=0.2.5`。
- 桌面包版本升到 `0.2.5`。

### 已知问题

- 客户端仍不内置任何模型 key；公网 backend 可以保护 key，但不能完全阻止抓包复用接口，后续需要设备注册、限流和签名校验增强。
- Mac DMG / Windows EXE / Android APK 仍是 demo 分发形态；正式商店分发还需要签名、notarization 和发布渠道账号。

---

## [0.2.4] – 2026-06-23

TV meeting-room hotfix：补齐智能电视安装后的值守能力，以及会后扫码保存/清理会议资料。

### 新增

- Android TV APK 增加开机自启 receiver：设备重启后自动尝试拉起 EchoDesk，适合会议室常驻大屏。
- 会议结束后右侧纪要区新增「扫码保存」入口：大屏生成 QR，手机扫码打开轻量分享页，可保存会议纪要并下载产物。
- 分享页支持合并会议纪要 todo 中关联的产物，以及当前前端会话已知的本会议产物。
- 扫码弹窗提供复制链接、打开分享页、下载 Markdown 纪要、删除本会议输出。

### 修复

- 修复会议待办「执行」生成产物时丢失 `meeting_id` / `todo_id` 的问题；产物现在能正确归属本会议并回写 todo。
- 普通会议中 `@生成 PDF/PPT/Excel/...` 也会携带当前 `meeting_id`，避免产物只进入全局 outputs。

### 配置变更

- Android 版本升到 `versionCode=204`、`versionName=0.2.4`。
- 新增前端依赖 `qrcode` / `@types/qrcode`，仅在打开扫码弹窗时动态加载 QR 生成逻辑。

### 已知问题

- 开机自启受 Android TV 厂商限制：部分电视需要在系统设置中允许自启动/后台启动，或不能在 boot broadcast 后自动拉起 Activity。
- APK 仍是会议室内测 debug 包；正式外部分发需要 release 签名与 HTTPS backend。
- 非 Android TV（Samsung Tizen、LG webOS、Apple TV）仍不能直接安装 APK。

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
