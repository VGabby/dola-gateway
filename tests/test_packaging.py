from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = PROJECT_ROOT / "packaging"
RUNTIME_BUILDER = PACKAGING_DIR / "desktop" / "build_runtime.py"


def load_runtime_builder():
    spec = importlib.util.spec_from_file_location("dola_runtime_builder", RUNTIME_BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_inputs_pass_offline_validation():
    result = subprocess.run(
        [sys.executable, "packaging/check_release.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "version-aligned, and leak-free" in result.stdout


def test_desktop_allowlist_contains_only_the_current_application_surface():
    builder = load_runtime_builder()
    items = set(builder.load_allowlist())

    assert {Path("VERSION"), Path("app_version.py"), Path("web/playground.html")} <= items
    assert Path("account_config.py") not in items
    assert Path("web/index.html") not in items


def test_extension_fixtures_are_utf8_json():
    extension = PROJECT_ROOT / "extensions" / "dola30"
    for name in ("dola-skill-pack-response.json", "doubao-skill-pack-response.json"):
        payload = json.loads((extension / name).read_text(encoding="utf-8"))
        assert payload["code"] == 0


def test_development_requirements_are_available_to_fresh_clones():
    requirements = PROJECT_ROOT / "requirements-dev.txt"
    assert requirements.read_text(encoding="utf-8").splitlines() == [
        "-r requirements.txt",
        "pytest",
        "httpx",
    ]
    assert "!requirements-dev.txt" in (PROJECT_ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert "requirements-dev.txt" in (PROJECT_ROOT / "README.md").read_text(
        encoding="utf-8"
    )


def test_legacy_bootstrap_packaging_is_absent():
    assert not (PACKAGING_DIR / "build_release.py").exists()
    assert not (PACKAGING_DIR / "release_common.py").exists()
    assert not (PACKAGING_DIR / "macos").exists()
    assert not (PACKAGING_DIR / "windows").exists()
