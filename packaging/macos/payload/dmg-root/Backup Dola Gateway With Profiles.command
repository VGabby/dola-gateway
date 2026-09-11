#!/bin/bash
set -e
printf 'WARNING: This backup includes reusable browser login sessions. Never send it to anyone.\n'
read -r -p 'Press Return to continue or close this window to cancel...'
root="$(cd "$(dirname "$0")" && pwd)"
. "$root/.locate-app.sh"
app="$(find_dola_app)" || { printf 'Dola Gateway.app was not found.\n'; read -r; exit 1; }
"$app/Contents/Resources/launcher/backup.sh" --include-profiles
read -r -p 'Press Return to close...'
