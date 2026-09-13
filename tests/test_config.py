"""Configuration defaults and explicit environment overrides."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _image_headless_value(tmp_path, *, desktop: bool, override: str | None) -> str:
    env = os.environ.copy()
    env["DOLA_ENV_FILE"] = str(tmp_path / "missing.env")
    if desktop:
        env["DOLA_DESKTOP_TOKEN"] = "test-desktop-token"
    else:
        env.pop("DOLA_DESKTOP_TOKEN", None)
    if override is None:
        env.pop("DOLA_IMAGE_HEADLESS", None)
    else:
        env["DOLA_IMAGE_HEADLESS"] = override
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dola_gateway import config; print(int(config.IMAGE_HEADLESS))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("desktop", [False, True])
def test_image_browser_is_headed_by_default_in_all_modes(tmp_path, desktop):
    assert _image_headless_value(tmp_path, desktop=desktop, override=None) == "0"


def test_image_browser_can_still_be_explicitly_headless(tmp_path):
    assert _image_headless_value(tmp_path, desktop=False, override="1") == "1"
