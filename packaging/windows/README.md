# Windows packaging

This directory contains the Windows-first delivery tooling for Dola Image/Video
Gateway.

The intended output is an OS-specific ZIP that a recipient can extract and set
up without receiving any of the developer machine's credentials or runtime
state.

## Release layout

```text
DolaGateway-<version>-windows-x64/
  app/                 Clean application source
  launcher/            Setup, run, stop, backup, support, and doctor commands
  QUICKSTART.md
  VERSION
  CHECKSUMS.txt
```

Mutable state must be created on the recipient's computer under:

```text
%LOCALAPPDATA%\DolaGateway\
```

That directory will hold configuration, account browser profiles, SQLite
databases, generated media, and logs. It must never be copied into a release
ZIP.

## Safety boundary

`release-files.txt` is an allowlist. The Windows package builder should copy
only the listed paths rather than copying the repository and trying to remove
sensitive files afterward.

The following local paths must never be packaged:

- `.env.local`
- `accounts/`
- `accounts.local.json`
- `cookies.txt`
- `*.db`, SQLite journals, WAL, and SHM files
- `downloads/`
- `*.log` and debug captures
- `.venv/` and `.venv-moved-backup/`
- locally installed Chromium/browser caches
- `.git/`, `.pytest_cache/`, `__pycache__/`, and `*.pyc`

## Implemented in the first package

1. A deterministic Python package builder stages only the allowlisted files.
2. `setup.cmd`/`setup.ps1` create a private Python environment and install
   Patchright Chromium.
3. Localhost-only run/stop commands start the service and perform a health check.
4. Mutable paths live under `%LOCALAPPDATA%\DolaGateway`.
5. Diagnostics include an offline Chromium launch check.
6. Backup commands omit browser profiles by default and require the service to
   be stopped.

`Dola Gateway.cmd` is the normal entry point. On first launch it runs setup, then
starts the localhost service and opens `/`. Later launches reuse the private
runtime and configuration. `run.cmd` and `setup.cmd` remain explicit maintenance
entry points.

`create-support-bundle.cmd` is safe to share: it excludes credentials, browser
profiles, databases, generated media, and log contents. In contrast, both backup
commands produce private archives that must never be sent to support.

The version comes only from the repository-root `VERSION` file. Build and verify
with `python packaging/build_release.py windows` from the repository root.

## Remaining release validation

- Exercise setup, run, login, generation, stop, and backup on a clean Windows
  10 or 11 x64 virtual machine.
- Add a signed automatic update/rollback flow after the first manual release.
- Sign the Windows launcher/package before any public distribution. Current ZIPs
  are intentionally unsigned developer builds.
