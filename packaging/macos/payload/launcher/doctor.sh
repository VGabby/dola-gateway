#!/bin/bash
set -u
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

failed=0
printf 'Dola Gateway diagnostics\n'
if [[ -f "$VERSION_FILE" ]]; then printf 'Version:      %s\n' "$(tr -d '[:space:]' < "$VERSION_FILE")"; fi
printf 'Architecture: expected=%s actual=%s\n' "$EXPECTED_ARCH" "$(/usr/bin/uname -m)"
printf 'Application:  %s\n' "$(display_path "$APP_DIR")"
printf 'Data:         %s\n' "$(display_path "$STATE_DIR")"
printf 'Runtime:      %s\n' "$(display_path "$CACHE_DIR")"
printf 'Logs:         %s\n' "$(display_path "$LOG_DIR")"

check_architecture || failed=1
if [[ -x "$VENV_PYTHON" ]]; then
    "$VENV_PYTHON" --version
    (cd "$APP_DIR" && "$VENV_PYTHON" -c "import fastapi, aiohttp, pydantic, patchright, PIL, cv2; import config; print('[OK] Dependencies and configuration load')") || failed=1
    "$VENV_PYTHON" "$LAUNCHER_DIR/browser_check.py" || failed=1
else
    printf '[FAIL] Runtime is missing. Run setup first.\n' >&2
    failed=1
fi

if gateway_running; then
    port="$(read_env DOLA_PORT 8000)"
    /usr/bin/curl --silent --fail --max-time 3 "http://127.0.0.1:$port/health" >/dev/null \
        && printf '[OK] Local health endpoint is responding\n' \
        || { printf '[FAIL] Gateway process exists but health failed\n' >&2; failed=1; }
else
    printf '[INFO] Gateway is not running\n'
fi
exit "$failed"
