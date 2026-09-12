# Dola Gateway

Dola Gateway is a local multi-account session coordinator for Dola image and
video generation.

It provides browser-session isolation, task queue distribution, extended
duration handling, and a local application for creating media and managing the
gateway.

The application has one browser entry point at
**http://127.0.0.1:8000/**. It brings account setup, pool status, prompt creation,
and generation history together; the focused prompt workspace remains available
at `/playground`.

---

## Desktop installers

Build self-contained desktop installers on the destination platform. Installer
artifacts do not require Python, Node, Rust, Patchright, or a system browser on
the target machine. Do not include development environments, browser profiles,
or local application data in a distribution.

```bash
# macOS arm64, on an Apple Silicon Mac
.venv/bin/python tools/release/build_runtime.py \
  --target macos-arm64 --output dist/desktop-runtime/macos-arm64 --force --smoke
.venv/bin/python tools/release/build_installer.py --target macos-arm64

# Windows x64, in PowerShell on an x64 Windows builder
python tools/release/build_runtime.py `
  --target windows-x64 --output dist/desktop-runtime/windows-x64 --force --smoke
python tools/release/build_installer.py --target windows-x64
```

Distribute the matching DMG or NSIS installer from `dist/desktop-installers/`
together with its `.sha256` file. The app contains the exact CPython runtime,
application dependencies, and full Patchright Chromium revision recorded in
`runtime-manifest.json`. It starts its private loopback service itself and opens
the unified interface in a thin Tauri window.

Each installation uses its own accounts. Configuration, profiles, databases,
media, and logs are created in the operating system's private app-data directory,
outside the installed app. Updates are manual: quit the app, install the
replacement, and reopen it; the state directory is retained. Developer builds
are unsigned and not notarized, so they should be used only in trusted
environments.
See [tools/release/README.md](tools/release/README.md) for verification, platform status,
state boundaries, and clean-machine acceptance testing.

---

## Key capabilities

1. **OpenAI-Compatible Video API**:
   - `POST /v1/videos/generations`: Submit generation tasks with prompt, aspect ratio, duration (`10s`, `15s`, `30s`), and reference images.
   - `GET /v1/videos/<id>`: Poll task lifecycle (`queued` -> `processing` -> `completed` / `failed`).
   - High-speed MP4 streaming and static asset delivery.
2. **Extended Duration & Media Export**:
   - Dola-only browser extension support for `10s`, `15s`, and `30s` UI choices.
   - Generated media collection and local download handling.
3. **Multi-Account Browser Pool**:
   - Manages multiple persistent browser profiles in `accounts/`.
   - Automatic concurrency management, mutual exclusion, and session rotation.
   - Records observed image/video usage without assuming a Dola daily allowance.
   - Built-in verification handling.
4. **Unified Local Application**:
   - Create and review images or videos, manage account sessions, inspect activity, and check gateway settings from `/`.
   - The focused creation workspace remains available at `/playground` for compatibility.

Dola currently provides no dependable numeric quota, credit balance, or documented
reset time on the web. Explicit daily-limit, credit, and risk-control messages create
a persistent restriction for that account and media type. Nothing retries or clears
it on a timer. An administrator can choose **Retry video now** or **Retry image
now** on the Accounts page, enter a real prompt, and confirm one pinned attempt
with no probe or fallback. Content rejection and ambiguous upstream errors fail
only the task.

---

## Repository structure

```
dola-image-gateway/
├── VERSION                # Single runtime and release version
├── CHANGELOG.md           # Versioned GitHub release notes
├── pyproject.toml         # Project metadata and test configuration
├── src/dola_gateway/
│   ├── server.py          # FastAPI application, API, admin, and UI routes
│   ├── browser_pool.py    # Account concurrency manager and scheduler
│   ├── image_worker.py    # Dola image UI automation and result collection
│   ├── video_worker_ui.py # Dola video UI automation and verification handling
│   ├── video_protocol.py  # Active video result polling and download helpers
│   ├── web/               # Unified local application
│   └── extensions/dola30/ # Least-privilege Dola duration extension
├── desktop/               # Tauri shell source and runtime placeholder
├── tools/release/         # Reproducible desktop runtime and installer builders
├── tests/                 # Offline API, browser, release, and regression tests
└── .github/workflows/     # CI, dual-native build, and gated release publish
```

---

## Quick start

### 1. Requirements

* Python 3.11+
* Patchright's bundled Chromium
* Proxy with JP/KR egress

### 2. Set up the environment

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# Install runtime and development dependencies
pip install -r requirements-dev.txt
patchright install chromium
```

### Smoke test

Before adding an account or contacting Dola, run the offline test suite:

```bash
pytest -q
```

The suite compiles the Python sources, launches Patchright's bundled Chromium
against an offline page, and checks the local API and desktop integration paths.
A system Chrome installation is optional.

### Account profiles

The gateway discovers accounts directly from isolated profile directories under
`accounts/`. Account labels, scheduling state, login status, and restriction
metadata are stored in `pool_usage.db`; there is no separate account inventory
file to maintain.

To authenticate an account, start the server and open the Accounts tab in the
dashboard. Enter a unique profile name and optional email label, then select
**Open Google Login**. Complete Google credentials and 2FA directly in the
headed Chromium window. The gateway detects the resulting Dola session and
stores it only in `accounts/<name>/`.

Repeat this flow for each account. Password, TOTP, cookie, and session fields
are never accepted by the admin API. Removing an idle account deletes its saved
browser profile while retaining generation history.

### 3. Configure
```bash
# Set your proxy configuration
export DOLA_PROXY="http://127.0.0.1:7890"

# Set API key for client authentication (optional, empty = dev mode)
export DOLA_API_KEYS="sk-your-secret-key"

# Concurrency limits
export DOLA_MAX_CONCURRENCY=3

# Allow slow Dola video renders to finish (seconds)
export DOLA_VIDEO_TIMEOUT=900
```

### 4. Start the server

```bash
.venv/bin/python -m uvicorn dola_gateway.server:app --host 127.0.0.1 --port 8000
```
Open **http://127.0.0.1:8000/** to access the complete local application.

### 5. Try an image or video prompt in the browser

Open **http://127.0.0.1:8000/**. Enter a client API key, prompt, then choose Image
or Video. Image mode supports aspect ratio and style. Video mode
supports aspect ratio, Seedance model, 10/15/30-second duration, and optional
reference-image URLs. The page submits through the same account pool, shows queued
and processing states, and keeps a combined client-scoped Library for images and
videos. Completed media can be previewed or downloaded from the selected result.
Accounts and Activity use the admin password and keep it in page memory only.

The API key remains in page memory by default. The optional **Remember for this
tab** setting uses `sessionStorage`, survives reloads, and clears when the tab
closes; **Clear key** removes it immediately.
Use a client key with an appropriate daily and concurrency limit. Generated image
and video content is returned only after the requesting client is authenticated.

### 6. Generate an image through the API

The endpoint is asynchronous: submit once, then poll the returned task ID. If
`DOLA_API_KEYS` is empty, omit the `Authorization` header in local development.

```bash
export DOLA_API_KEY="sk-your-secret-key"

curl -sS http://127.0.0.1:8000/v1/images/generations \
  -H "Authorization: Bearer $DOLA_API_KEY" \
  -H "Idempotency-Key: your-unique-request-id" \
  -H "Content-Type: application/json" \
  -d '{"model":"dola-image","prompt":"A blue circle on white","size":"1024x1024","style":"minimal"}'

curl -sS http://127.0.0.1:8000/v1/images/IMAGE_TASK_ID \
  -H "Authorization: Bearer $DOLA_API_KEY"

curl -sS http://127.0.0.1:8000/v1/images/IMAGE_TASK_ID/content \
  -H "Authorization: Bearer $DOLA_API_KEY" \
  -o result.png
```

Supported ratios are `1:1`, `2:3`, `16:9`, `9:16`, `4:3`, and
`3:4`. Generated files are decode-checked, size-limited, and stored under
`downloads/images/`. Only accounts whose login check passed are scheduled.
The task response includes `image_urls` for every generated result and keeps
`image_url` as an alias for the first result.

## License

No license file is currently included in this repository.
