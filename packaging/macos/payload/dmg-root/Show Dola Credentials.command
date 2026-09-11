#!/bin/bash
env_file="$HOME/Library/Application Support/DolaGateway/.env.local"
if [[ ! -f "$env_file" ]]; then
    printf 'Private configuration does not exist. Run setup first.\n'
else
    printf 'Keep these credentials private.\n\n'
    /usr/bin/grep -E '^DOLA_(ADMIN_KEY|API_KEYS)=' "$env_file"
fi
read -r -p 'Press Return to close...'
