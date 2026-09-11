#!/bin/bash
set -u
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

pid="$(gateway_pid)" || {
    /bin/rm -f "$PID_FILE"
    printf 'Dola Gateway is not running.\n'
    exit 0
}
if ! gateway_running; then
    /bin/rm -f "$PID_FILE"
    printf 'Dola Gateway is not running; removed a stale PID file.\n'
    exit 0
fi

/bin/kill "$pid"
for _ in $(/usr/bin/seq 1 10); do
    /bin/kill -0 "$pid" 2>/dev/null || break
    /bin/sleep 1
done
if /bin/kill -0 "$pid" 2>/dev/null; then /bin/kill -9 "$pid"; fi
/bin/rm -f "$PID_FILE"
printf 'Dola Gateway stopped.\n'
