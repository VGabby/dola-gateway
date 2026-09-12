"""Application version loaded from the packaged VERSION file."""

from __future__ import annotations

from pathlib import Path


VERSION_FILE = Path(__file__).resolve().with_name("VERSION")


def _read_version() -> str:
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0+unknown"
    return value or "0+unknown"


APP_VERSION = _read_version()
