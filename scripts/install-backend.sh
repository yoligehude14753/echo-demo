#!/usr/bin/env bash
# EchoDesk · 一键 backend 安装脚本（P1.7 独立产品 Phase 1）
#
# 目标：在用户 mac 上准备好 EchoDesk.app 运行所需的所有 Python 资源：
#   ~/.echodesk/source/backend/         backend 源代码副本（不再绑 dev 仓库）
#   ~/.echodesk/source/backend/.venv/   独立 venv（Electron resolvePython 第一候选）
#   ~/.echodesk/config.json             默认用户配置（已存在则保留）
#   ~/.echodesk/logs/                   backend log 落盘目录（backend 启动自建）
#
# 用法：
#   bash scripts/install-backend.sh                 # 自动从当前仓库探源
#   bash scripts/install-backend.sh <repo_path>     # 显式指定仓库路径
#   bash scripts/install-backend.sh --uninstall     # 卸载（删 ~/.echodesk 整个目录前会要求确认）
#   bash scripts/install-backend.sh --reset-config  # 仅重置 config.json 为默认（保留 db / log）
#
# 退出码：
#   0 = 安装成功
#   1 = 依赖缺失（Python 3.11 等）
#   2 = 参数 / 路径错误
#   3 = pip install 失败
#   4 = smoke test 失败
#
# 兼容：mac arm64 + intel；Python 3.11 / 3.12（3.13 未测）

set -euo pipefail

# ---------- 工具 ----------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # reset

err()  { printf "${RED}ERROR:${NC} %s\n" "$*" >&2; }
warn() { printf "${YELLOW}WARN :${NC} %s\n" "$*" >&2; }
info() { printf "${BLUE}-->${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}OK  :${NC} %s\n" "$*"; }

trap 'err "脚本异常退出（line $LINENO）"; exit 99' ERR

# ---------- 路径常量 ----------

ECHODESK_HOME="${ECHODESK_HOME:-$HOME/.echodesk}"
SOURCE_DIR="$ECHODESK_HOME/source"
BACKEND_DIR="$SOURCE_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
USER_CONFIG="$ECHODESK_HOME/config.json"
LOG_DIR="$ECHODESK_HOME/logs"

# ---------- 子命令分发 ----------

if [ "${1:-}" = "--uninstall" ]; then
    info "卸载 EchoDesk backend 数据"
    if [ ! -d "$ECHODESK_HOME" ]; then
        ok "无需卸载（$ECHODESK_HOME 不存在）"
        exit 0
    fi
    warn "这会删除 $ECHODESK_HOME 下所有数据，包括："
    warn "  - 会议数据库 echodesk.db"
    warn "  - 录音文件 storage/"
    warn "  - 工作区索引 rag_index/"
    warn "  - 用户配置 config.json"
    warn "  - backend log 与 venv"
    echo
    read -p "确认彻底删除? (yes/NO) " ans
    if [ "$ans" != "yes" ]; then
        info "已取消"
        exit 0
    fi
    rm -rf "$ECHODESK_HOME"
    ok "已删除 $ECHODESK_HOME"
    info "提示：/Applications/EchoDesk.app 需要手动拖到废纸篓"
    exit 0
fi

# ---------- 步骤 1：解析仓库路径 ----------

step1_resolve_repo() {
    info "step 1: 解析仓库源路径"
    local repo
    if [ "${1:-}" != "" ] && [ "${1:-}" != "--reset-config" ]; then
        repo="$1"
    else
        # 脚本自身位置 → 仓库根（scripts/install-backend.sh）
        local script_dir
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        repo="$(cd "$script_dir/.." && pwd)"
    fi
    if [ ! -d "$repo/backend" ] || [ ! -f "$repo/backend/requirements.txt" ]; then
        err "$repo 不像是 echo-demo 仓库（缺 backend/requirements.txt）"
        err "用法：bash $0 [<repo_path>]"
        exit 2
    fi
    REPO="$repo"
    ok "仓库源: $REPO"
}

# ---------- 步骤 2：检查 Python 3.11+ ----------

step2_check_python() {
    info "step 2: 检查 Python 3.11+"
    # 候选顺序：环境变量 → 常见 brew / 系统位置 → PATH
    local cands=("${ECHO_INSTALL_PYTHON:-}")
    cands+=("/opt/homebrew/bin/python3.11")
    cands+=("/opt/homebrew/bin/python3.12")
    cands+=("/usr/local/bin/python3.11")
    cands+=("/usr/local/bin/python3.12")
    cands+=("python3.11" "python3.12" "python3")

    local chosen=""
    local v
    for py in "${cands[@]}"; do
        [ -z "$py" ] && continue
        if command -v "$py" >/dev/null 2>&1; then
            v="$("$py" --version 2>&1 || true)"
            # 接受 Python 3.11.x 或 3.12.x
            if [[ "$v" =~ Python\ 3\.(11|12)\. ]]; then
                chosen="$py"
                break
            fi
        fi
    done
    if [ -z "$chosen" ]; then
        err "没找到 Python 3.11 或 3.12"
        err "推荐装法（任选一）："
        err "  brew install python@3.11"
        err "  pyenv install 3.11 && pyenv global 3.11"
        err "  从 python.org 下 3.11 installer"
        err "已知路径都试过：${cands[*]}"
        exit 1
    fi
    PYTHON="$chosen"
    ok "Python: $PYTHON ($v)"
}

# ---------- 步骤 3：准备用户目录 ----------

step3_prepare_dirs() {
    info "step 3: 准备 ~/.echodesk/ 目录"
    mkdir -p "$ECHODESK_HOME" "$SOURCE_DIR" "$LOG_DIR"
    # 注意：不创建 storage / rag_index / skill_build，这些由 backend lifespan 按需建
    ok "目录就绪: $ECHODESK_HOME"
}

# ---------- 步骤 4：同步 backend 源码 ----------

step4_sync_backend() {
    info "step 4: 同步 backend 源码到 $BACKEND_DIR"
    # 用 rsync 排除大文件 / 缓存。--delete 保证退新版时旧文件被清掉，
    # 但留 .venv（不在 source 里）和 logs 等不在 backend 目录的东西
    rsync -a --delete \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        --exclude='*.egg-info' \
        --exclude='.coverage' \
        --exclude='htmlcov' \
        "$REPO/backend/" "$BACKEND_DIR/"
    ok "backend 源码同步完成"
}

# ---------- 步骤 5：创建 / 复用 venv ----------

step5_create_venv() {
    info "step 5: 创建 venv $VENV_DIR"
    if [ -x "$VENV_PY" ]; then
        local existing_ver
        existing_ver="$("$VENV_PY" --version 2>&1 || true)"
        info "venv 已存在: $existing_ver"
        # 如果 Python 版本变了，强制重建
        local want_minor
        want_minor="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        if [[ "$existing_ver" != *"Python $want_minor"* ]]; then
            warn "现有 venv ($existing_ver) 跟当前 Python ($want_minor) 不匹配，重建"
            rm -rf "$VENV_DIR"
        fi
    fi
    if [ ! -x "$VENV_PY" ]; then
        "$PYTHON" -m venv "$VENV_DIR"
        ok "venv 已创建"
    else
        ok "venv 复用现有"
    fi
}

# ---------- 步骤 6：装依赖 ----------

step6_install_deps() {
    info "step 6: pip install -r requirements.txt（首次约 3-10 min，含 torch）"
    "$VENV_PY" -m pip install --upgrade pip --quiet
    if ! "$VENV_PY" -m pip install -r "$BACKEND_DIR/requirements.txt" --quiet 2>&1 | tail -20; then
        err "pip install 失败，看上面的输出"
        exit 3
    fi
    ok "依赖装好"
}

# ---------- 步骤 6.5：装 ppt_ib_deck 的 node deps（phase4-doc-skills） ----------

# 投行风 PPT skill 依赖 docxtemplater + pizzip；走 backend/app/adapters/skill/assets/ppt_ib_deck
# 里的本地 package.json。没装 node 直接跳过（用户用 @生成 ppt 时会 fallback 到 legacy 或报错），
# 不让整个 install 卡住。详见 prompts.py / llm_skill.py 的 phase4-doc-skills 注释。

step6_5_install_ppt_deck_deps() {
    local deck_dir="$BACKEND_DIR/app/adapters/skill/assets/ppt_ib_deck"
    info "step 6.5: 装 ppt_ib_deck node deps（投行风 PPT skill 依赖）"
    if [ ! -d "$deck_dir" ]; then
        warn "ppt_ib_deck 资产目录不存在（$deck_dir），跳过"
        return
    fi
    if [ ! -f "$deck_dir/package.json" ]; then
        warn "ppt_ib_deck/package.json 缺失，跳过"
        return
    fi
    if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
        warn "node / npm 不在 PATH，跳过 ppt_ib_deck deps 安装"
        warn "→ @生成 ppt 时会自动 fallback 到 legacy pptxgenjs（USE_LEGACY_HTML_PPT=true 等效路径）"
        warn "  装 node 后重跑此脚本：bash $0"
        return
    fi
    if [ -d "$deck_dir/node_modules" ] && [ -d "$deck_dir/node_modules/docxtemplater" ]; then
        ok "ppt_ib_deck/node_modules 已装好，跳过 npm install"
        return
    fi
    info "在 $deck_dir 跑 npm install（首次约 10-20s）..."
    if ! (cd "$deck_dir" && npm install --silent --no-audit --no-fund 2>&1 | tail -5); then
        warn "npm install 失败 → @生成 ppt 会 fallback 到 legacy；不阻塞主流程"
        return
    fi
    ok "ppt_ib_deck node deps 装好"
}

# ---------- 步骤 7：写默认 config.json ----------

# 默认 config 内容；不要把 yunwu / tavily key 内置（避免泄露）
DEFAULT_CONFIG=$(cat <<'JSON'
{
  "port": 8769,
  "log_level": "INFO",
  "stt_backend": "firered",
  "stt_firered_url": "http://100.76.3.59:8090",
  "stt_language": "zh",
  "tts_enabled": true,
  "tts_provider": "qwen3_tts",
  "tts_qwen3_url": "http://100.76.3.59:8094",
  "llm_main_provider": "yunwu",
  "llm_main_model": "MiniMax-M2.7",
  "llm_main_base_url": "https://yunwu.ai/v1",
  "llm_fast_provider": "yunwu",
  "llm_fast_model": "MiniMax-M2.7",
  "llm_fast_base_url": "https://yunwu.ai/v1",
  "yunwu_open_key": "",
  "tavily_api_key": "",
  "diarizer_enabled": true
}
JSON
)

step7_write_config() {
    info "step 7: 写 $USER_CONFIG"
    if [ "${RESET_CONFIG:-0}" = "1" ]; then
        warn "--reset-config 触发：覆盖现有 config.json"
        echo "$DEFAULT_CONFIG" > "$USER_CONFIG"
        ok "config.json 已重置为默认"
        return
    fi
    if [ -f "$USER_CONFIG" ]; then
        ok "config.json 已存在，保留（用 --reset-config 重置）"
        return
    fi
    echo "$DEFAULT_CONFIG" > "$USER_CONFIG"
    ok "config.json 已写入默认值"
    warn "未填 YUNWU_OPEN_KEY → @生成 / 纪要功能不可用；编辑 $USER_CONFIG 填入即可"
    warn "未填 TAVILY_API_KEY → @查 联网检索不可用"
}

# ---------- 步骤 8：smoke test ----------

step8_smoke() {
    info "step 8: smoke test（验证 import + 1 次启停）"
    cd "$BACKEND_DIR"
    # 把 ECHODESK_HOME（脚本接口名，更直观）映射给 backend 看到的
    # ECHO_USER_DIR（config_io.py:user_config_dir 用这个），让 smoke step 启的
    # backend 用临时目录而不是污染真实 ~/.echodesk/
    export ECHO_USER_DIR="$ECHODESK_HOME"
    if ! "$VENV_PY" -c "from app.config import get_settings; from app.main import create_app; print('import ok')" 2>&1; then
        err "import 失败"
        exit 4
    fi
    ok "import 干净"

    # 启 backend 几秒，curl healthz，然后 kill
    info "试启 backend on :8769..."
    local port=8769
    if lsof -ti "tcp:$port" -sTCP:LISTEN >/dev/null 2>&1; then
        warn "端口 $port 已被占用，跳过 smoke（你的 EchoDesk 可能已在跑）"
        return
    fi
    "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" --log-level warning > /tmp/echodesk-install-smoke.log 2>&1 &
    local spid=$!
    local i=0
    while [ $i -lt 30 ]; do
        sleep 0.5
        if curl -fsS --max-time 1 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
            ok "healthz 200"
            break
        fi
        i=$((i+1))
    done
    if ! curl -fsS --max-time 1 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
        err "smoke 启动 15s 后 healthz 仍不通；最后 20 行日志："
        tail -20 /tmp/echodesk-install-smoke.log >&2
        kill "$spid" 2>/dev/null || true
        exit 4
    fi
    kill "$spid" 2>/dev/null || true
    # 等子进程退干净避免 stderr 噪音
    wait "$spid" 2>/dev/null || true
    ok "smoke 通过"
}

# ---------- 主流程 ----------

# 解析参数
RESET_CONFIG=0
ARG_REPO=""
for arg in "$@"; do
    case "$arg" in
        --reset-config) RESET_CONFIG=1 ;;
        --uninstall)    : ;;   # 已在脚本顶部处理
        --*)            err "未知选项: $arg"; exit 2 ;;
        *)              ARG_REPO="$arg" ;;
    esac
done

printf "\n${BLUE}╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${BLUE}║         EchoDesk · install-backend.sh (P1.7)             ║${NC}\n"
printf "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}\n\n"

step1_resolve_repo "$ARG_REPO"
step2_check_python
step3_prepare_dirs
step4_sync_backend
step5_create_venv
step6_install_deps
step6_5_install_ppt_deck_deps
step7_write_config
step8_smoke

printf "\n${GREEN}╔══════════════════════════════════════════════════════════╗${NC}\n"
printf "${GREEN}║                  安装完成                                  ║${NC}\n"
printf "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}\n\n"
cat <<MSG
EchoDesk backend 就位：

  venv:    $VENV_PY
  config:  $USER_CONFIG
  data:    $ECHODESK_HOME
  logs:    $LOG_DIR

下一步：
  1. 把 EchoDesk.app 拖到 /Applications/
  2. 双击 EchoDesk.app 即可使用
     （Electron BackendSupervisor 会自动 spawn backend）

要填的密钥（可选，不填对应功能灰）：
  - YUNWU_OPEN_KEY → @生成 / 会议纪要
  - TAVILY_API_KEY → @查 联网检索
  编辑 $USER_CONFIG 填入对应字段即可（重启 .app 生效）

故障排查：
  - 查 log：tail -F $LOG_DIR/backend.log
  - 完全重置 config：bash $0 --reset-config
  - 卸载：bash $0 --uninstall
  - 详细文档：$REPO/docs/INSTALL.md
MSG
