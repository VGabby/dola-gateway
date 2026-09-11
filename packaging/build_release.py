#!/usr/bin/env python3
"""Single entry point for checking and building friend-delivery packages."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGING_DIR.parent


def run(*parts: str) -> None:
    subprocess.run([sys.executable, *parts], cwd=PROJECT_ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "windows", "macos", "all"))
    parser.add_argument("--target", choices=("arm64", "x86_64"), help="macOS architecture")
    args = parser.parse_args()

    run("packaging/check_release.py")
    if args.command == "check":
        return
    if args.command in {"windows", "all"}:
        run("packaging/windows/build_release.py")
    if args.command in {"macos", "all"}:
        if platform.system() != "Darwin":
            raise SystemExit("macOS DMGs must be built on macOS")
        command = ["packaging/macos/build_release.py"]
        if args.target:
            command.extend(("--target", args.target))
        run(*command)


if __name__ == "__main__":
    main()
