#!/bin/bash
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

include_profiles=0
if [[ "$#" -gt 0 && "$1" == "--include-profiles" ]]; then include_profiles=1; fi
if gateway_running; then
    printf 'Stop Dola Gateway from its dashboard before creating a backup.\n' >&2
    exit 1
fi

backup_dir="$HOME/Documents/DolaGateway Backups"
mkdir -p "$backup_dir" "$CACHE_DIR"
stamp="$(/bin/date +%Y%m%d-%H%M%S)"
stage="$(/usr/bin/mktemp -d "$CACHE_DIR/backup.XXXXXX")"
trap '/bin/rm -rf "$stage"' EXIT
mkdir -p "$stage/DolaGateway"

for name in .env.local accounts.local.json tasks.db pool_usage.db downloads; do
    [[ -e "$STATE_DIR/$name" ]] && /usr/bin/ditto "$STATE_DIR/$name" "$stage/DolaGateway/$name"
done
if [[ "$include_profiles" -eq 1 && -d "$STATE_DIR/accounts" ]]; then
    /usr/bin/ditto "$STATE_DIR/accounts" "$stage/DolaGateway/accounts"
fi

archive="$backup_dir/DolaGateway-$stamp.zip"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$stage/DolaGateway" "$archive"
printf 'Backup created: %s\n' "$archive"
printf 'This archive contains sensitive configuration, history, and media. Never send it to anyone.\n'
if [[ "$include_profiles" -eq 1 ]]; then
    printf 'It also contains reusable browser login sessions.\n'
fi
