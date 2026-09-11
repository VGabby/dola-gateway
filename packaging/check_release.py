#!/usr/bin/env python3
"""Offline checks for release inputs and Windows ZIP artifacts."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

from release_common import (
    load_allowlist,
    release_version,
    sha256,
    verify_release_tree,
)


PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent


def verify_inputs() -> None:
    version = release_version(PROJECT_ROOT)
    with tempfile.TemporaryDirectory(prefix="dola-release-inputs-") as temp:
        for platform in ("macos", "windows"):
            platform_dir = PACKAGING_DIR / platform
            allowlist = load_allowlist(PROJECT_ROOT, platform_dir / "release-files.txt")
            for relative in allowlist:
                leaks = verify_release_tree_for_file(PROJECT_ROOT / relative, relative)
                if leaks:
                    raise ValueError(leaks)
            filtered_payload = Path(temp) / platform
            shutil.copytree(
                platform_dir / "payload",
                filtered_payload,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
            verify_release_tree(filtered_payload)
    print(f"[OK] release inputs are allowlisted and leak-free (version {version})")


def verify_release_tree_for_file(path: Path, relative: Path) -> str | None:
    """Use the tree scanner's text policy for a single allowlisted source file."""
    from release_common import scan_text

    leaks = scan_text(path)
    if leaks:
        return f"sensitive content in allowlisted file {relative}: {', '.join(leaks)}"
    return None


def verify_zip(path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="dola-release-check-") as temp:
        destination = Path(temp)
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                relative = Path(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"unsafe archive member: {member.filename}")
            archive.extractall(destination)
        roots = [item for item in destination.iterdir() if item.is_dir()]
        if len(roots) != 1:
            raise ValueError("release ZIP must contain exactly one top-level directory")
        root = roots[0]
        verify_release_tree(root)
        checksums = root / "CHECKSUMS.txt"
        if not checksums.is_file():
            raise ValueError("release ZIP does not contain CHECKSUMS.txt")
        for row in checksums.read_text(encoding="utf-8").splitlines():
            digest, name = row.split("  ", 1)
            member = root / name
            if not member.is_file() or sha256(member) != digest:
                raise ValueError(f"checksum mismatch in release ZIP: {name}")
    print(f"[OK] verified release ZIP: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", type=Path, help="Windows ZIPs to verify")
    args = parser.parse_args()
    verify_inputs()
    for artifact in args.artifacts:
        if artifact.suffix.lower() != ".zip":
            raise ValueError(f"unsupported artifact type for offline inspection: {artifact}")
        verify_zip(artifact)


if __name__ == "__main__":
    main()
