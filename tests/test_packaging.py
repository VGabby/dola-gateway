from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = PROJECT_ROOT / "tools" / "release"
RUNTIME_BUILDER = RELEASE_DIR / "build_runtime.py"


def load_runtime_builder():
    spec = importlib.util.spec_from_file_location("dola_runtime_builder", RUNTIME_BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_inputs_pass_offline_validation():
    result = subprocess.run(
        [sys.executable, "tools/release/check_release.py"],
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

    assert {
        Path("src/dola_gateway/VERSION"),
        Path("src/dola_gateway/app_version.py"),
        Path("src/dola_gateway/web/playground.html"),
    } <= items
    assert Path("src/dola_gateway/account_config.py") not in items
    assert Path("src/dola_gateway/web/index.html") not in items


def test_extension_has_least_privilege_and_only_dola_duration_assets():
    extension = PROJECT_ROOT / "src" / "dola_gateway" / "extensions" / "dola30"
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["permissions"] == ["debugger", "tabs"]
    assert manifest["host_permissions"] == ["*://*.dola.com/*"]
    assert "content_scripts" not in manifest
    files = {item.name for item in extension.iterdir() if item.is_file()}
    assert files == {"README.md", "manifest.json", "service-worker.js", "duration-patch.js"}
    source = "\n".join(
        (extension / name).read_text(encoding="utf-8")
        for name in ("service-worker.js", "duration-patch.js")
    ).lower()
    assert "doubao" not in source
    assert "downloads" not in source
    assert "media_found" not in source


def test_extension_duration_patch_runs_offline_for_10_15_and_30_seconds():
    helper = (
        PROJECT_ROOT / "src" / "dola_gateway" / "extensions" / "dola30" / "duration-patch.js"
    )
    script = r"""
const patch = require(process.argv[1]);
const nested = JSON.stringify({
  data: {
    selector: JSON.stringify({value: "duration", options: [{show_name: "10s", value: "10"}]}),
    capability: {supported_durations: ["10"]}
  }
});
const result = JSON.parse(patch.patchDurationPayload(nested));
const selector = JSON.parse(result.data.selector);
if (selector.options.map(item => item.value).join(",") !== "10,15,30") process.exit(2);
if (result.data.capability.supported_durations.join(",") !== "10,15,30") process.exit(3);
if (patch.patchDurationPayload("not-json") !== "not-json") process.exit(4);
"""
    result = subprocess.run(
        ["node", "-e", script, str(helper)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_development_requirements_are_available_to_fresh_clones():
    requirements = PROJECT_ROOT / "requirements-dev.txt"
    assert requirements.read_text(encoding="utf-8").splitlines() == [
        "-r requirements.txt",
        "pytest",
        "pytest-socket",
        "httpx2",
    ]
    assert "!requirements-dev.txt" in (PROJECT_ROOT / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert "requirements-dev.txt" in (PROJECT_ROOT / "README.md").read_text(
        encoding="utf-8"
    )


def test_ci_runs_the_full_suite_without_credentials_or_external_python_network():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "runs-on: macos-15" in workflow
    assert 'DOLA_API_KEYS: ""' in workflow
    assert 'DOLA_ADMIN_KEY: ""' in workflow
    assert "uv sync --frozen --group dev" in workflow
    assert "uv run --frozen pytest -q" in workflow
    assert "tools/release/check_release.py" in workflow
    assert "cargo test --locked" in workflow


def test_legacy_bootstrap_packaging_is_absent():
    assert not (PROJECT_ROOT / "packaging").exists()
    assert not (RELEASE_DIR / "build_release.py").exists()
    assert not (RELEASE_DIR / "release_common.py").exists()
