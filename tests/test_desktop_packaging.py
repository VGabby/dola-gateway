from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = PROJECT_ROOT / "tools" / "release" / "build_runtime.py"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release-build.yml"
PUBLISH_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release-publish.yml"


def load_builder():
    spec = importlib.util.spec_from_file_location("dola_desktop_runtime_builder", BUILDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_spec_is_exact_and_native_builds_are_restricted():
    builder = load_builder()
    spec = builder.load_spec()
    assert set(spec["targets"]) == {"macos-arm64", "windows-x64"}
    assert spec["python"] == {
        "provider": "uv-managed-python-build-standalone",
        "version": "3.13.7",
    }
    assert spec["patchright"] == {
        "version": "1.62.3",
        "chromium_revision": "1234",
        "chromium_version": "151.0.7922.34",
    }
    assert builder.native_target("Darwin", "arm64", spec=spec) == "macos-arm64"
    assert builder.native_target("Windows", "AMD64", spec=spec) == "windows-x64"
    with pytest.raises(builder.BuildError, match="unsupported build host"):
        builder.native_target("Linux", "x86_64", spec=spec)


def test_runtime_builder_selects_the_fully_pinned_python_install(tmp_path):
    builder = load_builder()
    alias = tmp_path / "cpython-3.13-windows-x86_64-none"
    pinned = tmp_path / "cpython-3.13.7-windows-x86_64-none"
    alias.mkdir()
    pinned.mkdir()

    assert builder._select_one_directory(tmp_path, "cpython-3.13.7-") == pinned


def test_windows_browser_version_check_does_not_launch_the_gui(monkeypatch, tmp_path):
    builder = load_builder()

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("Windows Chromium must not be launched with --version")

    monkeypatch.setattr(builder.subprocess, "run", unexpected_launch)
    builder._verify_reported_browser_version(
        tmp_path / "chrome.exe", "windows-x64", "151.0.7922.34"
    )


def test_windows_installer_is_per_user_and_offline_capable():
    config = json.loads(
        (PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    windows = config["bundle"]["windows"]
    assert windows["nsis"]["installMode"] == "currentUser"
    assert windows["webviewInstallMode"] == {
        "type": "offlineInstaller",
        "silent": True,
    }
    assert windows["allowDowngrades"] is False
    assert config["bundle"]["resources"] == {
        "../runtime-placeholder/": "runtime/"
    }
    placeholder = PROJECT_ROOT / "desktop" / "runtime-placeholder"
    assert (placeholder / "runtime-manifest.json").is_file()


def test_desktop_icon_set_is_declared_and_contains_native_formats():
    config = json.loads(
        (PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {
        "icons/32x32.png",
        "icons/128x128.png",
        "icons/128x128@2x.png",
        "icons/icon.icns",
        "icons/icon.ico",
    }
    assert set(config["bundle"]["icon"]) == expected
    icon_root = PROJECT_ROOT / "desktop" / "src-tauri"
    assert all((icon_root / relative).is_file() for relative in expected)
    assert (icon_root / "icons" / "icon.icns").read_bytes()[:4] == b"icns"
    assert (icon_root / "icons" / "icon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_release_workflows_build_both_native_targets_before_publish():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "runner: windows-2025" in workflow
    assert "runner: macos-15" in workflow
    assert "target: windows-x64" in workflow
    assert "target: macos-arm64" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "tools/release/check_release.py" in workflow
    assert "tools/release/build_runtime.py" in workflow
    assert "--target ${{ matrix.target }}" in workflow
    assert "--smoke" in workflow
    assert "tools/release/build_installer.py" in workflow
    assert "tools/release/create_manifest.py" in workflow
    assert "Run full offline regression suite" in workflow
    assert "uv run --frozen pytest -q" in workflow
    assert "uv run --frozen patchright install chromium" in workflow
    assert "tests/test_packaging.py" not in workflow
    assert "needs: build-native" in workflow
    assert "name: verified-release" in workflow
    assert "inputs.publish_tag != ''" in workflow
    assert "tag: ${{ inputs.publish_tag || github.ref_name }}" in workflow
    assert "actions/upload-artifact@v7" in workflow
    assert "secrets." not in workflow
    assert 'DOLA_API_KEYS: ""' in workflow
    assert 'DOLA_ADMIN_KEY: ""' in workflow
    assert "workflow_call:" in publish
    assert "--verify-only dist/release" in publish
    assert "gh release create" in publish
    assert "gh release upload" in publish
    assert "--prerelease" in publish
    assert "--verify-tag" in publish
    assert not (PROJECT_ROOT / ".github" / "workflows" / "windows-release.yml").exists()


def test_offline_fixture_build_has_verified_contained_manifest(tmp_path):
    builder = load_builder()
    output = tmp_path / "desktop-runtime"
    result = builder.build_runtime("macos-arm64", output, fixture=True)
    assert result == output
    manifest = builder.verify_runtime(output, allow_fixture=True)
    assert manifest["fixture"] is True
    assert manifest["target"] == "macos-arm64"
    assert manifest["app"]["directory"] == "app"
    assert manifest["app"]["entrypoint"] == "dola_gateway.server:app"
    assert manifest["python"]["interpreter"].startswith("python/")
    assert manifest["patchright"]["browser_executable"].startswith("browsers/chromium-1234/")
    assert (output / manifest["python"]["interpreter"]).is_file()
    assert (output / manifest["patchright"]["browser_executable"]).is_file()
    assert (output / "app/dola_gateway/server.py").is_file()
    assert (output / "app/dola_gateway/web/playground.html").is_file()
    assert not (output / ".build").exists()
    assert not list(output.rglob("__pycache__"))
    assert not list(output.rglob("*.pyc"))
    with pytest.raises(builder.BuildError, match="not distributable"):
        builder.verify_runtime(output)

    state_dir = tmp_path / "fixture-state"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(output / "app"),
        "DOLA_STATE_DIR": str(state_dir),
        "DOLA_DESKTOP_TOKEN": "fixture-desktop-token",
        "DOLA_ENV_FILE": str(tmp_path / "missing.env"),
        "DOLA_API_KEYS": "",
        "DOLA_ADMIN_KEY": "",
        "DOLA_PROXY": "",
    })
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "import dola_gateway; "
                "from dola_gateway import server; "
                f"assert Path(dola_gateway.__file__).resolve().is_relative_to(Path({str(output / 'app')!r})); "
                "assert server.app.title == 'Dola Gateway'; "
                "server.store.close(); server.pool.close()"
            ),
        ],
        cwd=output / "app",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr


def test_fixture_cli_is_fast_offline_and_verify_only_works(tmp_path):
    output = tmp_path / "fixture-cli"
    build = subprocess.run(
        [sys.executable, str(BUILDER_PATH), "--fixture", "--target", "windows-x64", "--output", str(output)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    verify = subprocess.run(
        [sys.executable, str(BUILDER_PATH), "--fixture", "--verify-only", str(output)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_manifest_paths_cannot_escape_runtime(tmp_path):
    builder = load_builder()
    output = builder.build_runtime("macos-arm64", tmp_path / "runtime", fixture=True)
    manifest_path = output / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["python"]["interpreter"] = "../outside-python"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(builder.BuildError, match="unsafe Python interpreter"):
        builder.verify_runtime(output, allow_fixture=True)


def test_runtime_verifier_rejects_private_state_credentials_and_escaping_links(tmp_path):
    builder = load_builder()
    output = builder.build_runtime("macos-arm64", tmp_path / "runtime", fixture=True)

    leaked_state = output / "accounts.local.json"
    leaked_state.write_text('{"accounts": []}', encoding="utf-8")
    with pytest.raises(builder.BuildError, match="private state file"):
        builder.verify_release_tree(output)
    leaked_state.unlink()

    leaked_key = output / "leaked.txt"
    leaked_key.write_text("DOLA_ADMIN_KEY=admin-1234567890abcdefghijklmnop\n", encoding="utf-8")
    with pytest.raises(builder.BuildError, match="hard-coded Dola credential"):
        builder.verify_release_tree(output)
    leaked_key.unlink()

    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    escaping = output / "escape"
    escaping.symlink_to(outside)
    with pytest.raises(builder.BuildError, match="absolute symlink|escapes the bundle"):
        builder.verify_release_tree(output)


def test_manifest_tree_hashes_detect_runtime_tampering(tmp_path):
    builder = load_builder()
    output = builder.build_runtime("macos-arm64", tmp_path / "runtime", fixture=True)
    (output / "app/dola_gateway/server.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(builder.BuildError, match="app_tree_sha256"):
        builder.verify_runtime(output, allow_fixture=True)


def test_installer_builder_rejects_fixture_runtime_before_packaging(tmp_path):
    builder = load_builder()
    target = builder.native_target()
    output = builder.build_runtime(target, tmp_path / "runtime", fixture=True)
    installer = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "release" / "build_installer.py"),
            "--target",
            target,
            "--runtime",
            str(output),
            "--output",
            str(tmp_path / "installers"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installer.returncode != 0
    assert "not distributable" in installer.stderr
