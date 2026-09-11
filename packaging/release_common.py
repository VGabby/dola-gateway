"""Shared, dependency-free release validation helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z]+)*$")
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    ".venv-moved-backup",
    "__pycache__",
    "accounts",
    "downloads",
}
FORBIDDEN_NAMES = {
    ".env.local",
    "accounts.local.json",
    "cookies.txt",
    ".ds_store",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pma",
    ".pyc",
}

# These patterns are intentionally narrow. They catch high-confidence leaked
# credentials without rejecting documentation, placeholder values, or code that
# generates a key on the recipient's computer.
SENSITIVE_TEXT_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Google session cookie": re.compile(r"(?:__Secure-[13]P?SID|SAPISID)=[^\s;]{12,}"),
    "signed cloud URL": re.compile(r"[?&](?:X-Amz-Signature|Signature)=[0-9A-Fa-f]{24,}"),
    "hard-coded Dola credential": re.compile(
        r"(?mi)^\s*DOLA_(?:API_KEYS|ADMIN_KEY)\s*=\s*"
        r"(?!replace-|\$|%|\{)[A-Za-z0-9_.:-]{20,}\s*$"
    ),
    "hard-coded bearer token": re.compile(
        r"(?i)Authorization\s*:\s*Bearer\s+[A-Za-z0-9_.-]{24,}"
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_version(project_root: Path) -> str:
    """Read and validate the one canonical product version."""
    version_file = project_root / "VERSION"
    value = version_file.read_text(encoding="utf-8").strip()
    if not VERSION_RE.fullmatch(value):
        raise ValueError(f"invalid canonical version in {version_file}: {value!r}")
    return value


def validate_release_path(relative: Path) -> None:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe release path: {relative}")
    lowered = {part.lower() for part in relative.parts}
    if lowered.intersection(FORBIDDEN_PARTS):
        raise ValueError(f"forbidden directory in release: {relative}")
    if relative.name.lower() in FORBIDDEN_NAMES:
        raise ValueError(f"forbidden file in release: {relative}")
    lowered_name = relative.name.lower()
    if any(lowered_name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise ValueError(f"forbidden file type in release: {relative}")


def load_allowlist(project_root: Path, allowlist_path: Path) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in allowlist_path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        relative = Path(value)
        validate_release_path(relative)
        normalized = relative.as_posix().lower()
        if normalized in seen:
            raise ValueError(f"duplicate allowlist entry: {value}")
        seen.add(normalized)
        source = project_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"allowlisted file is missing: {value}")
        result.append(relative)
    if not result:
        raise ValueError(f"release allowlist is empty: {allowlist_path}")
    return result


def scan_text(path: Path) -> list[str]:
    if path.stat().st_size > 2 * 1024 * 1024:
        return []
    try:
        body = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return [label for label, pattern in SENSITIVE_TEXT_PATTERNS.items() if pattern.search(body)]


def verify_release_tree(root: Path) -> list[Path]:
    """Reject private state, unsafe symlinks, and high-confidence secret leaks."""
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            # The macOS DMG intentionally links only its top-level Applications
            # shortcut. No application payload may contain arbitrary symlinks.
            if relative.as_posix() == "Applications" and path.readlink() == Path("/Applications"):
                continue
            raise ValueError(f"unexpected symlink in release: {relative}")
        if not path.is_file():
            continue
        validate_release_path(relative)
        leaks = scan_text(path)
        if leaks:
            raise ValueError(f"sensitive content in release file {relative}: {', '.join(leaks)}")
        files.append(relative)
    if not files:
        raise ValueError(f"release tree is empty: {root}")
    return files


def write_checksums(root: Path) -> None:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        relative = path.relative_to(root).as_posix()
        if relative == "CHECKSUMS.txt":
            continue
        rows.append(f"{sha256(path)}  {relative}")
    (root / "CHECKSUMS.txt").write_text(
        "\n".join(rows) + "\n", encoding="utf-8", newline="\n"
    )
