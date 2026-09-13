# Changelog

All notable changes to Dola Gateway are documented here.

## [Unreleased]

## [0.2.3-rc.2] - 2026-09-13

- Installed Patchright's pinned test browser in native release jobs so clean GitHub runners can complete the offline UI suite before packaging.

## [0.2.3-rc.1] - 2026-09-13

- Kept Tauri's internal `tauri.localhost` startup URL inside the desktop webview instead of opening it in the system browser.
- Made image automation headed by default in every mode so users can complete Dola verification, while retaining `DOLA_IMAGE_HEADLESS=1` as an explicit override.

## [0.2.2] - 2026-09-12

- Packaged the Python runtime under `dola_gateway` and removed obsolete protocol paths.
- Reduced the bundled extension to Dola-only 10/15/30-second duration behavior with least privileges.
- Added strict SemVer propagation, deterministic release metadata, and offline release validation.
- Added gated unsigned macOS ARM64 DMG and Windows x64 NSIS builds with normalized names and checksums.

## [0.2.2-rc.2] - 2026-09-12

- Corrected GitHub Actions environment isolation paths for workflow validation.

## [0.2.2-rc.1] - 2026-09-12

- Refactored the runtime into the `dola_gateway` package and removed obsolete protocol paths.
- Reduced the bundled extension to Dola-only 10/15/30-second duration behavior.
- Added locked offline CI and gated unsigned macOS ARM64 and Windows x64 release assembly.

## [0.2.1] - 2026-09-12

- Added a self-contained Tauri desktop shell with isolated local application state.
- Added reproducible Python and Chromium runtime assembly for macOS ARM64 and Windows x64.
- Added offline release validation, deterministic checksums, and native installer verification.
