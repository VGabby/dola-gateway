# Release tooling for the self-contained desktop app

Recipients receive a native installer, never the repository, a virtual
environment, browser profiles, or a state backup. The installer includes the
thin Tauri shell, the allowlisted Python application, a pinned standalone
CPython runtime, locked binary dependencies, and Patchright's matching full
Chromium build. The Windows NSIS installer uses Tauri's downloaded WebView2
bootstrapper: Windows needs network access during installation only if WebView2
is not already installed. The separate Chromium automation browser is still bundled.

## Supported release targets

| Target | Build host | Installer | Status in this repository |
| --- | --- | --- | --- |
| `macos-arm64` | Apple Silicon macOS | `.dmg` plus `.app` QA copy | Code and native artifact complete; clean-machine UI acceptance remains manual |
| `windows-x64` | x64 Windows | per-user NSIS `.exe` | Native GitHub Actions build, runtime smoke test, installer verification, and artifact upload pass; clean-machine UI acceptance remains manual |

There is no Linux target and no background service installation. Production
cross-builds are rejected. Updates are manual and preserve the external state
directory.

## Builder prerequisites

These are required only on the release machine: Python 3.11+, `uv`, Node/npm,
and Rustup with Rust 1.88. The recipient needs none of them. A production runtime
build requires network access to download the versions pinned in
`tools/release/runtime-spec.json`; the smoke test itself is offline and makes no Dola
request.

From the repository root:

```bash
python tools/release/check_release.py
pytest -q

# Apple Silicon macOS
python tools/release/build_runtime.py \
  --target macos-arm64 --output dist/desktop-runtime/macos-arm64 --force --smoke
python tools/release/build_installer.py --target macos-arm64

# x64 Windows PowerShell
python tools/release/build_runtime.py `
  --target windows-x64 --output dist/desktop-runtime/windows-x64 --force --smoke
python tools/release/build_installer.py --target windows-x64
```

## GitHub Actions dual-native release

`.github/workflows/release-build.yml` builds macOS ARM64 and Windows x64 in
parallel on native GitHub-hosted runners. It can be started manually or by an
exact `vVERSION` tag. Both jobs use empty Dola credentials and perform only
offline smoke checks; no live generation is part of CI or release.

Each native job uploads its canonical installer, checksum, and metadata. A
dependent job then requires exactly one DMG and one EXE at the same version,
recomputes both hashes, and creates `release-manifest.json` plus combined
`SHA256SUMS`. The complete bundle is retained as a 14-day workflow artifact.

Version tags call `.github/workflows/release-publish.yml` only after that combined
gate passes, and RC tags are marked as prereleases. A manual run can publish an
existing tag by supplying `publish_tag`. A portable Windows build is not part
of the supported release surface.

`build_runtime.py` permits a tiny `--fixture` tree only for offline tests.
`build_installer.py` always rejects fixture runtimes. The installer builder stages
a clean shell copy under ignored `dist/desktop-build/`, injects the verified
runtime, runs the exact Tauri CLI in `desktop/package-lock.json`, and writes the
artifacts, SHA-256 files, and canonical `.metadata.json` records under
`dist/desktop-installers/<target>/`.

On macOS, ad-hoc signing changes Mach-O bytes inside the embedded Python and
Chromium trees. The builder therefore validates all runtime provenance before
bundling, confirms the embedded manifest and Python application payload are
unchanged afterward, then verifies the app's deep code seal and the DMG
filesystem checksum. The target metadata records that boundary explicitly.

## Runtime and security boundary

The Tauri process chooses an ephemeral `127.0.0.1` port and a fresh launch
capability, starts the bundled Python entry point, checks an instance-specific
readiness response, then navigates its webview to the existing FastAPI UI. A
second app launch focuses the first window. Closing the window requests graceful
shutdown; bounded process-tree termination is the fallback.

Desktop mode rejects non-loopback binds, untrusted Host headers, and foreign
browser origins. The owner UI uses the launch capability without placing it in
local or session storage, and strips it from the URL after bootstrap. Detailed
health, owner APIs, media bytes, and shutdown are authenticated. The legacy
public `/videos` mount is disabled. External local API access is off unless a
developer supplies a fixed `DOLA_DESKTOP_API_PORT`; enabling it still requires a
configured client key.

Mutable state lives in Tauri's OS app-data directory for
`com.dolagateway.desktop`; logs use its OS app-log directory. That state includes
profiles, databases, private configuration, generated media, and support
archives. It is never bundled and survives a manual app replacement. On POSIX,
desktop state directories and sensitive files are tightened to owner-only modes.

The in-app support action creates a scrubbed ZIP with platform/version metadata,
aggregate inventory, and bounded redacted log tails. It excludes profiles,
databases, media, prompts stored in databases, cookies, local paths, emails, and
known credentials.

## Acceptance checklist

On a clean machine with no Python, Node, Rust, or Chrome installed:

1. Verify the installer checksum and install for the current user.
2. Start the app twice; confirm the second launch focuses the existing window.
3. Confirm the startup view reaches the app with no terminal. On Windows without
   WebView2, allow its installer-time download; if installing offline, provision
   WebView2 first.
4. Add a fresh account and complete login in the visible bundled Chromium.
5. Restart the app and confirm account metadata and generation history persist.
6. Confirm a bad Host/Origin request is rejected and media requires auth.
7. Create a support bundle and inspect it for profiles, databases, media,
   credentials, emails, prompts, and absolute home/state paths.
8. Quit during idle and active browser states; confirm the backend and Chromium
   process tree exits.
9. Replace the app with the same or newer manual build and confirm state remains.

Do not use a real Dola generation merely as an installer smoke test. Account and
generation acceptance is a deliberate manual test by the recipient.

## Signing

Current builds are unsigned for distribution. macOS uses an ad-hoc signature
only for bundle consistency and is not notarized; Windows NSIS output is not
Authenticode signed. Trusted users may need to use the operating system's manual
override. Public distribution requires Developer ID/notarization and
Authenticode signing outside this scope.
