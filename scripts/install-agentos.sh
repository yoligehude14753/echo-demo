#!/usr/bin/env bash

set -euo pipefail

LABEL="ai.echodesk.agentos"
ECHODESK_HOME="${ECHODESK_HOME:-$HOME/.echodesk}"
SOURCE_ROOT="$ECHODESK_HOME/source"
BACKEND_ROOT="$SOURCE_ROOT/backend"
BACKEND_PY="$BACKEND_ROOT/.venv/bin/python"
DEST_ROOT="$SOURCE_ROOT/agentos"
AGENTOS_VENV="$DEST_ROOT/.venv"
AGENTOS_PY="$AGENTOS_VENV/bin/python"
BIN_DIR="$ECHODESK_HOME/bin"
RUNNER_PATH="$BIN_DIR/run-agentos.sh"
CONFIG_PATH="$ECHODESK_HOME/config.json"
LOG_DIR="$ECHODESK_HOME/logs"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_PATH" "$RUNNER_PATH"
    rm -rf "$DEST_ROOT"
    if [ -x "$BACKEND_PY" ] && [ -f "$CONFIG_PATH" ]; then
        "$BACKEND_PY" - "$CONFIG_PATH" <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)
config["agent_os_enabled"] = False
directory = os.path.dirname(path)
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    temporary = handle.name
os.replace(temporary, path)
PY
    fi
    echo "AgentOS runtime removed; data preserved at $ECHODESK_HOME/agentos"
    exit 0
fi

if [ ! -x "$BACKEND_PY" ]; then
    echo "EchoDesk backend venv is missing: $BACKEND_PY" >&2
    echo "Run scripts/install-backend.sh first." >&2
    exit 66
fi
if [ ! -f "$CONFIG_PATH" ]; then
    echo "EchoDesk config is missing: $CONFIG_PATH" >&2
    exit 66
fi

AGENTOS_SOURCE="${ECHODESK_AGENTOS_SOURCE:-$(cd "$REPO_ROOT/.." && pwd)/agentos}"
if [ ! -f "$AGENTOS_SOURCE/agentos/server/__main__.py" ] || [ ! -f "$AGENTOS_SOURCE/pyproject.toml" ]; then
    echo "AgentOS source is missing: $AGENTOS_SOURCE" >&2
    echo "Set ECHODESK_AGENTOS_SOURCE to a local AgentOS checkout." >&2
    exit 66
fi

mkdir -p "$DEST_ROOT" "$BIN_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
rsync -a --delete \
    --exclude='.venv' \
    --exclude='.DS_Store' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$AGENTOS_SOURCE/agentos/" "$DEST_ROOT/agentos/"
install -m 0644 "$AGENTOS_SOURCE/pyproject.toml" "$DEST_ROOT/pyproject.toml"
if [ -f "$AGENTOS_SOURCE/README.md" ]; then
    install -m 0644 "$AGENTOS_SOURCE/README.md" "$DEST_ROOT/README.md"
fi
install -m 0755 "$REPO_ROOT/scripts/run-agentos.sh" "$RUNNER_PATH"

if [ ! -x "$AGENTOS_PY" ]; then
    "$BACKEND_PY" -m venv "$AGENTOS_VENV"
fi
"$AGENTOS_PY" -m pip install --quiet --upgrade pip
"$AGENTOS_PY" -m pip install --quiet -e "$DEST_ROOT" python-multipart
"$AGENTOS_PY" -c "import agentos; from agentos.server import create_server_app; print('AgentOS import ok')"

RUNNER_ENABLED="$($BACKEND_PY - "$CONFIG_PATH" <<'PY'
import json
import os
import sys
import tempfile

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)
enabled = bool(str(config.get("yunwu_open_key", "")).strip())
enabled = enabled and config.get("llm_main_provider") == "yunwu"
enabled = enabled and config.get("llm_main_model") == "deepseek-v4-flash"
enabled = enabled and str(config.get("llm_main_base_url", "")).rstrip("/") == "https://yunwu.ai/v1"
config["agent_os_enabled"] = enabled
config["agent_os_url"] = "http://127.0.0.1:4128"
directory = os.path.dirname(path)
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    temporary = handle.name
os.replace(temporary, path)
print("1" if enabled else "0")
PY
)"

"$BACKEND_PY" - "$PLIST_PATH" "$RUNNER_PATH" "$LOG_DIR/agentos.log" <<'PY'
import plistlib
import sys

path, runner, log_path = sys.argv[1:]
payload = {
    "Label": "ai.echodesk.agentos",
    "ProgramArguments": ["/bin/bash", runner],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 5,
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY
plutil -lint "$PLIST_PATH" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "$RUNNER_ENABLED" != "1" ]; then
    echo "AgentOS runtime installed but disabled." >&2
    echo "Configure the built-in DS v4 model and its API key, then rerun this installer." >&2
    exit 0
fi

bootstrapped=0
for _ in $(seq 1 10); do
    if launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null; then
        bootstrapped=1
        break
    fi
    sleep 0.2
done
if [ "$bootstrapped" != "1" ]; then
    echo "AgentOS LaunchAgent bootstrap failed: $PLIST_PATH" >&2
    exit 70
fi
launchctl kickstart -k "gui/$(id -u)/$LABEL"

for _ in $(seq 1 60); do
    if curl -fsS --noproxy '*' --max-time 1 "http://127.0.0.1:4128/api/v1/health" >/dev/null 2>&1; then
        echo "AgentOS ready at http://127.0.0.1:4128"
        exit 0
    fi
    sleep 0.5
done

echo "AgentOS did not become ready; inspect $LOG_DIR/agentos.log" >&2
tail -n 40 "$LOG_DIR/agentos.log" >&2 || true
exit 70
