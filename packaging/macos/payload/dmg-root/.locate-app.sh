#!/bin/bash
find_dola_app() {
    local here
    here="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
    for app in "/Applications/Dola Gateway.app" "$HOME/Applications/Dola Gateway.app" "$here/Dola Gateway.app"; do
        if [[ -d "$app" ]]; then printf '%s' "$app"; return 0; fi
    done
    return 1
}
