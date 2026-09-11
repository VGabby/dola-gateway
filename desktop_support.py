"""Privacy-conscious diagnostics for the trusted-friend desktop application."""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path


_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_COOKIE_RE = re.compile(
    r"(?i)((?:cookie|sessionid|SAPISID|__Secure-[13]P?SID)\s*[=:]\s*)[^\s;,]+"
)
_DOLA_SETTING_RE = re.compile(r"(?im)^(\s*DOLA_[A-Z0-9_]+\s*=).*$")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_./:+~-]{24,}\b")
_SAFE_LOG_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _directory_inventory(path: Path) -> dict[str, int | bool]:
    """Return aggregate counts without exposing private child names."""
    if not path.is_dir():
        return {"exists": False, "files": 0, "bytes": 0}
    files = 0
    total = 0
    for root, directories, names in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(root) / name).is_symlink()
        ]
        for name in names:
            candidate = Path(root) / name
            if candidate.is_symlink():
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            files += 1
            total += size
    return {"exists": True, "files": files, "bytes": total}


def _file_inventory(path: Path) -> dict[str, int | bool]:
    try:
        return {"exists": path.is_file(), "bytes": path.stat().st_size}
    except OSError:
        return {"exists": False, "bytes": 0}


def scrub_log_text(
    value: str,
    *,
    state_dir: Path,
    secrets: tuple[str, ...] = (),
) -> str:
    """Aggressively remove common credentials and identifying local paths."""
    text = value
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "<secret-redacted>")
    replacements = {str(state_dir.resolve()): "<STATE>"}
    try:
        replacements[str(Path.home().resolve())] = "<HOME>"
    except OSError:
        pass
    for source, replacement in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        text = text.replace(source, replacement)
    text = _DOLA_SETTING_RE.sub(r"\1<redacted>", text)
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    text = _COOKIE_RE.sub(r"\1<redacted>", text)
    text = _EMAIL_RE.sub("<email-redacted>", text)
    text = _URL_QUERY_RE.sub(r"\1?<query-redacted>", text)
    text = _LONG_TOKEN_RE.sub("<token-redacted>", text)
    return text


def _tail(path: Path, max_bytes: int = 64 * 1024) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        body = stream.read(max_bytes)
    return body.decode("utf-8", errors="replace")


def create_support_bundle(
    *,
    state_dir: Path,
    output_dir: Path,
    log_dir: Path,
    version: str,
    instance_id: str,
    db_path: Path,
    pool_db_path: Path,
    accounts_dir: Path,
    downloads_dir: Path,
    secrets: tuple[str, ...] = (),
) -> Path:
    """Create a shareable ZIP containing diagnostics but no private payloads."""
    state_dir = state_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        output_dir.chmod(0o700)

    created_at = datetime.now(timezone.utc)
    bundle_name = (
        f"DolaGateway-Support-{created_at:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.zip"
    )
    destination = output_dir / bundle_name
    diagnostics = {
        "created_at": created_at.isoformat(),
        "version": version,
        "instance_id": instance_id,
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "executable_kind": Path(sys.executable).name,
    }
    inventory = {
        "browser_profiles": _directory_inventory(accounts_dir),
        "generated_media": _directory_inventory(downloads_dir),
        "logs": _directory_inventory(log_dir),
        "task_database": _file_inventory(db_path),
        "pool_database": _file_inventory(pool_db_path),
        "note": "Only aggregate counts and sizes are included; private files are excluded.",
    }

    temporary = tempfile.NamedTemporaryFile(
        prefix=".support-", suffix=".zip", dir=output_dir, delete=False
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with zipfile.ZipFile(
            temporary_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "diagnostics.json",
                json.dumps(diagnostics, indent=2, sort_keys=True) + "\n",
            )
            archive.writestr(
                "inventory.json",
                json.dumps(inventory, indent=2, sort_keys=True) + "\n",
            )
            if log_dir.is_dir():
                used_names: set[str] = set()
                for index, log_path in enumerate(sorted(log_dir.glob("*.log"))):
                    if not log_path.is_file() or log_path.is_symlink():
                        continue
                    safe_name = _SAFE_LOG_NAME_RE.sub("_", log_path.name).strip("._")
                    safe_name = safe_name or f"log-{index + 1}"
                    while safe_name in used_names:
                        safe_name = f"{index + 1}-{safe_name}"
                    used_names.add(safe_name)
                    scrubbed = scrub_log_text(
                        _tail(log_path), state_dir=state_dir, secrets=secrets
                    )
                    archive.writestr(f"log-tails/{safe_name}.tail.txt", scrubbed)
        temporary_path.replace(destination)
        if os.name == "posix":
            destination.chmod(0o600)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)
