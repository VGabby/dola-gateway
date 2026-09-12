# Dola Duration Extension

This profile extension preserves the current Dola response and adds any missing
`10s`, `15s`, and `30s` video-duration choices used by the desktop application.
It runs only on Dola pages and does not inspect media responses or download files.

## Key Notes
1. **Incognito Mode**: If running in incognito or headless browser profiles, enable "Allow in Incognito" in `chrome://extensions/`.
2. **Prompt Guidelines**: When starting a new conversation for 30s generation, avoid putting duration words (e.g. "30s", "30 seconds") directly in the prompt text. The duration is selected in Dola's UI.
