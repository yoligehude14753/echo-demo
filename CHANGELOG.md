# CHANGELOG

EchoDesk 是一个本地优先、面向中文用户的桌面会议与办公助手。
本文档列出每个正式版本里**用户可见的能力变化**与**面向开发者的重要架构调整**。

格式参考 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [SemVer 2.0](https://semver.org/lang/zh-CN/)：在 1.0 之前任何
0.x 升级都可能有破坏性改动，会在条目里显式标注。

> 时间统一用本地时区 (UTC+8)。

---

## [Unreleased]

### Added
- _尚未释出的功能在此累积。_

### Changed
- _尚未释出的修改在此累积。_

### Fixed
- _尚未释出的修复在此累积。_

---

## [0.2.0] · Phase 2 · 自治产品 + 数据自助 — 2026-05-28

聚焦把 EchoDesk 从 demo 升级为可独立运行的桌面产品，并提供给用户
真正的「数据自助」能力。

### Added — Phase 2 主线
- **远程降级链路（P2.3）**：LLM/STT/TTS/Tavily 远端任意不可达时不再让整个
  app 崩溃；artifact 生成失败会发 `artifact.failed` 事件给前端，
  intent 分类器失败回落到 `either` 让 RAG 链路仍可工作。
  详见 `docs/DEGRADATION.md`。
- **DB schema migration 框架（P2.4）**：新增 `app.adapters.repo.migrator`，
  把 `CREATE TABLE` 字面值从 `sqlite.py` 拆到 `migrations/NNN_*.sql`，
  通过 `schema_version` 表幂等推进。
- **顶部状态栏 4 个 pill（P2.1）**：backend / heyi-bj / yunwu / 麦克风权限，
  实时展示远程依赖健康度（30s TCP 探针）+ 点击展开详细 popover；
  backend 异常时给「重启 backend」按钮一键自愈。
- **artifact.failed 卡片（P2.2）**：生成失败的产物会在 `ArtifactPanel`
  显示红色卡片 + 报错原因，不再只是控制台 toast。
- **`/admin` 三件套（P2.5 后端）**：
  - `GET /admin/data-dir` → `~/.echodesk/` 占用 + 5 个子目录 breakdown
  - `POST /admin/meetings/{id}/export` → 一键导出会议 zip（meeting.json + transcript.md + 音频 best-effort）
  - `POST /admin/speakers/reset` → 重置 speakers 表 + diarizer 内存，保留转写文字
- **诊断包导出（P2.6）**：`GET /admin/diagnostics/export` 返回 zip，
  含最近 7 天 backend log（≤5MB/文件，按 size cap）、配置（API key
  已脱敏）、DB schema、远程探针历史。报 bug 时把 zip 发我们就够了。
- **设置面板 Drawer（P2.5/P2.6 前端）**：右上角齿轮按钮打开，
  集中暴露上述四个能力。

### Changed
- `sqlite.py` 不再维护 inline DDL，启动时跑 `run_migrations` 推进 schema。
- 单元测试 + 架构 fitness function 加进 CI，main 强制全绿才能 merge。

### Fixed
- CI/main：51 个 ruff lint（pre-existing 未 format）+ 10 个 mypy strict
  违例 + 1 个 arch fitness（ambient_capture → audio_gate）一并清理。
- e2e `artifact-generate` 改走 `CommandBar @生成` 新流程（旧的 modal
  在 P2.2 已删，导致 e2e 一直挂）。

---

## [0.1.0] · Phase 1 · 独立桌面产品基础 — 2026-05-27

把仓库 demo 拆成可独立装的 mac app + 自管理 backend 进程。

### Added
- **BackendSupervisor（P1.5/P1.6）**：Electron main 进程自动 spawn /
  健康检查 / 自愈重启 / graceful shutdown backend；状态通过 IPC
  推送给 renderer 用于 status pill 显示。
- **`scripts/install-backend.sh`（P1.7）**：mac 一键准备
  `~/.echodesk/source/backend/` + 独立 venv；支持
  `--uninstall` / `--reset-config` 子命令。
- **三层配置（P1.2）**：env > `~/.echodesk/config.json` > 代码默认值，
  所有路径都尊重 `ECHODESK_HOME` 环境变量。
- **`/healthz/full` endpoint（P1.4）**：30s TCP 探针缓存到内存，
  前端 4 个 pill 与诊断包的统一数据源。
- **后端日志按天落盘（P1.3）**：`~/.echodesk/logs/backend.YYYY-MM-DD.log`，
  保留 14 天 rotate。
- **macOS 启动期自动清理 `.DS_Store`（P1.8）**：避免 RAG / storage 误吞。
- **`docs/INSTALL.md`**：装机文档 + 故障排查。

### Architecture
- speaker 引擎从单 chunk 多人混音根因重构成 VAD 句级触发（ECAPA centroid 持久化到 sqlite）。
- ambient 链路加 pre-STT gate（RMS + 语音帧比例）+ 后置幻觉过滤，
  显著降低 dia rizer 在静音下注册脏 speaker。
- STT 砍掉 SenseVoice，仅保留 FireRedASR2-AED（heyi-bj :8090）。

---

## [0.0.x] · Echo Demo 起步 — 2026-05 上半月

EchoDesk 前身：一个验证「环境音 + LLM + 多产物」体感的演示项目。
不再单独发布，详见 `git log --oneline --until=2026-05-25`。

主要里程碑：

- **m1**：LLM adapter（双 LLM 路由）/ STT / TTS / Diarizer 端口 + 适配器
- **m2**：会议 Pipeline UseCase + Skill 执行器（Word/Excel/HTML 三种产物）
- **m3**：WebSocket 事件总线 + 清单式 UI（React + Vite + AntD）
- **m4**：全链路 E2E + Electron 包装 + 白色优雅主题
- **m5**：产物子系统加 PPT + WS 协议契约 1.0（last_seq 续传） + Playwright E2E + 9 类 @ 意图路由器
- **m6**：文件拖拽入 RAG + 麦克风录音入口 + sad-path e2e
- **echodesk-spk-***：speaker 引擎多轮迭代（VAD/centroid/RMS gate）

---

## 链接

- [独立产品 Phase 计划](docs/ECHODESK-PHASES.md)（如有）
- [架构审计](docs/ARCH-AUDIT.md)（如有）
- [降级策略](docs/DEGRADATION.md)
- [安装手册](docs/INSTALL.md)
