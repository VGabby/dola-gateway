#!/usr/bin/env python3
"""Extract one version's Markdown release notes from CHANGELOG.md."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from versioning import require_semver


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def extract_release_notes(version: str, path: Path | None = None) -> str:
    require_semver(version)
    path = path or (PROJECT_ROOT / "CHANGELOG.md")
    body = path.read_text(encoding="utf-8")
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s+-\s+\d{{4}}-\d{{2}}-\d{{2}})?\s*$", re.MULTILINE)
    match = heading.search(body)
    if not match:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    start = match.end()
    following = re.search(r"^## \[", body[start:], re.MULTILINE)
    end = start + following.start() if following else len(body)
    notes = body[start:end].strip()
    if not notes or "Release notes pending" in notes:
        raise ValueError(f"CHANGELOG.md section for {version} has no final release notes")
    return notes + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    notes = extract_release_notes(args.version)
    if args.output:
        args.output.write_text(notes, encoding="utf-8")
    else:
        print(notes, end="")


if __name__ == "__main__":
    main()
