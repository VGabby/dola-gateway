#!/usr/bin/env python3
"""Propagate one strict SemVer through every desktop release version file."""

from __future__ import annotations

import argparse
import difflib
import json
import re
from datetime import date
from pathlib import Path

from versioning import require_semver, validate_version_alignment


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _json_text(value: dict) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _replace_package_version(body: str, package_name: str, version: str) -> str:
    pattern = re.compile(
        rf'(\[\[package\]\]\nname = "{re.escape(package_name)}"\nversion = ")[^"]+("\n)'
    )
    updated, count = pattern.subn(rf"\g<1>{version}\g<2>", body, count=1)
    if count != 1:
        raise ValueError(f"could not find {package_name} in Cargo.lock")
    return updated


def planned_updates(version: str, root: Path = PROJECT_ROOT, *, release_date: str) -> dict[Path, str]:
    require_semver(version)
    date.fromisoformat(release_date)
    updates: dict[Path, str] = {
        root / "VERSION": version + "\n",
        root / "src" / "dola_gateway" / "VERSION": version + "\n",
    }

    package_path = root / "desktop" / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["version"] = version
    updates[package_path] = _json_text(package)

    lock_path = root / "desktop" / "package-lock.json"
    package_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    package_lock["version"] = version
    package_lock["packages"][""]["version"] = version
    updates[lock_path] = _json_text(package_lock)

    tauri_path = root / "desktop" / "src-tauri" / "tauri.conf.json"
    tauri = json.loads(tauri_path.read_text(encoding="utf-8"))
    tauri["version"] = version
    updates[tauri_path] = _json_text(tauri)

    cargo_path = root / "desktop" / "src-tauri" / "Cargo.toml"
    cargo_body = cargo_path.read_text(encoding="utf-8")
    cargo_body, count = re.subn(
        r'(?m)^(version = ")[^"]+("\s*)$', rf"\g<1>{version}\g<2>", cargo_body, count=1
    )
    if count != 1:
        raise ValueError("could not update Cargo.toml package version")
    updates[cargo_path] = cargo_body

    cargo_lock_path = root / "desktop" / "src-tauri" / "Cargo.lock"
    updates[cargo_lock_path] = _replace_package_version(
        cargo_lock_path.read_text(encoding="utf-8"), "dola-gateway-desktop", version
    )

    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
        marker = "## [Unreleased]\n"
        if marker not in changelog:
            raise ValueError("CHANGELOG.md is missing an [Unreleased] section")
        insertion = f"\n## [{version}] - {release_date}\n\n- Release notes pending.\n"
        changelog = changelog.replace(marker, marker + insertion, 1)
    updates[changelog_path] = changelog
    return updates


def render_diff(updates: dict[Path, str], root: Path = PROJECT_ROOT) -> str:
    chunks: list[str] = []
    for path in sorted(updates, key=lambda item: item.relative_to(root).as_posix()):
        before = path.read_text(encoding="utf-8")
        after = updates[path]
        if before == after:
            continue
        relative = path.relative_to(root).as_posix()
        chunks.extend(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        ))
    return "".join(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    updates = planned_updates(args.version, release_date=args.date)
    diff = render_diff(updates)
    if args.dry_run:
        print(diff, end="")
        return
    for path, content in updates.items():
        path.write_text(content, encoding="utf-8")
    validate_version_alignment(PROJECT_ROOT)
    print(diff, end="")


if __name__ == "__main__":
    main()
