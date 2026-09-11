#!/bin/bash
set -u
. "$(cd "$(dirname "$0")" && pwd)/common.sh"

support_dir="$HOME/Documents/DolaGateway Support"
mkdir -p "$support_dir" "$CACHE_DIR"
stamp="$(/bin/date +%Y%m%d-%H%M%S)"
stage="$(/usr/bin/mktemp -d "$CACHE_DIR/support.XXXXXX")"
trap '/bin/rm -rf "$stage"' EXIT
bundle="$stage/DolaGateway-Support-$stamp"
mkdir -p "$bundle"

{
    printf 'Shareable Dola Gateway diagnostic report\n'
    printf 'Created: %s\n\n' "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$bundle/diagnostics.txt"
escaped_home="$(printf '%s' "$HOME" | /usr/bin/sed 's/[][\\.^$*|&]/\\&/g')"
DOLA_REDACT_PATHS=1 "$LAUNCHER_DIR/doctor.sh" 2>&1 \
    | /usr/bin/sed "s|$escaped_home|<HOME>|g" >> "$bundle/diagnostics.txt" || true

{
    printf 'Log inventory only; log contents are intentionally excluded.\n'
    for log in "$LOG_DIR"/*.log; do
        [[ -f "$log" ]] || continue
        printf '%s  bytes=%s  sha256=%s\n' \
            "$(basename "$log")" \
            "$(/usr/bin/stat -f %z "$log")" \
            "$(/usr/bin/shasum -a 256 "$log" | /usr/bin/awk '{print $1}')"
    done
} > "$bundle/log-inventory.txt"

if [[ -f "$ENV_FILE" ]]; then
    /usr/bin/awk -F= '/^[[:space:]]*DOLA_[A-Z0-9_]+[[:space:]]*=/{key=$1; gsub(/[[:space:]]/, "", key); print key "=<redacted>"}' \
        "$ENV_FILE" > "$bundle/configured-settings.txt"
fi

archive="$support_dir/DolaGateway-Support-$stamp.zip"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$bundle" "$archive"
printf 'Shareable support bundle created: %s\n' "$archive"
printf 'It excludes keys, profiles, databases, generated media, and log contents.\n'
