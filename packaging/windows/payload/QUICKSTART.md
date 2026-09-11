# Dola Gateway for Windows

This package is intended for Windows 10 or 11 on an x64 computer. It runs only
on this computer (`127.0.0.1`) by default.

## Install

1. Install 64-bit Python 3.11 or newer from <https://www.python.org/downloads/windows/>.
   Setup recognizes the Windows Python launcher, `python.exe` on `PATH`, and
   standard per-user Python installations.
2. Double-click `Dola Gateway.cmd`. It runs setup automatically on first use.
3. Setup downloads Python packages and Patchright Chromium. This can take
   several minutes and requires an internet connection.
4. Keep the generated admin and client keys private. They are stored in
   `%LOCALAPPDATA%\DolaGateway\.env.local`.

## Run

1. Double-click `Dola Gateway.cmd`.
2. The dashboard opens at <http://127.0.0.1:8000/>.
3. When asked for the admin key, copy `DOLA_ADMIN_KEY` from the private config
   file. `open-data-folder.cmd` opens its folder.
4. Open the Accounts tab, add a profile, and complete Google sign-in in the
   Chromium window. Use your own Dola account; never import another person's
   browser profile or cookies.

If Dola requires an allowed regional proxy, stop the gateway and set
`DOLA_PROXY` in `.env.local` before starting it again.

## Maintenance

- `stop.cmd` stops the local gateway.
- `doctor.cmd` checks the installation and reports whether the server is healthy.
- `create-support-bundle.cmd` creates a redacted ZIP that is safe to share with
  support. It never includes keys, profiles, databases, media, or log contents.
- `backup.cmd` creates a local backup without browser login profiles.
- `backup-with-profiles.cmd` also includes login profiles. Treat that backup
  like a password-vault export.
- Running `setup.cmd` again repairs dependencies without deleting user data.

Generated data, profiles, databases, configuration, and logs live under
`%LOCALAPPDATA%\DolaGateway`, outside this extracted application folder.

## Important security limits

- Do not change the host from `127.0.0.1` or forward port 8000 to the internet.
- Do not share `.env.local`, databases, backups, logs, downloads, or `accounts/`.
- Backups contain sensitive prompts, history, media, and possibly API keys.
- Share the support bundle, never a backup, when troubleshooting with someone else.
- This is browser automation software for private/internal use. The user is
  responsible for complying with Dola, Google, and applicable service terms.
