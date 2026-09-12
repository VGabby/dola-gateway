"""Shared strict SemVer and release-version propagation helpers."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


SEMVER_PATTERN = (
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
SEMVER_RE = re.compile(rf"^{SEMVER_PATTERN}$")


def require_semver(value: str) -> str:
    if not SEMVER_RE.fullmatch(value):
        raise ValueError(f"invalid strict SemVer: {value!r}")
    return value


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def collect_versions(root: Path) -> dict[str, str]:
    package = _json(root / "desktop" / "package.json")
    package_lock = _json(root / "desktop" / "package-lock.json")
    tauri = _json(root / "desktop" / "src-tauri" / "tauri.conf.json")
    cargo = tomllib.loads(
        (root / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    )
    cargo_lock = tomllib.loads(
        (root / "desktop" / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")
    )
    locked_shell = next(
        item for item in cargo_lock["package"]
        if item.get("name") == "dola-gateway-desktop"
    )
    return {
        "VERSION": (root / "VERSION").read_text(encoding="utf-8").strip(),
        "src/dola_gateway/VERSION": (
            root / "src" / "dola_gateway" / "VERSION"
        ).read_text(encoding="utf-8").strip(),
        "desktop/package.json": str(package["version"]),
        "desktop/package-lock.json": str(package_lock["version"]),
        "desktop/package-lock.json root": str(package_lock["packages"][""]["version"]),
        "desktop/src-tauri/tauri.conf.json": str(tauri["version"]),
        "desktop/src-tauri/Cargo.toml": str(cargo["package"]["version"]),
        "desktop/src-tauri/Cargo.lock": str(locked_shell["version"]),
    }


def validate_version_alignment(root: Path) -> str:
    versions = collect_versions(root)
    canonical = require_semver(versions["VERSION"])
    mismatches = {name: value for name, value in versions.items() if value != canonical}
    if mismatches:
        details = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise ValueError(f"desktop release versions do not match VERSION {canonical}: {details}")
    return canonical
