# Dola Gateway desktop shell

This directory is a deliberately thin Tauri v2 shell over the existing FastAPI
application. Rust owns one app window and one child process; Python continues to
own product behavior and the web UI.

- `src-tauri/src/main.rs` supervises startup, readiness, recovery, single-instance
  focus, loopback navigation, external links, and shutdown.
- `ui/` is the local startup/recovery surface shown before FastAPI is ready.
- `runtime-placeholder/` contains only a non-distributable placeholder in source. Release
  builds inject an assembled runtime into a clean staging copy.
- `package-lock.json`, `src-tauri/Cargo.lock`, and `rust-toolchain.toml` pin the
  native shell toolchain.

For source-only shell checks:

```bash
cd desktop
npm ci --ignore-scripts
cd src-tauri
RUSTC="$(rustup which --toolchain 1.88.0 rustc)" rustup run 1.88.0 cargo test
```

To launch against a separately assembled runtime during development, set
`DOLA_DESKTOP_RUNTIME_DIR` to its absolute path before running the Tauri app.
Production installers must be created with
`tools/release/build_installer.py`; it rejects the placeholder and test
fixtures. See `tools/release/README.md` for the complete release workflow.
