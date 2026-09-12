#!/usr/bin/env python3
"""Validate source inputs and version/tag parity for desktop releases."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


RELEASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RELEASE_DIR.parents[1]
sys.path.insert(0, str(RELEASE_DIR))

from build_runtime import load_spec, verify_application_inputs  # noqa: E402
from changelog import extract_release_notes  # noqa: E402
from versioning import validate_version_alignment  # noqa: E402


def verify_inputs(*, tag: str | None = None) -> None:
    load_spec()
    application_files = verify_application_inputs()
    canonical = validate_version_alignment(PROJECT_ROOT)
    extract_release_notes(canonical)
    if tag is not None and tag != f"v{canonical}":
        raise ValueError(f"release tag {tag!r} does not match VERSION v{canonical}")
    print(
        f"[OK] {len(application_files)} desktop application inputs are UTF-8, "
        f"valid, version-aligned, and leak-free (version {canonical})"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="require exact vVERSION tag parity")
    args = parser.parse_args()
    verify_inputs(tag=args.tag)
