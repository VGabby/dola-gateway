#!/bin/bash
root="$(cd "$(dirname "$0")" && pwd)"
. "$root/.locate-app.sh"
app="$(find_dola_app)" || { printf 'Dola Gateway.app was not found.\n'; read -r; exit 1; }
"$app/Contents/Resources/launcher/doctor.sh"
status=$?
read -r -p 'Press Return to close...'
exit "$status"
