# EchoDesk · 数字分身 (Another Me)

> **目标**：会议 + 办公场景的"数字分身"应用。0.3 Desktop Pro 默认使用本机 backend 与 SQLite；public demo 作为显式部署模式保留。
> **当前源码版本**：v0.3.0（详见 [`CHANGELOG.md`](CHANGELOG.md)）
> **公开下载**：见 [GitHub Releases](https://github.com/yoligehude14753/echo-demo/releases/latest)；本轮已本机构建 v0.3.0 macOS 资产，发布后可下载。
> **安装指南**：见 [`docs/INSTALL.md`](docs/INSTALL.md)
> **DEMO 复跑**：见 [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)
> **模型配置**：Desktop Pro 从本机 `~/.echodesk/config.json` 读取；public demo 的模型密钥留在服务端，不进入客户端包。
> **主线治理基准**：见 [`docs/GOVERNANCE_v0.2.50.md`](docs/GOVERNANCE_v0.2.50.md)。0.3.0 以 `v0.2.50` 为历史治理基线推进 Workflow Core，不继承旧 `0.2.7` 实验线。
> **0.3 工作流核心**：见 [`docs/0.3/README.md`](docs/0.3/README.md)。0.3 以统一 Workflow Core 收束会议、Artifact、Todo、Claude Code Agent、分享导出和诊断恢复。

## 立即下载

当前源码版本是 `v0.3.0`。公开安装包下载页：
<https://github.com/yoligehude14753/echo-demo/releases/latest>

| 平台 | Release 资产命名 | 说明 |
|---|---|---|
| macOS Apple Silicon | `EchoDesk-0.3.0-arm64.dmg` | 桌面版安装包 |
| macOS 备用 zip | `EchoDesk-0.3.0-arm64-mac.zip` | dmg 打不开时使用 |
| Windows 安装器 | `EchoDesk.Setup.0.3.0.exe` | 需 Windows 构建 workflow 产出后才可下载 |
| Windows 便携包 | `EchoDesk-0.3.0-win-x64.zip` | 需 Windows 构建 workflow 产出后才可下载 |
| Linux AppImage | `EchoDesk-0.3.0.AppImage` | 需 Linux 构建产出后才可下载 |
| Linux deb | `echodesk-desktop_0.3.0_amd64.deb` | 需 Linux 构建产出后才可下载 |
| Android 手机 / 平板 | `EchoDesk-0.3.0-android.apk` | 需 Android 构建产出后才可下载 |
| Android TV / 智能电视 | `EchoDesk-0.3.0-smart-tv.apk` | 需 TV 打包产出后才可下载 |
| 智能电视一键安装 | `EchoDesk-0.3.0-smart-tv-oneclick.zip` | 需 TV 打包产出后才可下载 |
| 校验文件 | `SHA256SUMS-0.3.0.txt` | 随已发布资产一起上传 |

Desktop Pro 桌面包默认启动本机 backend；安装前先运行 `scripts/install-backend.sh`。Android、TV
及显式 public demo 客户端连接 `https://echodesk.yoliyoli.uk`，模型服务和密钥留在服务端。
Windows 机器若出现 Device Guard / 组织策略拦截 `.exe` 安装器，请下载 `EchoDesk-0.3.0-win-x64.zip`，
解压后直接运行 `EchoDesk.exe`；该形态已在 Windows 远程机通过启动和设置页点击 smoke。
public demo 桌面启动入口需显式设置 `ECHO_PUBLIC_DEMO=1`；旧部署仍可用
`ECHO_FORCE_LOCAL_BACKEND=1` 强制覆盖为本机模式。
更详细的安装、电视侧载和本机服务说明见 [`docs/INSTALL.md`](docs/INSTALL.md)。
TV / 公共演示模式默认不拉取共享历史，新装设备只显示本机本次会议；若电视系统不向
三方 app 提供有效麦克风输入，EchoDesk 会提示接入 USB / 蓝牙会议麦克风。
TV APK 使用独立包名 `com.echodesk.tv`，不会再和 Android 手机 / 平板包 `com.echodesk.app`
覆盖或共享本地 WebView 数据。

## 状态摘要 (2026-07-10)

| 阶段 | 范围 | 状态 |
|---|---|---|
| Phase 1 (0.1.0) | 持续监听 + 会议 + 多类指令 + 一键产物 + 一键 install | ✅ released |
| Phase 2 (0.2.0) | 状态可视化 + artifact.failed + 模型服务降级 + DB migration + 管理 API + 诊断打包 | ✅ released |
| Phase 3 (0.2.35) | 首次启动引导 + 服务端模型配置 + 知识库面板 + 智能电视一键安装/自启 + 会后扫码保存 + 公共演示服务 + 检查更新 | ✅ demo hotfix |
| EchoDesk 0.3 alpha | Workflow Core + Artifact/Todo/Claude Agent 闭环 + local-first Desktop Pro | ✅ 本机验收完成 |
| Phase 4 | Keychain 集成 + Universal Binary | 计划中 |

测试：backend 确定性门禁 532 passed，0.3 workflow/contract 专项 19 passed，desktop scenarios 25 passed，更新设置/顶栏 e2e 10 passed；已安装 macOS `.app` + 已安装 backend 使用真实 SQLite、Claude Code bridge 和 AgentOS 跑通失败/重启/重试/取消/超时/产物恢复 E2E。Windows / Linux / Android / TV 全量 release 资产仍需单独跑对应发布 workflow。

## 架构

```
echodesk/
├── README.md
├── ARCHITECTURE.md          # 自上而下架构图 + 模块边界
├── docs/
│   ├── PRD_v6.7.1.md       # 从 echo 仓库引入的最终 PRD
│   └── adr/                # 关键决策记录
├── backend/                # FastAPI service (EchoDesk 服务端)
│   ├── app/
│   │   ├── llm/            # OpenAI-compatible 主模型 + fast fallback
│   │   ├── stt/            # 语音识别客户端
│   │   ├── tts/            # 语音合成客户端
│   │   ├── diarization/    # SpeechBrain ECAPA-TDNN（本地 CPU）默认参数
│   │   ├── rag/            # jieba + BM25 多文档+会议知识库检索
│   │   ├── web_search/     # Inspiro 主 + Tavily 备 + 仲裁器
│   │   ├── exporters/      # Anthropic Skill v6.7.1 (PPT/Word/Excel/HTML)
│   │   ├── intent/         # 多类指令路由
│   │   ├── ws/             # WebSocket broadcast (meeting state + notes)
│   │   ├── api/            # FastAPI routes
│   │   └── models/         # SQLAlchemy / pydantic schemas
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   └── e2e/            # 端到端场景测试
│   └── requirements.txt
├── desktop/                # Electron + React 18 + TS + Vite + Ant Design 5
│   ├── src/
│   │   ├── components/     # ChatView, NotesCard, MeetingControls, ...
│   │   ├── features/       # meeting/, document/, artifact/, agent/
│   │   ├── hooks/          # useEchoWS, useMeetingState, ...
│   │   ├── store/          # zustand
│   │   └── runtime/        # IPC + 服务端接口客户端
│   └── tests/
├── shared/                 # 前后端共享 types / proto
├── scripts/                # dev / e2e / demo 录屏
└── experiments_baseline/   # 从 echo 仓库迁过来的 v6.7.1 验证产物（只读参考）
```

## 已验证的技术决策（不再重测）

| 决策 | 出处 | 实测数据 |
|---|---|---|
| LLM 主通道：**OpenAI-compatible 主模型** + public fast fallback | PRD §四.A.2.6 | 12.6min 会议端到端 147s |
| 知识库检索：**jieba + BM25Okapi** | PRD §A.3 P1-1 | doc_cite 100% / 9 query 并发 1.28s |
| 联网检索：Tavily | PRD §A.2 + Tavily 验证 | 有 key 时启用，无 key 时明确提示不可用 |
| 一键 PPT：**pptxgenjs + Midnight 色板** | PRD §A.2.11 v6.7.1 | 417 视觉 shapes / notes 772 字/页 |
| 一键 Word：**python-docx + SKILL.md prompt** | PRD §A.2.11 v6.7.1 | 真 TOC + List style + 上标引用 |
| 一键 Excel：**openpyxl + Source 列**（去 cell.comment） | PRD §A.2.11 v6.7.1 | 4 sheet 含 DCF / 126 公式 / 46 跨 sheet / 0 errors |
| 一键 HTML：**single-file + Tailwind CDN** | PRD §A.2.11 v6.7.1 | 66K 字符 / 144 卡片块 / SVG 可视化 |
| 声纹识别：**SpeechBrain ECAPA-TDNN** 默认参数 | PRD §A.7 P2 | 本地 CPU 推理 |
| STT：**FireRedASR2-AED 服务端识别** | PRD §STT | 12.6min 端到端验证 |
| TTS：**qwen3-tts 服务端合成** | PRD §TTS | — |

## 开发节奏（2 周）

详见 `docs/DEV_PLAN.md`（自上而下，按架构分层）。

## License

Proprietary
