# Changelog

EchoDesk 桌面端的用户可见变更（User-Facing Changes）。

格式宽松遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，版本号语义化 ([SemVer](https://semver.org/lang/zh-CN/))。

> 仅记录会改变交互、可观察行为或配置形态的变更。纯重构 / 测试 / CI / 内部文档不列出。

---

## [0.2.26] – 2026-06-27

TV / 老 Android WebView 兼容热修复：为 Android / TV 打包产物补上 Vite legacy
`nomodule` fallback，避免部分 Android 8 会议电视只支持较旧 WebView 时打开 APK
停在白屏 / “EchoDesk 正在启动…”。

### 修复

- 前端生产构建启用 `@vitejs/plugin-legacy`，同时保留现代 `type=module` bundle；
  桌面新浏览器继续走现代包，旧 TV WebView 走 legacy + SystemJS/polyfill fallback。
- TV e2e 重新验证横屏 960×540 布局、遥控器确认键路径、扫码保存会议资料、删除输出、
  待办生成产物携带 `meeting_id` / `todo_id`。

### 验证

- `npm run typecheck`
- `npm run lint`
- `npm run build`
- `npx playwright test tests/e2e/tv-layout.spec.ts tests/e2e/tv-share.spec.ts`

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
- P4.2 keychain 集成（API key 不再以明文落 user.json）
- P4.3 macOS Universal Binary（arm64 + x64 合并）

---

## [0.2.25] – 2026-06-27

Public demo 数据隔离热修复：修正已安装 / 新安装客户端仍可能通过共享 public
backend 当前会议状态继承其它设备会议的问题，并补充跨平台 packaged CDP smoke 工具。

### 修复

- Public / TV / Android 默认连接 `https://echodesk.yoliyoli.uk` 时，不再轮询共享
  `/meetings/current` 作为本机会议状态；状态条只显示本机显式开始的会议。
- `/capture/chunk` 回包里的 `meeting_id` 只有在前端本次 chunk 明确带出同一个
  本机 `meeting_id` 时才会进入会议面板；待机状态下 public backend 的全局自动会议
  不会再污染新装客户端。
- WebSocket 在 public 默认 backend 下继续丢弃共享 replay，但允许本机已知
  `meeting_id` 的事件进入，避免本机会议的产物 / 待办事件被误杀。
- Public backend 服务端同步隔离待机采集：待机 `/capture/chunk` 不再沿用共享
  `MeetingState.current`，也不会把无 `meeting_id` 的 chunk 写入其它设备当前会议。
- TTS 客户端超时从 30s 放宽到 90s，避免 heyi 到 eight 的 tailnet 转发偶发慢响应时
  误报 “TTS 上游熔断”。
- Fast LLM public 默认改为跟随 Yunwu `MiniMax-M2.7` 主通道；当 eight fast LLM
  未启动时，路由 / RAG 仲裁不再因为 `:7905` 连接失败而整体降级。

### 工具 / 验证

- 新增 `desktop/scripts/cdp-packaged-smoke.cjs`，可对 macOS / Windows / Linux
  打包应用通过 Electron CDP 做不白屏、连接态、设置入口、工作区入口、输入框和
  基础布局边界检查，并保存截图。
- Windows NSIS 安装器改为 one-click/current-user 模式，禁用安装目录选择和
  安装后自动启动，避免远程静默安装或普通用户一键安装卡在交互安装界面。
- 新增 `EchoDesk-0.2.25-win-x64.zip` 便携包，便于远程/托管环境绕过 NSIS
  安装器直接做 smoke 或临时使用。
- E2E 新增回归：public / TV 待机时即使 `/capture/chunk` 返回共享 `meeting_id`，
  也不会显示共享会议段落或切换会议状态。

### 配置变更

- 桌面版本升到 `0.2.25`。
- backend 默认 `app_version=0.2.25`。
- Android / TV `versionCode=225`、`versionName=0.2.25`。
- STT / TTS 当前默认走 eight，Fast LLM 默认走 Yunwu 兜底：
  - STT: `http://100.76.3.59:8090`
  - TTS: `http://100.76.3.59:8094`
  - Fast LLM: `https://yunwu.ai/v1`, model `MiniMax-M2.7`

---

## [0.2.24] – 2026-06-27

Public demo 多端热修复：修正新装客户端继承旧历史、移动端/TV 版布局挤压，以及安装文档仍指向旧版本的问题。
后端版本号同步到 `0.2.24`，避免 public demo 客户端误判公网 backend 仍落后，并便于确认
STT / TTS / 扫码保存修复已部署到服务端。
Fast LLM 模型名同步到 eight 当前实际 served model `qwen3.5-9b-local-gpu0`，避免
纪要恢复、RAG 仲裁和短问答继续请求旧模型名导致 404。
当 public backend 的 MAIN/FAST 都临时指向 eight 9B 时，显式 FAST 请求优先使用 fast
token budget；线上 `LLM_MAIN_MAX_TOKENS` / `MINUTES_MAX_TOKENS` 同步降到 4096，
避免超过 eight 当前 8192 上下文窗口。

### 修复

- Public / TV 首次启动的数据边界升级到 schema 3，并把
  `echodesk.localCaptureState.v1` 纳入一次性清理范围；新装 public 包不再从旧 WebView
  或共享 demo backend 继承其它设备历史，已完成迁移的设备升级后继续保留本机历史。
- Android 手机 / 平板窄屏下压缩顶栏、工作区栏和底部输入区，保证设置、发送、附件按钮
  不越界，输入框字号和按钮高度保持一致。
- TV / 低分辨率横屏下把主内容固定为「转写流 + 右侧产物栏」两列，右侧栏按 26vw 收缩，
  避免 960×540 电视 WebView 上转写区被挤窄或错位。
- README、安装指南、TV 安装页统一指向 `v0.2.24` release 资产。

### 配置变更

- 桌面版本升到 `0.2.24`。
- Android / TV `versionCode=224`、`versionName=0.2.24`。

### 验证

- `cd desktop && npm run typecheck`
- `cd desktop && npm run lint`
- `cd desktop && npm run e2e`
- Android emulator：安装 APK、授权麦克风、进入主界面并验证原生 `AudioRecord`
  持续产出音频 chunk。

---

## [0.2.20] – 2026-06-26

Public demo / TV 录音同步热修复：解决 TV 端 capture/chunk 有结果但会议转写不显示、
public 模式本机历史刷新后消失的问题。

### 修复

- Public / TV 模式下不再依赖共享 WebSocket 业务事件来显示本机会议转写；
  `/capture/chunk` 回包带 `meeting_id` / `meeting_segments` 时，前端会直接创建/选中
  本机会议并合并转写段。
- Public / TV 模式新增本机 localStorage 快照 `echodesk.localCaptureState.v1`，保存本机
  ambient、会议段、会议列表和 outputs，避免刷新或重启后历史空掉。
- 数据边界迁移改为一次性执行；已完成迁移的设备升级后不再每次启动删除本机历史键。
- Public / TV 模式收到共享 backend 的 `server_resync` 时不再清空本机 store，避免本机
  会议列表被共享 WS 重同步误擦。
- 手动开始/结束会议时立即更新本机 store，保证屏蔽共享 WS 的 public/TV 客户端也能
  看到当前会议状态。

### 配置变更

- 桌面版本升到 `0.2.20`。
- Android / TV `versionCode=220`、`versionName=0.2.20`。
- backend 默认 `app_version=0.2.20`。

### 验证

- `cd desktop && npm run typecheck -- --noEmit`
- `cd desktop && npm run lint -- --quiet`
- `cd desktop && npm run build`
- `cd desktop && npm run e2e`

---

## [0.2.19] – 2026-06-26

Public demo 发布补丁：修正客户端状态误报、安装包校验和 Android / TV APK 的对外分发形态。

### 修复

- Electron public demo 健康检查现在按 backend URL 协议选择 `http` / `https`，不再把
  `https://echodesk.yoliyoli.uk` 误判为 backend unhealthy。
- About 弹窗底部文案改为「Public demo · 客户端不内置模型密钥」，避免把公网 demo
  误描述成“数据不出机”。
- Android instrumentation 占位测试不再硬编码 `com.echodesk.app`，避免 TV 包名
  `com.echodesk.tv` 跑测试时失败。
- Android / TV 打包脚本改为生成非 debuggable 的 release variant APK，并在本机用 demo
  signing key 签名；正式客户分发仍建议换私有 release keystore。
- Release 校验文件改为 flat asset 文件名，用户在下载目录直接执行
  `shasum -a 256 -c SHA256SUMS-0.2.19.txt` 即可校验。

### 配置变更

- 桌面版本升到 `0.2.19`。
- Android / TV `versionCode=219`、`versionName=0.2.19`。
- backend 默认 `app_version=0.2.19`。

---

## [0.2.18] – 2026-06-26

Public demo 数据边界与 UI 一致性修复：解决新装/升级后误继承共享历史、远程 backend
版本落后不可见、工作区配置难找、桌面输入栏和顶栏对齐不稳的问题。

### 修复

- Public demo / Android / TV 启动前执行本地 storage 数据边界迁移：默认清理旧
  `echodesk.mobileBackendBase`、会议选择、ambient 缓存等历史状态，避免新装设备
  看起来继承别人的会议；用户在设置里显式保存过自定义 backend 时会保留该地址。
- 自定义 backend（内网/私有演示）不再被 public demo 历史隔离逻辑误挡，仍可加载
  该私有 backend 自己的会议历史。
- backend 版本低于客户端版本时，顶部 backend 状态弹窗和设置页更新区都会显示黄色
  警告，避免“客户端已最新但 public backend 仍旧版”的隐性不一致。
- 工作区配置入口直达设置抽屉的「工作区目录」区块，并自动聚焦「添加目录」按钮；
  知识库弹窗不再把 `.env` 当成主路径，引导用户直接添加目录。
- 桌面 1280px 宽度不再过早套用大屏三栏尺寸，转写区保持足够宽度；输入栏 placeholder
  缩短并固定按钮盒模型，避免对话栏换行撑高。
- 顶栏 backend/eight/云/麦克风 pill、会议状态、TTS、设置按钮统一 32px 控件高度，
  减少视觉拼装感。

### 配置变更

- 桌面版本升到 `0.2.18`。
- Android / TV `versionCode=218`、`versionName=0.2.18`。
- backend 默认 `app_version=0.2.18`。
- `desktop/package.json` 的 `private` 改为 `false`，避免 public demo 仓库被误读成私有包。

### 验证

- `cd desktop && npm run typecheck -- --noEmit`
- `cd desktop && npm run lint -- --quiet`
- `cd desktop && npx playwright test tests/e2e/public-demo-settings.spec.ts tests/e2e/acceptance-clickthrough.spec.ts tests/e2e/meeting-status-bar.spec.ts tests/e2e/workspace-knowledge.spec.ts tests/e2e/tv-layout.spec.ts`

---

## [0.2.17] – 2026-06-26

安装与数据隔离热修复：把本机 macOS 打不开 / 开错旧版、TV 与 Android 包共享数据的问题收口。

### 修复

- macOS release 打包阶段增加 ad-hoc codesign hook，打包后立即 `codesign --verify --deep --strict`，避免安装到 `/Applications` 后出现签名损坏导致无法打开。
- TV APK 改用独立 Android 包名 `com.echodesk.tv`；Android 手机 / 平板包继续使用 `com.echodesk.app`，同一台 Android 设备上不再互相覆盖或共享 WebView 数据。
- TV 一键安装脚本默认清理 `com.echodesk.tv` 并卸载旧 TV 遗留包 `com.echodesk.app`；需要保留旧包时可显式设置 `ECHODESK_TV_KEEP_LEGACY=1`。
- TV 安装脚本改用 launcher 启动方式，不再假设 activity 是 `$PKG/.MainActivity`，兼容 `applicationId` 与 Java package 分离。
- README / INSTALL / TV_INSTALL 明确 public demo 和私有本地后端的数据边界，避免误解为公网 demo 也是 100% 本地数据。

### 配置变更

- 桌面版本升到 `0.2.17`。
- Android / TV `versionCode=217`、`versionName=0.2.17`。
- backend 默认 `app_version=0.2.17`。

---

## [0.2.16] – 2026-06-25

TV 真机修复补丁：把智能电视端从“桌面 UI 缩放版”收敛成会议室可读布局，并补齐录音真实性诊断。

### 修复

- TV 端最终布局改为固定两栏会议室模式：隐藏会议历史侧栏，转写区优先，outputs 右栏固定，顶部服务状态、工作区行、转写头、输入栏和按钮统一基线。
- TV 端字体和点击目标重新调到 960×540 CSS viewport 的远距可读尺寸：命令栏、转写气泡、speaker tag、会议纪要 / outputs 标题不再各自漂移。
- Android 原生录音每个 chunk 记录 `source/sampleRate/bytes/rms/peak` 到 logcat，现场能直接区分“真的录到了声音”和“电视系统给了静音输入”。
- TV e2e 增加更严格的视觉断言：header/workspace/outputs/transcript/input 高宽和字号必须满足电视布局边界。

### 配置变更

- 桌面版本升到 `0.2.16`。
- Android / TV `versionCode=216`、`versionName=0.2.16`。
- backend 默认 `app_version=0.2.16`。

### 已知限制

- MiTV_ASTP0 现场设备当前没有向第三方 app 暴露有效麦克风输入；EchoDesk 会提示接入 USB / 蓝牙会议麦克风。STT 是否正确必须基于有声 chunk 或 public backend 上传测试判断，不能把电视静音输入误判为 STT 失败。

---

## [0.2.15] – 2026-06-25

TV 现场可用性补丁：继续修正智能电视端前端比例、录音源兼容和错误提示。

### 修复

- Android 原生录音从单一 `16k + VOICE_RECOGNITION/MIC/CAMCORDER` 扩展为
  `DEFAULT/MIC/VOICE_RECOGNITION/VOICE_COMMUNICATION/CAMCORDER` ×
  `16k/48k/44.1k` 回退；实际选中的 source/sampleRate 会写入 log 并随 WAV
  头传给后端。
- 电视端读到全零输入时会停止上传静音块并继续定时重试，插入 USB / 蓝牙会议
  麦克风后无需重启应用。
- TV 视觉再次收口：顶部状态不再挤成一排大灰块，工作区行、转写头、输入栏、
  outputs 面板固定基线和宽度；960×540 CSS viewport 下继续禁止横向溢出。
- TV e2e 的布局断言同步更新，卡住 header/workspace/outputs/transcript/input 的
  尺寸边界。

### 配置变更

- 桌面版本升到 `0.2.15`。
- Android / TV `versionCode=215`、`versionName=0.2.15`。
- backend 默认 `app_version=0.2.15`。

### 已知限制

- MiTV_ASTP0 现场仍可能由系统 HAL 拒绝第三方 app 麦克风输入；此时 EchoDesk
  会明确提示接入外置会议麦克风。TTS 播放命令已验证可走系统输出。

---

## [0.2.14] – 2026-06-25

TV / STT 可用性补丁：修复前端 TV 视口挤压、录音上传格式错位和麦克风错误
文案误导。

### 修复

- 后端 capture / meeting / STT / diarizer 入口统一兼容前端上传的 WAV 容器与旧
  raw PCM；WAV 会先解成 16k mono PCM，再进入 RMS/VAD/STT/声纹链路，避免
  把 `RIFF` 头当成音频样本或把 WAV 再包一层 WAV。
- ambient 落盘的 `.wav` 永远写成有效 WAV 文件，便于后续回放和排查录音质量。
- TV 布局改成稳定两栏：隐藏左侧会议历史，转写区优先，outputs 固定宽度，
  输入栏和转写气泡统一字体大小，避免 960×540 CSS viewport 下互相挤压。
- 麦克风不可用提示区分可自动重试和需要外接麦克风的错误；电视没有有效输入时
  不再显示误导性的“5s 后重试”。

### 验证

- `cd desktop && npx tsc --noEmit`
- `cd desktop && npm run lint -- --quiet`
- `cd desktop && npx playwright test tests/e2e/tv-layout.spec.ts`
- `cd backend && .venv/bin/python -m pytest tests/unit/test_audio_wav.py tests/unit/test_stt_adapter.py tests/unit/test_capture_stats_api.py tests/unit/test_meeting_pipeline.py tests/unit/test_ambient_capture.py`
- 960×540 TV 视口截图：无横向溢出，转写区 640px，outputs 320px，会议侧栏 0px。
- public backend `/capture/chunk` 真实上传合成语音返回 `stt_status="ok"`，主体文本正确识别。

### 已知限制

- 本机当前无法直连 eight `100.76.3.59:8090`，FireRed 直连集成测试会 skip；
  public backend 可访问 eight STT，公开安装包默认走 public backend。
- FireRed 对品牌词 `EchoDesk` 会误听成英文近音，后续需要热词或后处理。

### 配置变更

- 桌面版本升到 `0.2.14`。
- Android / TV `versionCode=214`、`versionName=0.2.14`。
- backend 默认 `app_version=0.2.14`。

---

## [0.2.13] – 2026-06-25

TV 会议可用性 hotfix：修复智能电视端状态误报、录音兼容和公网 backend 纪要
生成失败的关键路径。

### 修复

- Android / TV 无 Electron supervisor 时，`/healthz/full` 成功会显示
  `backend 外部`，不再误报 `backend 未知`。
- TV / public demo 的 backend 与 TTS 健康检查 timeout 加长，避免旧 Android
  WebView + Cloudflare 慢响应时把可用服务误判成未知。
- Android / TV 录音优先走原生 `AudioRecord` 插件，不再依赖旧 WebView 的
  `getUserMedia`；连续检测到全零 / 极低输入时停止上传静音块，并提示接入
  USB / 蓝牙会议麦克风。
- TV / public demo 模式不再启动期请求共享 `/meetings` 历史，避免新装电视继承
  公网 demo backend 上其它设备的会议记录。
- 会议纪要生成新增 `minutes_max_tokens=12000`，不再硬编码 `max_tokens=80000`；
  public backend 即使把主模型配置到 eight 的 `qwen3.5-9b-local`，也不会触发
  `max_tokens > max_model_len=16384` 的 400 错误。
- E2E mock 补齐 `/meetings/current` 与 `/tts/diag`，TV 模拟点击测试不再泄漏
  Vite proxy 错误。

### 配置变更

- 桌面版本升到 `0.2.13`。
- Android / TV `versionCode=213`、`versionName=0.2.13`。
- backend 默认 `app_version=0.2.13`。

### 验证

- `npm run typecheck`
- `npm run lint -- --quiet`
- `cd backend && .venv/bin/python -m pytest tests/unit/test_meeting_pipeline.py tests/unit/test_llm_adapter.py`
- `npx playwright test tests/e2e/tv-layout.spec.ts tests/e2e/tv-share.spec.ts`

### 已知限制

- `10.10.12.25` 这台 MiTV 的 logcat 仍显示系统音频 HAL 无法打开输入设备；
  EchoDesk 可以诊断并提示，但若电视系统本身没有可用麦克风输入，仍需要接入
  遥控器麦克风、USB/蓝牙会议麦克风，或由桌面/手机端负责采音。
- 公网 `https://echodesk.yoliyoli.uk` backend 当前仍跑旧部署；代码已修复，
  但需要拿到正确 public backend 部署入口后热更新服务端。

---

## [0.2.12] – 2026-06-25

TV / 会议室显示 hotfix：基于 `MiTV-ASTP0`（Android 9，IP `10.10.12.25`）
真机 ADB 安装、截图和遥控器模拟测试修复。

### 修复

- TV 底部对话栏改用短 placeholder，避免 960x540 CSS viewport 下文字被裁切。
- TV 设置抽屉的按钮组和更新区域增加 wrap / 间距，避免「检查更新 / 下载最新版本 / Release」
  挤在一起。
- TV 遥控器焦点不再落到纯展示的采集状态 tag 上，方向键确认更容易进入设置等可操作区域。
- Android TV 麦克风采集新增连续零输入诊断：当 WebView 有音频回调但 peak 长时间接近 0 时，
  显示“电视麦克风没有有效输入”，避免把电视底层音频 HAL 故障误报成 STT 熔断。
- 继续保留 TV / Android public demo 隔离策略：新装默认清本地缓存，不读取共享历史。

### 配置变更

- 桌面版本升到 `0.2.12`。
- Android / TV `versionCode=212`、`versionName=0.2.12`。

### 验证

- `npm run typecheck`
- `npm run lint`
- `npm run build`
- `npx playwright test tests/e2e/tv-layout.spec.ts tests/e2e/tv-share.spec.ts`
- `npm run app:dist:mac`
- `npm run app:dist:win`
- `npm run app:dist:linux`
- `npm run app:dist:android`
- `npm run app:package:tv`
- ADB 真机安装 `EchoDesk-0.2.12-smart-tv.apk` 到 `10.10.12.25`，截图确认主界面、
  输入栏、设置抽屉和麦克风诊断状态。

### 已知限制

- 该电视 logcat 仍持续报 `audio_hw_primary: cannot open pcm_in driver`，说明问题在电视系统
  音频输入设备/驱动层。EchoDesk 已能显式诊断并提示，但如果要直接远场采音，仍需要电视系统
  能识别可用麦克风或接入外部会议麦克风。

---

## [0.2.11] – 2026-06-25

更新机制与分发 hotfix：补齐用户主动检查更新入口，并为后续桌面端自动覆盖安装
生成所需的 GitHub Release updater 元数据。

### 新增

- 设置页新增「更新」区域：显示当前版本、GitHub Release 最新版本、匹配当前平台的
  安装资产，并提供「检查更新 / 下载最新版本 / Release」入口。
- macOS / Windows / Linux 桌面端接入 `electron-updater`：
  - 打包版可从 GitHub Release 检查新版本；
  - 发现新版本后可下载并安装；
  - 更新安装保留用户本机数据目录，不清 `~/.echodesk`。
- Android / TV 端复用同一个检查入口：不能静默安装 APK 时打开对应 APK 或 TV 一键包下载页。
- electron-builder 增加 GitHub `publish` 配置，后续 release 会带 `latest*.yml`
  元数据供桌面自动更新使用。

### 修复

- README / INSTALL 下载表同步列出 macOS、Windows、Linux、Android、TV 和一键包，
  避免用户只看到 DMG。
- TV 一键安装文案区分“首次安装清缓存”和“升级保留数据”，减少误以为所有升级都会清库。

### 配置变更

- 桌面版本升到 `0.2.11`。
- Android / TV `versionCode=211`、`versionName=0.2.11`。

### 已知限制

- 已安装的 `0.2.10` 客户端本身没有 updater 代码，不能自己弹出 0.2.11；
  需要用户手动安装一次 0.2.11。之后的桌面版本可走应用内更新。
- Android / TV 侧载 APK 受系统限制，不能无提示静默替换；会进入系统安装确认流程。

---

## [0.2.10] – 2026-06-24

跨平台 public demo hotfix：macOS / Windows / Linux 桌面公开安装包默认连接公网
EchoDesk backend，不再要求新用户本机安装 Python backend；补齐 Linux 发行包与跨平台
packaged-app 点击验证入口。

### 新增

- Linux x64 发行包：`EchoDesk-0.2.10.AppImage` 与 `echodesk-desktop_0.2.10_amd64.deb`。
- `npm run app:dist:linux`，与现有 macOS / Windows / Android / TV 打包脚本并列。
- 打包后 Electron 真 App E2E 支持 `ECHODESK_APP_BIN`，同一套 smoke 可以在 macOS、
  Windows 和 Linux 上验证启动、public backend、设置、知识库入口和输入框点击路径。

### 修复

- 公开桌面包默认进入 public demo 模式，`getBackendHost()` 返回
  `https://echodesk.yoliyoli.uk`，模型 key 与 STT/TTS/LLM 调用保留在服务端。
- public demo 桌面包不再启动本机 Python backend；私有部署可显式设置
  `ECHO_FORCE_LOCAL_BACKEND=1` 恢复原来的本地 backend。
- public demo 桌面包与 Android/TV 一样隐藏共享历史 hydrate 与共享 WS 业务事件，
  避免新安装用户看到其它设备的会议历史。
- public backend 短暂异常时 Electron 主进程不会误判为本地外部 backend 退出并尝试
  接管启动 Python。

### 验证

- macOS 本机 typecheck / lint / build / browser e2e 通过。
- Windows 在 `win-sunny-friend` 上做真实安装包与 packaged-app smoke。
- Linux 在 `heyi-daheng` 上做 AppImage/deb 构建与 packaged-app smoke。

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
  v0.2.8 旧包 logcat 报 `Unexpected token ?` 并白屏；v0.2.9 新包不再报语法错误，主界面正常显示，
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
