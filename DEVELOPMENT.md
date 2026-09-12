# Dola Gateway development notes

Technical reference for the service architecture, profile coordination, and task pipeline.

---

## 1. Core architecture

The system operates across three tiers:
1. **API tier (`server.py`)**: FastAPI application exposing OpenAI-compatible image and video endpoints, the unified application at `/`, the focused workspace at `/playground`, and authenticated administration routes under `/api/admin/*`.
2. **Pool Management Tier (`browser_pool.py`)**: Manages persistent browser profiles in `accounts/`, controlling task concurrency, account locking, observed usage, and media-specific persistent restrictions after explicit upstream responses. Restrictions have no assumed reset time and can be tested only by a confirmed, real, pinned generation from the Accounts page. Content rejection and ambiguous errors never restrict an account.
3. **Execution Tier (`image_worker.py`, `video_worker_ui.py`, `video_protocol.py`)**:
   - Automates Dola's browser UI through isolated persistent profiles and injects prompts.
   - Leaves verification challenges to the user and resumes only after completion.
   - Polls accepted video conversations and downloads their completed result.

---

## 2. Duration Configuration

- The bundled Dola-only extension patches existing Dola responses to make the
  application's supported `10s`, `15s`, and `30s` UI choices available.
- It has no Doubao host access, download permission, or media-extraction content script.

---

## 3. High-Definition Stream Processing

- Video rendering status is monitored through the event stream.
- High-definition stream URLs are parsed and downloaded directly to the local storage directory `downloads/`.

---

## 4. Verification Handling

- Detects verification challenges and waits for manual completion in the visible browser.

---

## 5. Application and release boundary

`dola_gateway.server:app` is the application entry point used by local development and the
desktop runtime. The installed app opens `/`, the unified administration interface, while
`/playground` remains a focused client workspace and the API routes remain stable.

Application code and mutable state are deliberately separate. `DOLA_STATE_DIR`
owns relative configuration, profile, database, and download paths;
`DOLA_ENV_FILE` selects the private environment file. Packaged launchers set both
before importing the application, so upgrading or replacing an application
artifact cannot overwrite recipient data.

The top-level `VERSION` file is the canonical release version.
`tools/release/bump_version.py` propagates it to the packaged Python version,
npm lock, Cargo lock, and Tauri configuration; `check_release.py` rejects any
mismatch and enforces exact `vVERSION` tag parity.

Self-contained desktop release workflow:

```bash
pytest -q
python tools/release/check_release.py
python tools/release/build_runtime.py --target TARGET --force --smoke
python tools/release/build_installer.py --target TARGET
```

Use `macos-arm64` only on Apple Silicon macOS and `windows-x64` only on x64
Windows. Production cross-builds are deliberately rejected because both the
Python and Chromium executables must be inspected and smoke-tested natively.
The runtime build downloads on the builder, then imports the packaged app and
launches the bundled full Chromium against an offline page. The installer build
accepts only a non-fixture verified runtime. See `tools/release/README.md` for the
exact tool prerequisites and clean-machine acceptance testing.

The release-tooling surface is intentionally small: `check_release.py` validates
source and versions, `build_runtime.py` and `build_installer.py` create native
outputs, and `create_manifest.py` refuses to assemble a release without both
verified targets. Obsolete protocol clients, bootstrap packages, and launchers
are not maintained.
