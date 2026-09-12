# Changelog

All notable changes to Dola Gateway are documented here.

## [Unreleased]

## [0.2.2-rc.1] - 2026-09-12

- Refactored the runtime into the `dola_gateway` package and removed obsolete protocol paths.
- Reduced the bundled extension to Dola-only 10/15/30-second duration behavior.
- Added locked offline CI and gated unsigned macOS ARM64 and Windows x64 release assembly.

## [0.2.1] - 2026-09-12

- Added a self-contained Tauri desktop shell with isolated local application state.
- Added reproducible Python and Chromium runtime assembly for macOS ARM64 and Windows x64.
- Added offline release validation, deterministic checksums, and native installer verification.
