# Dola Gateway

A local multi-account session coordinator for Dola image and video generation.

Provides automated browser session isolation, task queue distribution, extended duration handling, and one local application for creating media and managing the gateway.

The finished local product has one browser entry point at
**http://127.0.0.1:8000/**. It brings account setup, pool status, prompt creation,
and generation history together; the focused prompt workspace remains available
at `/playground`.

---

## Deliver it to a friend

Do not send the repository, a virtual environment, browser profiles, or a copy
of your local data. Build a self-contained desktop installer on the destination
platform. The recipient does not install Python, Node, Rust, Patchright, or a
browser.

```bash
# macOS arm64, on an Apple Silicon Mac
.venv/bin/python packaging/desktop/build_runtime.py \
  --target macos-arm64 --output dist/desktop-runtime/macos-arm64 --force --smoke
.venv/bin/python packaging/desktop/build_desktop.py --target macos-arm64

# Windows x64, in PowerShell on an x64 Windows builder
python packaging/desktop/build_runtime.py `
  --target windows-x64 --output dist/desktop-runtime/windows-x64 --force --smoke
python packaging/desktop/build_desktop.py --target windows-x64
```

Send the matching DMG or NSIS installer from `dist/desktop-installers/` together
with its `.sha256` file. The app contains the exact CPython runtime, application
dependencies, and full Patchright Chromium revision recorded in
`runtime-manifest.json`. It starts its private loopback service itself and opens
the unified interface in a thin Tauri window.

Each recipient signs in with their own accounts. Configuration, profiles,
databases, media, and logs are created in the operating system's private app-data
directory, outside the installed app. Updates are manual: quit the app, install
the replacement, and reopen it; the state directory is retained. Developer
builds are unsigned and not notarized, so distribute them only to trusted users.
See [packaging/README.md](packaging/README.md) for verification, platform status,
state boundaries, and clean-machine acceptance testing.

---

## 🌟 Key Capabilities

1. **OpenAI-Compatible Video API**:
   - `POST /v1/videos/generations`: Submit generation tasks with prompt, aspect ratio, duration (`10s`, `15s`, `30s`), and reference images.
   - `GET /v1/videos/<id>`: Poll task lifecycle (`queued` -> `processing` -> `completed` / `failed`).
   - High-speed MP4 streaming and static asset delivery.
2. **Extended Duration & High-Definition Media Export**:
   - Integrated browser automation profile for managing extended duration options.
   - Direct original quality stream extraction and processing.
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
it on a timer. An owner can choose **Retry video now** or **Retry image now** on the
Accounts page, enter a real prompt, and confirm one pinned attempt with no probe or
fallback. Content rejection and ambiguous upstream errors fail only the task.

---

## 📁 Repository Structure

```
dola-image-gateway/
├── VERSION                # Single runtime and release version
├── pyproject.toml         # Project metadata and test configuration
├── server.py              # FastAPI application, client API, owner API, and UI routes
├── app_version.py         # Loads the canonical version
├── browser_pool.py        # Account pool concurrency manager and task scheduler
├── browser.py             # Playwright persistent context launcher
├── image_worker.py        # Dola image automation and result collection
├── video_worker_ui.py     # UI automation worker with verification handler
├── video_worker.py        # Protocol worker and status polling
├── store.py               # SQLite task persistence and API key storage
├── upstream_errors.py     # Multilingual upstream outcome classifier
├── dola_client.py         # API client communication module
├── media.py               # Reference media processor
├── config.py              # Configuration & environment variables
├── add_account.py         # Automated account profile setup
├── web/
│   └── playground.html    # Unified Create, Library, Accounts, Activity, and Settings UI
├── packaging/             # Cross-platform builders, launchers, checks, and support tools
├── tests/                 # Offline API, browser, release, and regression tests
└── extensions/
    └── dola30/            # Chromium extension profile
```

---

## 🚀 Quick Start

### 1. Requirements
* Python 3.11+
* Patchright's bundled Chromium
* Proxy with JP/KR egress

### 2. Setup Environment
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
patchright install chromium
```

### Smoke test

Before adding any account or contacting Dola, install the development dependencies
and run the offline smoke suite:

```bash
pip install -r requirements-dev.txt
patchright install chromium
pytest -q
```

The suite compiles every Python file, validates the credential-free account
inventory, launches Patchright's bundled Chromium against an offline page, and
checks the local `/health` endpoint. A system Chrome installation is optional.

### Multiple-account inventory

Copy `accounts.local.json.example` to `accounts.local.json` and add one entry per
account. This file contains labels only—never put passwords, TOTP secrets,
cookies, or session IDs in it. The actual browser sessions live in the ignored
`accounts/<name>/` profile directories.

To authenticate an account, start the server and open the Accounts tab in the
dashboard. Enter a unique profile name and optional email label, then select
**Open Google Login**. Complete Google credentials and 2FA directly in the
headed Chromium window. The gateway detects the resulting Dola session and
stores it only in `accounts/<name>/`.

Repeat this flow for each account. Password, TOTP, cookie, and session fields
are rejected by the admin API.

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

### 4. Start Server
```bash
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000
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
image and video content is returned only after the task owner is authenticated.

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


### 🌐 SonicVoice (For Voice Clone)

[![Website](https://img.shields.io/badge/Website-SonicVoice.pro-6366f1?style=for-the-badge&logo=google-chrome&logoColor=white)](https://sonicvoice.pro)

### 💬 Admin & Support

[![Telegram](https://img.shields.io/badge/Telegram-Bang%20Ngọc%20Thái-229ED9?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/BangNgocThai47271)

---

## 📜 License
For educational and internal testing purposes.
