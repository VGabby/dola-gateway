#!/usr/bin/env python3
"""Build a native, self-contained Dola Gateway desktop installer.

The production runtime and Tauri installer must be built on their destination
operating system. This script stages a clean copy of the shell, injects an
already verified runtime, invokes the pinned Tauri CLI, and emits checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build_runtime import (
    BuildError,
    DEFAULT_OUTPUT_ROOT,
    PROJECT_ROOT,
    native_target,
    tree_sha256,
    verify_release_tree,
    verify_runtime,
)


DESKTOP_SOURCE = PROJECT_ROOT / "desktop"
DEFAULT_DIST = PROJECT_ROOT / "dist" / "desktop-installers"
IGNORED_SHELL_ITEMS = {"node_modules", "runtime-placeholder", "target", ".DS_Store"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_checksum(path: Path) -> Path:
    checksum = path.with_name(path.name + ".sha256")
    checksum.write_text(f"{_sha256(path)}  {path.name}\n", encoding="utf-8")
    return checksum


def _canonical_artifact_name(candidate: Path, target: str, version: str) -> str:
    base = f"dola-gateway-v{version}-{target}"
    if candidate.suffix.casefold() == ".exe":
        return base + ".exe"
    if candidate.suffix.casefold() == ".dmg":
        return base + ".dmg"
    if candidate.suffix.casefold() == ".app":
        return base + ".app"
    raise BuildError(f"unexpected Tauri bundle type: {candidate.name}")


def _copy_shell(destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in IGNORED_SHELL_ITEMS}

    shutil.copytree(DESKTOP_SOURCE, destination, ignore=ignore)


def _builder_environment() -> dict[str, str]:
    environment = os.environ.copy()
    rustup = shutil.which("rustup")
    if not rustup:
        raise BuildError("rustup is required on the release builder")
    cargo = subprocess.run(
        [rustup, "which", "--toolchain", "1.88.0", "cargo"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rustc = subprocess.run(
        [rustup, "which", "--toolchain", "1.88.0", "rustc"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    environment["RUSTUP_TOOLCHAIN"] = "1.88.0"
    environment["RUSTC"] = rustc
    environment["PATH"] = str(Path(cargo).parent) + os.pathsep + environment.get("PATH", "")
    return environment


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _artifact_candidates(stage: Path, target: str) -> list[Path]:
    bundle_dir = stage / "src-tauri" / "target" / "release" / "bundle"
    if target == "macos-arm64":
        candidates = [
            *(bundle_dir / "dmg").glob("*.dmg"),
            *(bundle_dir / "macos").glob("*.app"),
        ]
    else:
        candidates = list((bundle_dir / "nsis").glob("*.exe"))
    return sorted(path for path in candidates if path.is_file() or path.is_dir())


def _smoke_signed_macos_runtime(embedded: Path, manifest: dict) -> None:
    embedded = embedded.resolve()
    interpreter = embedded / manifest["python"]["interpreter"]
    browser = embedded / manifest["patchright"]["browser_executable"]
    app_dir = embedded / manifest["app"]["directory"]
    code = """
import asyncio
from patchright.async_api import async_playwright
import aiohttp, fastapi, pydantic, PIL, cv2
from dola_gateway import server

async def main():
    async with async_playwright() as playwright:
        instance = await playwright.chromium.launch(
            headless=True,
            executable_path=__import__('os').environ['DOLA_BROWSER_EXECUTABLE'],
        )
        try:
            page = await instance.new_page()
            await page.set_content('<p id="ready">signed package ready</p>')
            assert await page.locator('#ready').text_content() == 'signed package ready'
        finally:
            await instance.close()

asyncio.run(main())
print('signed embedded runtime smoke passed')
"""
    with tempfile.TemporaryDirectory(prefix="dola-signed-runtime-") as state:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("DOLA_") and key != "PLAYWRIGHT_BROWSERS_PATH"
        }
        environment.update(
            {
                "DOLA_STATE_DIR": state,
                "DOLA_ENV_FILE": str(Path(state) / "missing.env"),
                "DOLA_BROWSER_EXECUTABLE": str(browser),
                "PLAYWRIGHT_BROWSERS_PATH": str(embedded / "browsers"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            }
        )
        _run([str(interpreter), "-c", code], cwd=app_dir, env=environment)


def _verify_emitted_artifacts(
    emitted: list[Path],
    *,
    target: str,
    runtime: Path,
    manifest: dict,
) -> dict[str, object]:
    """Verify the platform artifact after Tauri's signing/bundling phase."""
    if target == "macos-arm64":
        apps = [path for path in emitted if path.suffix == ".app"]
        images = [path for path in emitted if path.suffix == ".dmg"]
        if len(apps) != 1 or len(images) != 1:
            raise BuildError("macOS packaging must emit exactly one app and one DMG")
        app = apps[0]
        plist_path = app / "Contents" / "Info.plist"
        plist = plistlib.loads(plist_path.read_bytes())
        icon_name = plist.get("CFBundleIconFile") or plist.get("CFBundleIconName")
        if not isinstance(icon_name, str) or not icon_name.strip():
            raise BuildError("macOS app bundle has no icon declared in Info.plist")
        icon_path = app / "Contents" / "Resources" / icon_name
        if not icon_path.suffix:
            icon_path = icon_path.with_suffix(".icns")
        if not icon_path.is_file() or icon_path.read_bytes()[:4] != b"icns":
            raise BuildError("macOS app bundle is missing its declared ICNS icon")
        embedded = app / "Contents" / "Resources" / "runtime"
        verify_release_tree(embedded)
        source_manifest_hash = _sha256(runtime / "runtime-manifest.json")
        if _sha256(embedded / "runtime-manifest.json") != source_manifest_hash:
            raise BuildError("Tauri changed the embedded runtime manifest")
        if tree_sha256(embedded / "app") != manifest["provenance"]["app_tree_sha256"]:
            raise BuildError("Tauri changed the embedded Python application payload")
        # Ad-hoc codesigning intentionally changes Mach-O bytes in the Python
        # and Chromium trees. Their pre-sign hashes remain provenance records;
        # the outer code seal is the post-sign integrity boundary.
        _run(
            ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
            cwd=app.parent,
        )
        _smoke_signed_macos_runtime(embedded, manifest)
        _run(["hdiutil", "verify", str(images[0])], cwd=images[0].parent)
        return {
            "post_bundle_verification": [
                "macOS declared ICNS icon",
                "embedded runtime manifest",
                "application payload hash",
                "macOS deep code seal",
                "signed embedded runtime offline smoke",
                "DMG filesystem checksum",
            ],
            "native_runtime_hashes_modified_by_adhoc_signing": True,
        }

    installers = [path for path in emitted if path.suffix.casefold() == ".exe"]
    header = b""
    if len(installers) == 1:
        with installers[0].open("rb") as stream:
            header = stream.read(2)
    if len(installers) != 1 or header != b"MZ":
        raise BuildError("Windows packaging must emit one valid PE NSIS installer")
    return {
        "post_bundle_verification": ["NSIS PE header"],
        "native_runtime_hashes_modified_by_adhoc_signing": False,
    }


def build_installer(
    target: str,
    runtime: Path,
    output: Path,
    *,
    keep_stage: bool = False,
) -> list[Path]:
    expected = native_target()
    if target != expected:
        raise BuildError(
            f"desktop installers are native-only: requested {target}, current host is {expected}"
        )
    runtime = runtime.expanduser().resolve()
    manifest = verify_runtime(runtime)
    if manifest["target"] != target:
        raise BuildError(
            f"runtime target {manifest['target']} does not match installer target {target}"
        )
    npm = shutil.which("npm")
    if not npm:
        raise BuildError("npm is required on the release builder")
    if not shutil.which("rustup"):
        raise BuildError("rustup is required on the release builder")

    output = output.expanduser().resolve()
    if output in {Path("/").resolve(), Path.home().resolve(), PROJECT_ROOT.resolve()}:
        raise BuildError(f"refusing broad installer output path: {output}")
    output.mkdir(parents=True, exist_ok=True)
    stage_root = PROJECT_ROOT / "dist" / "desktop-build" / target
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.parent.mkdir(parents=True, exist_ok=True)
    _copy_shell(stage_root)
    shutil.copytree(runtime, stage_root / "runtime-placeholder", symlinks=True)

    try:
        _run([npm, "ci", "--ignore-scripts"], cwd=stage_root)
        bundles = "app,dmg" if target == "macos-arm64" else "nsis"
        _run(
            [npm, "run", "tauri", "--", "build", "--bundles", bundles],
            cwd=stage_root,
            env=_builder_environment(),
        )
        candidates = _artifact_candidates(stage_root, target)
        if not candidates:
            raise BuildError("Tauri completed without producing the requested installer")

        emitted: list[Path] = []
        for candidate in candidates:
            destination = output / _canonical_artifact_name(
                candidate, target, manifest["app"]["version"]
            )
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if candidate.is_dir():
                shutil.copytree(candidate, destination, symlinks=True)
            else:
                shutil.copy2(candidate, destination)
                _write_checksum(destination)
            emitted.append(destination)
        verification = _verify_emitted_artifacts(
            emitted, target=target, runtime=runtime, manifest=manifest
        )
        release_artifacts = [
            path for path in emitted
            if path.is_file() and path.suffix.casefold() in {".dmg", ".exe"}
        ]
        if len(release_artifacts) != 1:
            raise BuildError(f"{target} must produce exactly one distributable installer")
        release_artifact = release_artifacts[0]
        checksum = release_artifact.with_name(release_artifact.name + ".sha256")
        metadata = {
            "schema_version": 1,
            "target": target,
            "application_version": manifest["app"]["version"],
            "runtime_manifest_sha256": _sha256(runtime / "runtime-manifest.json"),
            "artifacts": [{
                "name": release_artifact.name,
                "checksum": checksum.name,
                "sha256": _sha256(release_artifact),
                "size": release_artifact.stat().st_size,
            }],
            "qa_artifacts": [path.name for path in emitted if path.is_dir()],
            "signed_for_distribution": False,
            **verification,
        }
        metadata_path = output / (
            f"dola-gateway-v{manifest['app']['version']}-{target}.metadata.json"
        )
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return emitted
    finally:
        if not keep_stage:
            shutil.rmtree(stage_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("macos-arm64", "windows-x64"))
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-stage", action="store_true")
    args = parser.parse_args()
    try:
        target = args.target or native_target()
        runtime = args.runtime or (DEFAULT_OUTPUT_ROOT / target)
        output = args.output or (DEFAULT_DIST / target)
        artifacts = build_installer(target, runtime, output, keep_stage=args.keep_stage)
        for artifact in artifacts:
            print(f"built desktop artifact: {artifact}")
    except (BuildError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"desktop installer build failed: {exc}") from exc


if __name__ == "__main__":
    main()
