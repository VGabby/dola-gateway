#!/usr/bin/env python3
"""Validate the source inputs for self-contained desktop releases."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent
DESKTOP_PACKAGING_DIR = PACKAGING_DIR / "desktop"
sys.path.insert(0, str(DESKTOP_PACKAGING_DIR))

from build_runtime import load_spec, verify_application_inputs  # noqa: E402


VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-.][0-9A-Za-z]+)*$")


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path.relative_to(PROJECT_ROOT)}")
    return value


def _release_versions() -> dict[str, str]:
    package = _json(PROJECT_ROOT / "desktop" / "package.json")
    package_lock = _json(PROJECT_ROOT / "desktop" / "package-lock.json")
    tauri = _json(PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.conf.json")
    cargo = tomllib.loads(
        (PROJECT_ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    )
    cargo_lock = tomllib.loads(
        (PROJECT_ROOT / "desktop" / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")
    )
    locked_shell = next(
        item for item in cargo_lock["package"] if item.get("name") == "dola-gateway-desktop"
    )
    return {
        "VERSION": (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "desktop/package.json": str(package["version"]),
        "desktop/package-lock.json": str(package_lock["version"]),
        "desktop/package-lock.json root": str(package_lock["packages"][""]["version"]),
        "desktop/src-tauri/tauri.conf.json": str(tauri["version"]),
        "desktop/src-tauri/Cargo.toml": str(cargo["package"]["version"]),
        "desktop/src-tauri/Cargo.lock": str(locked_shell["version"]),
    }


def verify_inputs() -> None:
    load_spec()
    application_files = verify_application_inputs()
    versions = _release_versions()
    canonical = versions["VERSION"]
    if not VERSION_RE.fullmatch(canonical):
        raise ValueError(f"invalid release version: {canonical!r}")
    mismatches = {name: value for name, value in versions.items() if value != canonical}
    if mismatches:
        details = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise ValueError(f"desktop release versions do not match VERSION {canonical}: {details}")
    print(
        f"[OK] {len(application_files)} desktop application inputs are UTF-8, "
        f"valid, version-aligned, and leak-free (version {canonical})"
    )


if __name__ == "__main__":
    verify_inputs()
