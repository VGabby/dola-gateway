from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = PROJECT_ROOT / "packaging"


def load_release_common():
    path = PACKAGING_DIR / "release_common.py"
    spec = importlib.util.spec_from_file_location("dola_release_common", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_inputs_pass_offline_leak_check():
    result = subprocess.run(
        [sys.executable, "packaging/check_release.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "leak-free" in result.stdout


def test_platform_builders_share_one_canonical_version():
    common = load_release_common()
    assert common.release_version(PROJECT_ROOT) == (PROJECT_ROOT / "VERSION").read_text().strip()
    assert not (PACKAGING_DIR / "macos" / "VERSION").exists()
    assert not (PACKAGING_DIR / "windows" / "VERSION").exists()


def test_allowlists_ship_runtime_version_reader():
    common = load_release_common()
    expected = {Path("VERSION"), Path("app_version.py")}
    for platform_name in ("macos", "windows"):
        items = set(common.load_allowlist(
            PROJECT_ROOT, PACKAGING_DIR / platform_name / "release-files.txt"
        ))
        assert expected <= items


def test_release_tree_rejects_private_state_and_credentials(tmp_path):
    common = load_release_common()
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "README.md").write_text("placeholder only", encoding="utf-8")
    assert common.verify_release_tree(safe) == [Path("README.md")]

    leaked = tmp_path / "leaked"
    leaked.mkdir()
    (leaked / ".env.local").write_text(
        "DOLA_API_KEYS=test-key-abcdefghijklmnopqrstuvwxyz0123456789\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbidden file"):
        common.verify_release_tree(leaked)


def test_friend_launchers_and_support_tools_are_packaged():
    assert (PACKAGING_DIR / "macos/payload/app-template/Contents/MacOS/dola-gateway").is_file()
    assert (PACKAGING_DIR / "macos/payload/dmg-root/Create Support Bundle.command").is_file()
    assert (PACKAGING_DIR / "windows/payload/Dola Gateway.cmd").is_file()
    assert (PACKAGING_DIR / "windows/payload/create-support-bundle.cmd").is_file()
