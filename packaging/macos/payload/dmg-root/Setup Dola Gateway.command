#!/bin/bash
set -e
root="$(cd "$(dirname "$0")" && pwd)"
. "$root/.locate-app.sh"
app="$(find_dola_app)" || {
    printf 'Dola Gateway.app was not found. Drag it to Applications first.\n'
    read -r
    exit 1
}
exec "$app/Contents/Resources/launcher/setup.command"
