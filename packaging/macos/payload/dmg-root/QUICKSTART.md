# Dola Gateway for macOS

This is an unsigned local-use build for macOS 12 or newer.

## First use

1. Confirm that the DMG matches your Mac:
   - arm64: Apple Silicon (M1, M2, M3, M4, or newer)
   - x86_64: Intel Mac
2. Drag Dola Gateway.app to the Applications shortcut.
3. Control-click Setup Dola Gateway.command, choose Open, then confirm the
   macOS warning.
4. Setup downloads a private Python runtime, dependencies, and Patchright
   Chromium. It does not require Homebrew, Xcode, or administrator access.
5. Save the generated admin and client keys in a password manager.
6. When setup finishes, the local dashboard opens automatically.
7. Enter the admin key, open Accounts, add a profile, and complete Google
   sign-in in the Chromium window.

If macOS blocks the app, Control-click Dola Gateway.app in Applications, choose
Open, and confirm the warning. Do not disable Gatekeeper.

## Daily use

- Open Dola Gateway from Applications. It starts the local service if necessary
  and opens the dashboard.
- Use the authenticated Stop button in the dashboard before backups or updates.
- Show Dola Credentials displays the saved keys.
- Diagnose Dola Gateway checks the private runtime and Chromium.
- Create Support Bundle creates a redacted ZIP that is safe to share with
  support. It excludes keys, profiles, databases, media, and log contents.
- Normal backups omit login profiles. The separate profile backup contains
  reusable sessions and must never be shared.

Setup requires internet access and approximately 1 GB of free disk space.
The gateway listens only on 127.0.0.1 and must not be exposed to the internet.
