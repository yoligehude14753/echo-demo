# EchoDesk · 数字分身工作台

EchoDesk 是面向会议与办公场景的本地优先桌面应用：持续采集和会议转写进入知识库，用户可以基于会议与资料检索、生成纪要和办公产物，并把长任务交给 Agent 执行。当前源码版本为 **v0.3.2**。

v0.3.2 新增 L0-L3 分层 memory、ASR 文本关联与信息流来源卡，快速任务实际使用 Yunwu `gpt-5.4-nano`（界面显示 `qwen3 8b`），Echo AI 主回答继续使用 Yunwu `deepseek-v4-flash`；同时加入 app/backend build 握手、数据库 schema fail-closed、会议自动结束/cooldown 与 FireRed 复读/幻觉过滤。

- 桌面端默认启动安装包内自带的本机 backend，业务数据写入本机 SQLite。
- public demo 只有在发布入口显式设置 `ECHO_PUBLIC_DEMO=1` 时启用；Android / TV 作为受限客户端连接配置的服务端。
- v0.3.2 已作为 [GitHub prerelease](https://github.com/yoligehude14753/echo-demo/releases/tag/v0.3.2) 发布；Linux 与 Android / TV 为正式候选资产，Windows 明确标记为 `UNSIGNED-TEST`，macOS 为 ad-hoc、未 notarize 的测试资产。
- 公开下载只以 GitHub Release 实际列出的资产为准。CI artifact、本机构建产物和源码版本都不自动等同于已公开发布；在 macOS / Windows 取得正式签名链前，不提升为 stable/latest。

## macOS v0.3.3-preview.2 安装

[v0.3.3-preview.2](https://github.com/yoligehude14753/echo-demo/releases/tag/v0.3.3-preview.2)
当前提供 Apple 芯片 Preview bootstrap ZIP。该版本仅使用本机 ad-hoc 签名，未使用
Developer ID，未完成 Apple notarization 或 staple，因此不是 Apple 已公证发行版。

首次安装请打开“终端”，完整复制并执行下面整段命令。它不会执行或依赖 ZIP 内的
`.command`，不会使用 `curl | sh`，也不会关闭全局 Gatekeeper；写入 `/Applications`
时最多请求一次管理员密码。命令会先校验固定 SHA-256，只清除 staged bundle 的
quarantine 属性，并且仅替换 `/Applications/EchoDesk Preview.app`。如果安装失败，
会恢复该目标原有版本，不会触碰其他 App。

```bash
/bin/bash <<'ECHODESK_INSTALL'
set -euo pipefail

URL='https://github.com/yoligehude14753/echo-demo/releases/download/v0.3.3-preview.2/EchoDesk-0.3.3-preview.2-macOS-arm64.zip'
EXPECTED_SHA256='ce769da22915470b8b94f1b44fd7886e84d28faa9f3eaace0cc9bea6291974e3'
TARGET='/Applications/EchoDesk Preview.app'
WORK="$(mktemp -d "${TMPDIR:-/tmp}/echodesk-preview-install.XXXXXX")"
ZIP="$WORK/EchoDesk-0.3.3-preview.2-macOS-arm64.zip"
UNPACK="$WORK/unpacked"
STAGED="$WORK/EchoDesk Preview.app"
trap '/bin/rm -rf -- "$WORK"' EXIT

/bin/mkdir -p "$UNPACK"
/usr/bin/curl --fail --location --proto '=https' --tlsv1.2 --output "$ZIP" "$URL"
ACTUAL_SHA256="$(/usr/bin/shasum -a 256 "$ZIP" | /usr/bin/awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "SHA-256 校验失败：expected=$EXPECTED_SHA256 actual=$ACTUAL_SHA256" >&2
  exit 1
fi

/usr/bin/ditto -x -k -- "$ZIP" "$UNPACK"
SOURCE="$(/usr/bin/find "$UNPACK" -type d -path '*/Payload/EchoDesk Preview.app' -prune -print -quit)"
if [[ -z "$SOURCE" || ! -d "$SOURCE" ]]; then
  echo 'ZIP 中未找到 Payload/EchoDesk Preview.app' >&2
  exit 1
fi

/usr/bin/ditto -- "$SOURCE" "$STAGED"
/usr/bin/xattr -r -d com.apple.quarantine "$STAGED" 2>/dev/null || true
if /usr/bin/xattr -r "$STAGED" 2>/dev/null | /usr/bin/grep -Fq com.apple.quarantine; then
  echo '无法清除 staged bundle 的 quarantine 属性' >&2
  exit 1
fi
/usr/bin/codesign --force --deep --sign - "$STAGED"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$STAGED"

/usr/bin/sudo /bin/bash -s -- "$STAGED" "$TARGET" <<'ROOT_INSTALL'
set -euo pipefail
STAGED="$1"
TARGET="$2"
[[ "$TARGET" == '/Applications/EchoDesk Preview.app' ]] || exit 1
[[ ! -L "$TARGET" ]] || { echo '拒绝替换符号链接目标' >&2; exit 1; }

TX="$(/usr/bin/mktemp -d '/Applications/.echodesk-preview-install.XXXXXX')"
NEW="$TX/EchoDesk Preview.app"
BACKUP="$TX/previous.app"
previous_moved=0
new_installed=0
rollback() {
  status=$?
  trap - EXIT HUP INT TERM
  if [[ "$status" -ne 0 ]]; then
    if [[ "$new_installed" -eq 1 && -e "$TARGET" ]]; then
      /bin/rm -rf -- "$TARGET"
    fi
    if [[ "$previous_moved" -eq 1 && -e "$BACKUP" ]]; then
      /bin/mv -- "$BACKUP" "$TARGET" || true
    fi
  fi
  /bin/rm -rf -- "$TX"
  exit "$status"
}
trap rollback EXIT HUP INT TERM

/usr/bin/ditto -- "$STAGED" "$NEW"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$NEW"
if [[ -e "$TARGET" ]]; then
  /bin/mv -- "$TARGET" "$BACKUP"
  previous_moved=1
fi
/bin/mv -- "$NEW" "$TARGET"
new_installed=1
/usr/bin/codesign --verify --deep --strict --verbose=2 "$TARGET"

if [[ "$previous_moved" -eq 1 ]]; then
  /bin/rm -rf -- "$BACKUP"
  previous_moved=0
fi
new_installed=0
ROOT_INSTALL

/usr/bin/codesign --verify --deep --strict --verbose=2 "$TARGET"
/usr/bin/open "$TARGET"
echo "EchoDesk Preview 已安装并启动：$TARGET"
ECHODESK_INSTALL
```

首次启动时按系统提示授予麦克风权限。应用打开后麦克风仍保持待机，直到用户点击开始
会议并确认收音设备。不要通过全盘清除 quarantine、关闭 Gatekeeper 或复用旧包绕过
校验；只有命令显示 SHA-256 与上文完全一致并且 strict verify 通过时，才继续启动。

现有旧版内置的 `electron-updater` 不会自动安装本 Preview。该 prerelease 不包含
`latest-mac.yml`、blockmap 或 DMG，旧版用户必须从上述 GitHub Release 使用 Terminal
命令手动下载并覆盖安装；本版本不提供或声称应用内一键更新。

## 北极星指标

> **已授权 Workflow 有效闭环率**：进入终态的非用户主动取消 workflow 中，最终为 `succeeded` 的比例；按 tenant / owner 分域统计，不跨主体聚合原始内容。
>
> 当前生产基线：尚未建立。v0.3.2 exact-SHA CI、安装态和发布资产结果是发布门禁证据，不代替生产基线。
>
> v0.3.1 发布后 30 天目标：`>= 95%`；测量频率：weekly。完整口径与护栏见 [`METRICS.md`](METRICS.md)。

## 当前状态

v0.3.2 当前源码、受控安装态与公开 prerelease 证据：

- Backend：`1045 collected`，其中 `18` 条 live contract 明确分流；确定性门禁 `1027 selected / 1027 passed / 0 skipped / 0 failed / 0 errors`，line coverage `87.46%`（终端显示 `87%`），pytest 进程自然退出。Ruff check、Ruff format `250 files`、mypy `128 source files` 与 compile 均通过。
- Desktop：Electron main-process contracts `177 / 177 passed`；Playwright E2E `150 passed`；业务 scenarios `29 passed`。
- Public isolation：self-test 与双 principal 完整 smoke 均通过；release aggregate `31 / 31 passed`，actionlint 与 action pin 检查通过。
- Android / TV：current exact-SHA phone 与 TV build、JVM `4 / 4`、instrumentation `6 / 6`、APK identity `0.3.1 (301)` 与 unsigned fail-closed 全部通过；聚合 lint 为 `Fatal 0 / Error 0 / Warning 0`，另有 Capacitor `Hint 2`。产物是 debug APK，不可作为公开发布资产。
- 依赖审计：desktop npm 与内置 `ppt_ib_deck` npm 均为 `0` finding。Python six locks 均有效；runtime/dev/build 三份锁各仍报告同一项上游无 `fix_versions` 的 `torch` `CVE-2025-3000`，按文档化例外控制至 2026-08-12；lint/typecheck/audit-tool 锁为 `0` finding，不能把 Python 总体结果写成 clean 或零漏洞。
- 版本：v0.3.2 源码中的 Desktop、Backend、Android、package-lock、Commitizen 和版本契约已统一；发布 tag 与 `main` 精确指向同一 SHA。

current exact-SHA macOS arm64 门禁已通过：fresh ad-hoc DMG、ZIP、metadata、blockmap、codesign、plist、asar、forbidden-file scan、`1066` 组件 SBOM 与 SHA-256 校验通过，read-only mounted DMG smoke `1 / 1 passed`；安装态完整 workflow `1 / 1 passed`，覆盖真实下载 `0600`、marker、安全文件名、无残留 partial，以及 GLM/RAG、失败注入、重启、retry、AgentOS success/cancel/timeout/restart；live contract `2 / 2 passed`，`0 skipped / 0 failed`。Developer ID、notary、staple 与 Gatekeeper 正式链路因缺外部签名输入而 skipped，不能由 ad-hoc 结果替代。

截至 2026-07-14，GitHub Release `v0.3.2` 已公开为 prerelease，共含 macOS、Windows、Linux、Android / TV 的 22 个安装、校验、SBOM 与签名/升级证据资产。macOS 缺 Developer ID/notary，Windows 缺受信任代码签名证书，因此两者仍是测试分发，不能标为正式可信安装包。

## 产品主线

```text
会议输入 -> 知识沉淀 -> 任务执行 -> 产物生成 -> 分享归档 -> 诊断恢复
```

核心能力：

- 会议采集、转写、说话人识别、纪要生成和失败重试；纪要生成由当前 Workflow run 持有，取消、超时或失败与会议可见终态在同一事务收口，显式 retry 才能由新 run 接管。
- 工作区资料与会议内容统一进入 owner-scoped RAG；meeting 投影用 generation + index/delete fence 抵御迟到写，ambient 片段用稳定 operation id 和持久退避队列完成重启修复。
- Artifact、Todo、Meeting finalize 与 Agent task 进入统一 Workflow Kernel。retry 的 child、parent lineage event、outbox 与 domain marker 在一个 Unit of Work 内提交，并与同 scope 的 fresh create 通过 `active_key` 仲裁唯一活动 run。
- public backend 使用服务端签发的 tenant / user / device / session principal，隔离会议、RAG、Artifact、Workflow、Agent 与 WebSocket；匿名 session credential body 使用独立的全局/peer admission pool，慢请求不能占满普通已认证业务通道。
- Electron 桌面端采用 Session Navigation、Workbench、Inspector 三层信息架构；public workspace 传输把 HTTPS backend origin、credential vault、session 和 origin-scoped registry 绑定为同一边界，切换后取消旧操作且不复用旧 bearer/文档登记。

## 运行模式

| 模式 | 启用方式 | Backend | 数据边界 |
|---|---|---|---|
| Desktop Pro | 默认 | 安装包内本机 backend | 本机 `~/.echodesk/` |
| Public demo | `ECHO_PUBLIC_DEMO=1` | 配置的 HTTPS 服务 | 服务端 principal scope |
| 强制本机兼容 | `ECHO_FORCE_LOCAL_BACKEND=1` | 本机 backend | 覆盖 public 开关 |
| Android / TV | 移动端安装包 | 配置的 HTTPS 服务 | 设备身份 + 服务端 scope |

本机模式是受信任的单机能力边界：用户授权后，Agent 可以执行宿主机任务，Electron IPC 可以访问明确暴露的本机能力。public 普通 principal 不获得 host-admin 能力。

## 快速开始

要求：Python 3.11、Node.js 24。

```bash
# Backend
python3.11 -m venv backend/.venv
backend/.venv/bin/pip install --require-hashes -r backend/requirements-dev.lock
backend/.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8769 --ws-max-size 4096
```

另开终端：

```bash
cd desktop
npm ci
npm run dev
```

构建自包含桌面安装包：

```bash
cd desktop
npm run app:dist:mac       # macOS arm64
npm run app:dist:win       # Windows x64 runner
npm run app:dist:linux     # Linux x64 runner
```

安装包会携带对应平台的 backend binary；正常用户不需要先运行旧的 `install-backend.sh`。该脚本只保留给源码部署与旧安装迁移。

## 质量门

```bash
# 供应链与版本
node scripts/check-npm-lock-registries.cjs
python3 scripts/check-ci-action-pins.py
python3 scripts/check-python-locks.py
node desktop/scripts/check-version-sync.cjs

# Backend
backend/.venv/bin/ruff check backend
backend/.venv/bin/ruff format --check backend
backend/.venv/bin/mypy backend/app
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
export ECHO_USER_DIR="$TEST_ROOT"
export DB_PATH="$TEST_ROOT/echodesk.db"
export STORAGE_DIR="$TEST_ROOT/storage"
export RAG_INDEX_DIR="$TEST_ROOT/rag_index"
(cd backend && .venv/bin/pytest tests -m "not live")

# Desktop
cd desktop
npm run test:electron
npm run lint
npm run typecheck
npm run build
CI=1 NODE_ENV=test npm run e2e
CI=1 NODE_ENV=test npm run scenarios
```

live contract 与安装态 workflow 是独立门禁，不能用 `/healthz`、mock E2E 或 packaged smoke 代替。

## 文档

- [`PRD.md`](PRD.md)：产品目标、用户流程、范围与验收。
- [`METRICS.md`](METRICS.md)：北极星、输入指标和护栏指标。
- [`ARCHITECTURE.md`](ARCHITECTURE.md)：当前实现架构、事务边界与已知 P2。
- [`docs/0.3/README.md`](docs/0.3/README.md)：0.3 文档索引与当前/最终证据边界。
- [`docs/INSTALL.md`](docs/INSTALL.md)：安装、运行模式、签名与排障。
- [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)：可复跑演示流程。
- [`CHANGELOG.md`](CHANGELOG.md)：用户可见变更。

## 技术栈

- Desktop：Electron、React 18、TypeScript、Vite、Ant Design 5、Playwright。
- Backend：FastAPI、Pydantic、SQLite、aiosqlite、Ports & Adapters。
- Retrieval：jieba、BM25、持久化 scope 文档与协调索引。
- Packaging：PyInstaller、electron-builder、Capacitor / Android Gradle。

## License

Proprietary
