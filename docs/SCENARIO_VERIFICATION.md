# EchoDesk · 场景验证报告（v0.2.0）

> 本报告记录 v0.2.0 发版前的端到端场景巡检结果。
> 9 个场景全部由 Playwright 自动模拟点击 + 同步录像，覆盖 Phase 1 / Phase 2 / Phase 3 所有用户面能力。
> 验证日期：2026-05-27 · 通过率：**9 / 9 ✅**

---

## 跑通方法

```bash
cd desktop
npm run scenarios            # 跑全套场景并录像（≈30s）
bash scripts/collect-scenario-videos.sh   # 整理成 webm + mp4
```

视频产物：
- 原始 webm：`desktop/test-results/scenarios/<test-name>/video.webm`
- 收集 mp4 + webm（重命名干净）：`desktop/test-results/scenario-videos/sNN-*.{webm,mp4}`
- HTML 报告 + trace：`desktop/test-results/scenarios-html/index.html` → `npx playwright show-report test-results/scenarios-html`

---

## 场景清单

| # | 场景 | 覆盖功能 | 视频文件 | 步骤数 | 结果 |
|---|---|---|---|---:|:---:|
| S01 | 首次启动引导 → About 对话框 | P3.1 / P3.5 / P3.3 | `s01-首次启动引导+关于对话框.mp4` | 7 | ✅ |
| S02 | 顶栏 4 个诊断 pill 巡检 | P2.1 | `s02a-诊断pill巡检-全绿态.mp4` | 5 | ✅ |
| S02b | mic denied → 系统设置深链 | P3.5 | `s02b-麦克风denied深链.mp4` | 3 | ✅ |
| S03 | 设置面板：远端配置 + 重启 + 回放引导 | P2.5 / P3.2 / P3.1 | `s03-设置面板远端配置+回放引导.mp4` | 7 | ✅ |
| S04 | @生成 HTML 命令链路 | Phase 1 intent / artifact | `s04-生成HTML命令链路.mp4` | 4 | ✅ |
| S05a | @生成 后端 500 错误处理 | P2.2 | `s05a-生成失败错误处理.mp4` | 4 | ✅ |
| S05b | WebSocket 断线 → 自动重连 | Phase 1 WS | `s05b-WebSocket断线重连.mp4` | 3 | ✅ |
| S06a | heyi-bj 全挂 → heyi pill 红色 | P2.3 / P2.1 | `s06a-heyi降级红pill.mp4` | 2 | ✅ |
| S06b | Yunwu / Tavily 缺 key → 橙色 pill | P2.3 / P2.1 | `s06b-Yunwu缺key橙pill.mp4` | 2 | ✅ |

---

## 详细 assert 表

### S01 · 首次启动引导 → About 对话框（P3.1 + P3.5 + P3.3）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 打开 EchoDesk | OnboardingModal 自动弹出，显示「欢迎来到 EchoDesk」 | ✅ |
| 2 | 点 Next | 进入「授权麦克风」步骤，显示 `onboarding-mic-state` pill | ✅ |
| 3 | 点 Next | 进入「准备就绪」完成页 | ✅ |
| 4 | 点「完成」 | Modal 关闭，主界面 status-bar / `open-about` 可见 | ✅ |
| 5 | 点顶栏 v0.2.0 徽章 | AboutModal 弹出，前端版本 `v0.2.0` + 后端 `0.2.0` 同时显示 | ✅ |
| 6 | 验证链接 | CHANGELOG 链接 href 以 `CHANGELOG.md` 结尾、INSTALL 链接 href 以 `docs/INSTALL.md` 结尾 | ✅ |
| 7 | 关 About → reload 页面 | 引导不再弹（持久化生效），主界面直接显示 | ✅ |

### S02 · 顶栏 4 个诊断 pill 巡检（P2.1）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 主界面渲染 | `pill-backend` / `pill-heyi` / `pill-yunwu` / `pill-mic` 4 个都可见 | ✅ |
| 2 | 点 backend pill | popover 显示 `version` / `0.2.0` / `8769`（来自 `/healthz/full`） | ✅ |
| 3 | 点 heyi-bj pill | popover 显示 STT FireRed / TTS Qwen3 / Fast LLM 3 行探针 | ✅ |
| 4 | 点云 pill | popover 显示 Yunwu MiniMax + Tavily 状态 | ✅ |
| 5 | 点 mic pill | popover 显示「权限状态 granted」绿色 | ✅ |

### S02b · mic denied 深链（P3.5）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 主界面（mic perm = denied） | `pill-mic` 内圆点为红色 `bg-err` | ✅ |
| 2 | 点 mic pill | popover 显示「打开系统设置」按钮（`mic-open-system-prefs`） | ✅ |
| 3 | 点该按钮 | Electron IPC `openMicSystemPrefs` 被调用（验证 `window.__openPrefsCalled__` flag） | ✅ |

### S03 · 设置面板：远端配置 + 重启 + 回放引导（P2.5 + P3.2 + P3.1）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 点齿轮 → Drawer 打开 | `remote-settings-form` 可见 | ✅ |
| 2 | 数据目录 section | 显示 `/Users/test/.echodesk` | ✅ |
| 3 | 远端服务表单 | `llm_main_base_url=https://yunwu.ai/v1`、`stt_firered_url=http://100.87.251.9:8090` 预填，`user.json` 标签出现在 `yunwu_open_key` 行 | ✅ |
| 4 | 改 `llm_main_base_url` → 保存 | toast「已写入 1 项」出现 | ✅ |
| 5 | `restart-backend-after-config` 按钮浮现 | ✅ |
| 6 | 点重启 → Electron IPC `manualRestartBackend` 被调用 + 「已发送重启请求」toast | ✅ |
| 7 | 点「回放引导」 | OnboardingModal 重新显示 | ✅ |

### S04 · @生成 HTML 命令链路

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 主界面 → WS 已连接 | 顶栏「已连接」绿点 | ✅ |
| 2 | 在 `command-textarea` 输入 `@生成 HTML 测试报告` | textarea 显示完整文本 | ✅ |
| 3 | 按 Enter | `/artifacts/generate` POST 被调用（mock fetchLog 验证） | ✅ |
| 4 | WS 推 `artifact.ready` | ArtifactPanel 卡片出现「HTML 报告」/ artifact_id | ✅ |
| 5 | textarea 清空，可继续下条命令 | ✅ |

### S05a · @生成 后端 500 错误处理（P2.2）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 输入 `@生成 HTML 测试报告` 提交 | `/artifacts/generate` 返回 500 | ✅ |
| 2 | 错误 toast 弹出 | `.ant-message-error / .ant-notification-notice-error` 可见 | ✅ |
| 3 | textarea 未被禁用 | 可继续输入「@查 今天天气」 | ✅ |

### S05b · WebSocket 断线 → 自动重连

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | 初始状态 | 顶栏「已连接」 | ✅ |
| 2 | mock.closeWs(1006) | 顶栏切到「断线」 | ✅ |
| 3 | mock.reopenWs() | 10s 内自动恢复「已连接」 | ✅ |

### S06a · heyi-bj 全挂（P2.3）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | `/healthz/full` mock `heyi-down` | `pill-heyi` 内圆点红色 | ✅ |
| 2 | 点 heyi pill | popover 显示「Connection refused」错误 | ✅ |

### S06b · Yunwu / Tavily 缺 key（P2.3）

| 步骤 | 操作 | 期望 | 实际 |
|---|---|---|---|
| 1 | `/healthz/full` mock `yunwu-no-key`（`ok: null, reason: "no_api_key"`） | `pill-yunwu` 内圆点橙色 `bg-amber-500` | ✅ |
| 2 | 点云 pill | popover 显示「部分密钥未配置」+ 提示编辑 `config.json` | ✅ |

---

## 局限性

这套场景跑在 **纯浏览器 Playwright + mock 后端** 上，不覆盖：

- 真实 Electron 主进程 IPC 行为（BackendSupervisor 真启 / 真停 / 真重启）；通过 `window.echo.*` mock 间接验证调用契约
- 真实麦克风采集（`getUserMedia` 在 `_helpers.ts` 里被静音 oscillator 替代，仅为防 toast 干扰）
- 真实 STT / TTS / LLM HTTP 调用（已有 `tests/e2e-real/` 真服务测试覆盖，跑前需先启 backend）
- 真实音频转写流（`TranscriptStream` 在场景中始终显示「等待环境音转写」空状态）

跑真实服务版本：
```bash
cd backend && uvicorn app.main:app --port 8769   # 终端 1
cd desktop && VITE_API_TARGET=http://localhost:8769 npm run dev -- --port 5173  # 终端 2
npm run demo:record    # 跑 tests/e2e-real/demo-recording.spec.ts
```

---

## 文件清单

```
desktop/
├── playwright.scenarios.config.ts          # 录像 + slowMo 配置
├── tests/scenarios/
│   ├── _helpers.ts                         # mock window.echo / permissions / healthz
│   ├── s01_first_run_and_about.spec.ts
│   ├── s02_status_pills.spec.ts            # S02 + S02b
│   ├── s03_settings_remote_config.spec.ts
│   ├── s04_meeting_and_artifact.spec.ts
│   ├── s05_sad_paths_and_reconnect.spec.ts # S05a + S05b
│   └── s06_degraded_state.spec.ts          # S06a + S06b
└── scripts/collect-scenario-videos.sh      # webm → mp4 + 重命名

docs/SCENARIO_VERIFICATION.md               # 本文档
```

下一次新功能上线时，新增一个 `sNN_*.spec.ts` 即可自动加入巡检集。
