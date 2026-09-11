#!/bin/bash

LAUNCHER_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
RESOURCE_DIR="$(cd "$LAUNCHER_DIR/.." && pwd)"
APP_DIR="$RESOURCE_DIR/app"
EXPECTED_ARCH="$(tr -d '[:space:]' < "$RESOURCE_DIR/ARCHITECTURE")"
STATE_DIR="$HOME/Library/Application Support/DolaGateway"
CACHE_DIR="$HOME/Library/Caches/DolaGateway"
LOG_DIR="$HOME/Library/Logs/DolaGateway"
UV_DIR="$CACHE_DIR/uv"
UV_BIN="$UV_DIR/uv"
VENV_DIR="$CACHE_DIR/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
BROWSER_DIR="$CACHE_DIR/browsers"
ENV_FILE="$STATE_DIR/.env.local"
PID_FILE="$STATE_DIR/gateway.pid"
VERSION_FILE="$RESOURCE_DIR/VERSION"

export DOLA_STATE_DIR="$STATE_DIR"
export DOLA_ENV_FILE="$ENV_FILE"
export PLAYWRIGHT_BROWSERS_PATH="$BROWSER_DIR"
export PYTHONUTF8=1

display_path() {
    local value="$1"
    if [[ "${DOLA_REDACT_PATHS:-0}" == "1" && "$value" == "$HOME"* ]]; then
        printf '<HOME>%s' "${value#"$HOME"}"
    else
        printf '%s' "$value"
    fi
}

read_env() {
    local name="$1"
    local default_value="$2"
    if [[ ! -f "$ENV_FILE" ]]; then
        printf '%s' "$default_value"
        return
    fi
    local value
    value="$(/usr/bin/grep -E "^[[:space:]]*$name[[:space:]]*=" "$ENV_FILE" | /usr/bin/head -n 1 | /usr/bin/cut -d= -f2-)"
    if [[ -n "$value" ]]; then printf '%s' "$value"; else printf '%s' "$default_value"; fi
}

gateway_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local value
    value="$(tr -d '[:space:]' < "$PID_FILE")"
    [[ "$value" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$value"
}

gateway_running() {
    local pid
    pid="$(gateway_pid)" || return 1
    /bin/kill -0 "$pid" 2>/dev/null || return 1
    /bin/ps -p "$pid" -o command= 2>/dev/null | /usr/bin/grep -q "uvicorn server:app"
}

check_architecture() {
    local actual
    actual="$(/usr/bin/uname -m)"
    if [[ "$actual" != "$EXPECTED_ARCH" ]]; then
        printf 'This package is for %s, but this Mac is running %s.\n' "$EXPECTED_ARCH" "$actual" >&2
        return 1
    fi
}

show_error() {
    printf '%s\n' "$1" >&2
    /usr/bin/osascript -e 'display alert "Dola Gateway" message "The operation failed. Run Diagnose Dola Gateway from the disk image for details." as critical' >/dev/null 2>&1 || true
}
