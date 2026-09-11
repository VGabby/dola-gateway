#!/bin/bash
set -u
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

if ! check_architecture; then
    show_error "Wrong package architecture"
    exit 1
fi
if [[ ! -x "$VENV_PYTHON" || ! -f "$ENV_FILE" ]]; then
    /usr/bin/open -a Terminal "$LAUNCHER_DIR/setup.command"
    exit 0
fi

port="$(read_env DOLA_PORT 8000)"
if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    show_error "Invalid DOLA_PORT in $ENV_FILE"
    exit 1
fi
admin_key="$(read_env DOLA_ADMIN_KEY "")"
client_key="$(read_env DOLA_API_KEYS "")"
if [[ -z "$admin_key" || -z "$client_key" || "$admin_key" == replace-* || "$client_key" == replace-* ]]; then
    show_error "Private API/admin keys are missing from $ENV_FILE"
    exit 1
fi

url="http://127.0.0.1:$port/"
if gateway_running; then
    /usr/bin/open "$url"
    exit 0
fi
/bin/rm -f "$PID_FILE"
mkdir -p "$LOG_DIR"
stamp="$(/bin/date +%Y%m%d-%H%M%S)"
[[ -f "$LOG_DIR/server.out.log" ]] && /bin/mv "$LOG_DIR/server.out.log" "$LOG_DIR/server-$stamp.out.log"
[[ -f "$LOG_DIR/server.err.log" ]] && /bin/mv "$LOG_DIR/server.err.log" "$LOG_DIR/server-$stamp.err.log"

cd "$APP_DIR"
/usr/bin/nohup "$VENV_PYTHON" -m uvicorn server:app --host 127.0.0.1 --port "$port" \
    >>"$LOG_DIR/server.out.log" 2>>"$LOG_DIR/server.err.log" &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

ready=0
for _ in $(/usr/bin/seq 1 30); do
    /bin/sleep 1
    if ! /bin/kill -0 "$pid" 2>/dev/null; then break; fi
    if /usr/bin/curl --silent --fail --max-time 2 "http://127.0.0.1:$port/health" >/dev/null; then
        ready=1
        break
    fi
done
if [[ "$ready" -ne 1 ]]; then
    /bin/kill "$pid" 2>/dev/null || true
    /bin/rm -f "$PID_FILE"
    show_error "Dola Gateway did not become healthy. See $LOG_DIR/server.err.log"
    /usr/bin/open -a Console "$LOG_DIR/server.err.log" 2>/dev/null || true
    exit 1
fi

/usr/bin/open "$url"
