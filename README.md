# EchoDesk · 数字分身 (Another Me)

> **目标**：会议 + 办公场景的"数字分身"应用。public demo 的桌面 / Android / TV 客户端默认连接 EchoDesk 公网 backend，模型密钥不进入客户端包；私有本地部署仍可显式启用本机 backend。
> **当前版本**：v0.2.10（详见 [`CHANGELOG.md`](CHANGELOG.md)）
> **立即下载**：见 [GitHub Releases v0.2.10](https://github.com/yoligehude14753/echo-demo/releases/tag/v0.2.10)
> **安装指南**：见 [`docs/INSTALL.md`](docs/INSTALL.md)
> **DEMO 复跑**：见 [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)
> **远程后端 eight endpoint（STT/TTS/Fast LLM）**：见 [`docs/REMOTE_API.md`](docs/REMOTE_API.md)

## 立即下载

当前 public demo 版本是 `v0.2.10`，安装包统一放在：
<https://github.com/yoligehude14753/echo-demo/releases/tag/v0.2.10>

| 平台 | Release 资产 | 说明 |
|---|---|---|
| macOS Apple Silicon | `EchoDesk-0.2.10-arm64.dmg` | 桌面版安装包 |
| macOS 备用 zip | `EchoDesk-0.2.10-arm64-mac.zip` | dmg 打不开时使用 |
| Windows | `EchoDesk.Setup.0.2.10.exe` | Windows 安装包 |
| Linux AppImage | `EchoDesk-0.2.10.AppImage` | Linux x64 免安装运行 |
| Linux deb | `echodesk-desktop_0.2.10_amd64.deb` | Ubuntu / Debian 安装包 |
| Android 手机 / 平板 | `EchoDesk-0.2.10-android.apk` | 默认连接公网 demo backend |
| Android TV / 智能电视 | `EchoDesk-0.2.10-smart-tv.apk` | 适配遥控器、电视桌面入口和开机自启 |
| 智能电视一键安装 | `EchoDesk-0.2.10-smart-tv-oneclick.zip` | 内含 macOS / Windows ADB 安装脚本 |
| 校验文件 | `SHA256SUMS-0.2.10.txt` | 校验下载完整性 |

公开桌面包、Android 和 TV 客户端默认连接 `https://echodesk.yoliyoli.uk`，模型服务和密钥都在服务端。
私有桌面部署可设置 `ECHO_FORCE_LOCAL_BACKEND=1` 恢复本机 Python backend。
更详细的安装、电视侧载和本地后端说明见 [`docs/INSTALL.md`](docs/INSTALL.md)。

## 状态摘要 (2026-06-24)

| 阶段 | 范围 | 状态 |
|---|---|---|
| Phase 1 (0.1.0) | 持续监听 + 会议 + 9 类 intent + 一键产物 + 一键 install | ✅ released |
| Phase 2 (0.2.0) | 状态可视化 + artifact.failed + 远端降级 + DB migration + 管理 API + 诊断打包 | ✅ released |
| Phase 3 (0.2.10) | 首次启动引导 + 远端 endpoint 配置 + 知识库面板 + 智能电视一键安装/自启 + 会后扫码保存 + public demo backend | ✅ demo hotfix |
| Phase 4 | Keychain 集成 + 自动更新 + Universal Binary | 计划中 |

测试：本地 backend WS unit 通过；desktop typecheck/lint/build 通过；TV / 分享 / 工作区 / 设置相关 e2e 模拟点击通过；macOS / Windows / Linux / Android / TV release 产物已构建。

## 架构

```
echodesk/
├── README.md
├── ARCHITECTURE.md          # 自上而下架构图 + 模块边界
├── docs/
│   ├── PRD_v6.7.1.md       # 从 echo 仓库引入的最终 PRD
│   └── adr/                # 关键决策记录
├── backend/                # FastAPI server (Echo desktop 后端)
│   ├── app/
│   │   ├── llm/            # yoli_llm 中台 → Yunwu M2.7 主通道 + qwen3.5-9b-local fast
│   │   ├── stt/            # eight FireRedASR2-AED 客户端
│   │   ├── tts/            # eight TTS 客户端
│   │   ├── diarization/    # SpeechBrain ECAPA-TDNN（本地 CPU）默认参数
│   │   ├── rag/            # jieba + BM25 多文档+会议 RAG
│   │   ├── web_search/     # Inspiro 主 + Tavily 备 + 仲裁器
│   │   ├── exporters/      # Anthropic Skill v6.7.1 (PPT/Word/Excel/HTML)
│   │   ├── intent/         # 9 类 intent 路由
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
│   │   └── runtime/        # IPC + backend client
│   └── tests/
├── shared/                 # 前后端共享 types / proto
├── scripts/                # dev / e2e / demo 录屏
└── experiments_baseline/   # 从 echo 仓库迁过来的 v6.7.1 验证产物（只读参考）
```

## 已验证的技术决策（不再重测）

| 决策 | 出处 | 实测数据 |
|---|---|---|
| LLM 主通道：**Yunwu M2.7** + qwen3.5-9b-local fast | PRD §四.A.2.6 | 12.6min 会议端到端 147s |
| RAG：**jieba + BM25Okapi**（不上 mem0/LightRAG）| PRD §A.3 P1-1 | doc_cite 100% / 9 query 并发 1.28s |
| Web Search 仲裁：Inspiro 主 + Tavily 备 + DDG 兜底 | PRD §A.2 + Tavily 验证 | winner_ok 7/8 / fab_ok 6/8 |
| 一键 PPT：**pptxgenjs + Midnight 色板** | PRD §A.2.11 v6.7.1 | 417 视觉 shapes / notes 772 字/页 |
| 一键 Word：**python-docx + SKILL.md prompt** | PRD §A.2.11 v6.7.1 | 真 TOC + List style + 上标引用 |
| 一键 Excel：**openpyxl + Source 列**（去 cell.comment） | PRD §A.2.11 v6.7.1 | 4 sheet 含 DCF / 126 公式 / 46 跨 sheet / 0 errors |
| 一键 HTML：**single-file + Tailwind CDN** | PRD §A.2.11 v6.7.1 | 66K 字符 / 144 卡片块 / SVG 可视化 |
| 声纹识别：**SpeechBrain ECAPA-TDNN** 默认参数 | PRD §A.7 P2 | 本地 CPU 推理 |
| STT：**FireRedASR2-AED @ eight** | PRD §STT | 12.6min 端到端验证 |
| TTS：**eight qwen3-tts** | PRD §TTS | — |

## 开发节奏（2 周）

详见 `docs/DEV_PLAN.md`（自上而下，按架构分层）。

## License

Proprietary
