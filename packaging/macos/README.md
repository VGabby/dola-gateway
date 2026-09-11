# macOS packaging

This directory builds two unsigned, local-use disk images:

- DolaGateway-version-macos-arm64.dmg for Apple Silicon
- DolaGateway-version-macos-x86_64.dmg for Intel Macs

Persistent data is stored in Library/Application Support/DolaGateway inside the
user home directory. Disposable runtime components are stored in the matching
Library/Caches directory, and logs are stored in Library/Logs.

The builder uses an explicit source allowlist and never includes local profiles,
credentials, databases, generated media, logs, virtual environments, or browser
caches.

Opening `Dola Gateway.app` is the normal one-click entry point. First launch opens
the setup flow; later launches start the localhost service and open `/`. The
focused prompt workspace remains available at `/playground`.

## Build

Run:

    python packaging/build_release.py macos

Use `--target arm64` or `--target x86_64` to build one disk image. Both outputs
read their version from the repository-root `VERSION` file and run release leak
checks before the DMG is created.

The application uses an ad-hoc signature but is not Developer ID signed or
notarized. A recipient must Control-click the setup command and application and
choose Open on first use.

“Create Support Bundle” produces a redacted archive that is safe to send for
troubleshooting. Backup archives contain private keys, history, and media; profile
backups also contain reusable sessions. Never send either backup to support.
