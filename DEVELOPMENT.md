# Video Generation Gateway - Architecture & Technical Notes

Technical reference for the service architecture, profile coordination, and task pipeline.

---

## 1. Core Architecture

The system operates across three tiers:
1. **API Tier (`server.py`)**: FastAPI application exposing standard OpenAI video endpoints (`/v1/videos/generations`, `/v1/videos/<id>`) and web dashboard routes (`/web`, `/api/admin/*`).
2. **Pool Management Tier (`browser_pool.py`)**: Manages persistent browser profiles in `accounts/`, controlling task concurrency, account locking, observed usage, and media-specific persistent restrictions after explicit upstream responses. Restrictions have no assumed reset time and can be tested only by a confirmed, real, pinned generation from the Accounts page. Content rejection and ambiguous errors never restrict an account.
3. **Execution Tier (`video_worker_ui.py`, `video_worker.py`)**:
   - **UI Automation Mode**: Automates browser interactions, authenticates via persistent browser session, injects prompts, and handles verification challenges.
   - **Protocol Mode**: Sends structured SSE requests to completion endpoints and polls status endpoints for video rendering progress.

---

## 2. Duration Configuration

- The browser module manages duration parameters (`15s`, `30s`) via request interception.
- Synchronizes with client action bar configuration to present extended duration selections.

---

## 3. High-Definition Stream Processing

- Video rendering status is monitored through the event stream.
- High-definition stream URLs are parsed and downloaded directly to the local storage directory `downloads/`.

---

## 4. Verification Handling

- Handles verification challenges using automated visual template alignment to ensure reliable background execution.

---

## 5. Application and release boundary

`server:app` remains the compatibility entry point used by launchers and local
development. The installed app opens `/`, the unified owner interface, while
`/playground` remains a focused client workspace and the API routes remain stable.

Application code and mutable state are deliberately separate. `DOLA_STATE_DIR`
owns relative configuration, profile, database, and download paths;
`DOLA_ENV_FILE` selects the private environment file. Packaged launchers set both
before importing the application, so upgrading or replacing an application
artifact cannot overwrite recipient data.

The top-level `VERSION` file is the only release version source. The runtime
reads it through `app_version.py`, and both builders read the same file. Never add
a platform-specific version file.

Self-contained desktop release workflow:

```bash
pytest -q
python packaging/check_release.py
python packaging/desktop/build_runtime.py --target TARGET --force --smoke
python packaging/desktop/build_desktop.py --target TARGET
```

Use `macos-arm64` only on Apple Silicon macOS and `windows-x64` only on x64
Windows. Production cross-builds are deliberately rejected because both the
Python and Chromium executables must be inspected and smoke-tested natively.
The runtime build downloads on the builder, then imports the packaged app and
launches the bundled full Chromium against an offline page. The installer build
accepts only a non-fixture verified runtime. See `packaging/README.md` for the
exact tool prerequisites and clean-machine acceptance testing.

`packaging/build_release.py`, `packaging/macos/`, and `packaging/windows/` are
the earlier bootstrap/portable packaging path. They remain for compatibility
but are not a self-contained trusted-friend desktop release: those launchers may
download runtime components or require developer prerequisites after delivery.
