#!/usr/bin/env bash

set -euo pipefail

ECHODESK_HOME="${ECHODESK_HOME:-$HOME/.echodesk}"
PYTHON_BIN="${ECHODESK_AGENTOS_PYTHON:-$ECHODESK_HOME/source/agentos/.venv/bin/python}"
AGENTOS_SOURCE="${ECHODESK_AGENTOS_SOURCE:-$ECHODESK_HOME/source/agentos}"
CONFIG_PATH="${ECHODESK_CONFIG_PATH:-$ECHODESK_HOME/config.json}"
PROXY_PORT="${ECHODESK_AGENTOS_PROXY_PORT:-4127}"
SERVER_PORT="${AGENTOS_SERVER_PORT:-4128}"
AGENTOS_DATA_DIR="${ECHODESK_AGENTOS_DATA_DIR:-$ECHODESK_HOME/agentos}"

CLAUDE_BIN="${ECHODESK_CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ]; then
    for candidate in \
        "$HOME/.local/bin/claude" \
        "/opt/homebrew/bin/claude" \
        "/usr/local/bin/claude"; do
        if [ -x "$candidate" ]; then
            CLAUDE_BIN="$candidate"
            break
        fi
    done
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
    echo "Claude Code CLI is missing; set ECHODESK_CLAUDE_BIN" >&2
    exit 66
fi
export PATH="$(dirname "$CLAUDE_BIN"):/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v whisper >/dev/null 2>&1; then
    echo "Whisper CLI is missing; install openai-whisper or set PATH" >&2
    exit 66
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "AgentOS Python is missing: $PYTHON_BIN" >&2
    exit 66
fi
if [ ! -f "$AGENTOS_SOURCE/agentos/server/__main__.py" ]; then
    echo "AgentOS source is missing: $AGENTOS_SOURCE" >&2
    exit 66
fi
if [ ! -f "$CONFIG_PATH" ]; then
    echo "EchoDesk config is missing: $CONFIG_PATH" >&2
    exit 66
fi

read_config() {
    "$PYTHON_BIN" - "$CONFIG_PATH" "$1" "$2" <<'PY'
import json
import sys

path, key, default = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    value = json.load(handle).get(key, default)
print(str(value).strip())
PY
}

MAIN_PROVIDER="$(read_config llm_main_provider openai-compatible)"
MAIN_MODEL="$(read_config llm_main_model '')"
MAIN_BASE_URL="$(read_config llm_main_base_url '')"
MAIN_API_KEY="$(read_config llm_main_api_key '')"
if [ -z "$MAIN_API_KEY" ]; then
    MAIN_API_KEY="$(read_config yunwu_open_key '')"
fi

if [ -z "$MAIN_PROVIDER" ] || [ -z "$MAIN_MODEL" ] || [ -z "$MAIN_BASE_URL" ]; then
    echo "AgentOS cannot start: main-model provider, model and base URL are required" >&2
    exit 78
fi
if [ -z "$MAIN_API_KEY" ]; then
    PRIVATE_UPSTREAM="$($PYTHON_BIN - "$MAIN_BASE_URL" <<'PY'
import ipaddress
import sys
import urllib.parse

try:
    parsed = urllib.parse.urlsplit(sys.argv[1])
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    if parsed.hostname.lower() == "localhost":
        print("1")
    else:
        address = ipaddress.ip_address(parsed.hostname)
        print("1" if address.is_private or address.is_loopback else "0")
except ValueError:
    print("0")
PY
)"
    if [ "$PRIVATE_UPSTREAM" = "1" ]; then
        MAIN_API_KEY="agentos-internal-vllm-no-auth"
    else
        echo "AgentOS cannot start: public main-model API key is empty" >&2
        exit 78
    fi
fi

mkdir -p "$AGENTOS_DATA_DIR/workspaces"

if lsof -ti "tcp:$PROXY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "AgentOS proxy port $PROXY_PORT is already in use" >&2
    exit 75
fi
if lsof -ti "tcp:$SERVER_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "AgentOS server port $SERVER_PORT is already in use" >&2
    exit 75
fi

export PYTHONPATH="$AGENTOS_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
export AGENTOS_PROXY_AUTOCONFIG_ECHO=false
export AGENTOS_PROXY_UPSTREAM_BASE_URL="$MAIN_BASE_URL"
export AGENTOS_PROXY_UPSTREAM_MODEL="$MAIN_MODEL"
export AGENTOS_PROXY_UPSTREAM_API_KEY="$MAIN_API_KEY"
export AGENTOS_PROXY_REASONING_TOKEN_BUDGET=0
export HTTP_PROXY=""
export HTTPS_PROXY=""
export ALL_PROXY=""
export NO_PROXY="*"

PROXY_PID=""
SERVER_PID=""

cleanup() {
    trap - EXIT INT TERM
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null || true
    fi
    if [ -n "$SERVER_PID" ]; then
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    if [ -n "$PROXY_PID" ]; then
        wait "$PROXY_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

PORT="$PROXY_PORT" "$PYTHON_BIN" -m agentos.proxy.anthropic_to_openai &
PROXY_PID=$!

proxy_ready=0
for _ in $(seq 1 60); do
    if curl -sS --noproxy '*' --max-time 1 "http://127.0.0.1:$PROXY_PORT/" >/dev/null 2>&1; then
        proxy_ready=1
        break
    fi
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done
if [ "$proxy_ready" != "1" ]; then
    echo "AgentOS proxy failed to start on port $PROXY_PORT" >&2
    exit 70
fi

"$PYTHON_BIN" -m agentos.server \
    --host 127.0.0.1 \
    --port "$SERVER_PORT" \
    --workspaces "$AGENTOS_DATA_DIR/workspaces" \
    --proxy-url "http://127.0.0.1:$PROXY_PORT" \
    --log-level info &
SERVER_PID=$!
wait "$SERVER_PID"
