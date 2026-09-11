#!/usr/bin/env python3
"""Assemble and verify the self-contained Python/Chromium desktop runtime.

Production builds are deliberately native-only. The output contains everything
the desktop shell needs at runtime; downloads and dependency installation happen
only on the builder machine. ``--fixture`` is an explicit, tiny offline mode for
tests and must never be distributed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


DESKTOP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DESKTOP_DIR.parents[1]
SPEC_PATH = DESKTOP_DIR / "runtime-spec.json"
ALLOWLIST_PATH = DESKTOP_DIR / "app-files.txt"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "dist" / "desktop-runtime"

FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    ".venv-moved-backup",
    "__pycache__",
    "accounts",
    "downloads",
}
FORBIDDEN_NAMES = {
    ".env",
    ".env.local",
    "accounts.local.json",
    "cookies.txt",
    "gateway.pid",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pma",
    ".pyc",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Google session cookie": re.compile(r"(?:__Secure-[13]P?SID|SAPISID)=[^\s;]{12,}"),
    "signed cloud URL": re.compile(r"[?&](?:X-Amz-Signature|Signature)=[0-9A-Fa-f]{24,}"),
    "hard-coded Dola credential": re.compile(
        r"(?mi)^\s*DOLA_(?:API_KEYS|ADMIN_KEY)\s*=\s*"
        r"(?!replace-|<|\$|%|\{)[A-Za-z0-9_.:-]{20,}\s*$"
    ),
    "hard-coded bearer token": re.compile(
        r"(?i)Authorization\s*:\s*Bearer\s+[A-Za-z0-9_.-]{24,}"
    ),
}
REQUIRED_IMPORTS = ("aiohttp", "fastapi", "pydantic", "patchright", "PIL", "cv2")


class BuildError(RuntimeError):
    """A release input or assembled runtime is invalid."""


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("schema_version") != 1:
        raise BuildError(f"unsupported runtime spec schema in {path}")
    targets = spec.get("targets")
    if not isinstance(targets, dict) or set(targets) != {"macos-arm64", "windows-x64"}:
        raise BuildError("runtime spec must define only macos-arm64 and windows-x64")
    for section, fields in {
        "python": ("provider", "version"),
        "patchright": ("version", "chromium_revision", "chromium_version"),
    }.items():
        value = spec.get(section)
        if not isinstance(value, dict) or any(not value.get(field) for field in fields):
            raise BuildError(f"runtime spec has an incomplete {section} section")
    return spec


def native_target(
    system: str | None = None, machine: str | None = None, *, spec: dict[str, Any] | None = None
) -> str:
    spec = spec or load_spec()
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    for target, metadata in spec["targets"].items():
        if system == metadata["host_system"] and machine in {
            item.lower() for item in metadata["host_machines"]
        }:
            return target
    raise BuildError(
        f"unsupported build host {system}/{machine}; use native macOS arm64 or Windows x64"
    )


def require_native_target(target: str, *, spec: dict[str, Any] | None = None) -> None:
    actual = native_target(spec=spec)
    if target != actual:
        raise BuildError(
            f"cross-host desktop runtime builds are not supported: requested {target}, "
            f"current host is {actual}. Build {target} on its native OS."
        )


def _safe_relative(value: str | Path, *, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise BuildError(f"unsafe {label}: {value}")
    return relative


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        relative = _safe_relative(value, label="allowlist path")
        key = relative.as_posix().casefold()
        if key in seen:
            raise BuildError(f"duplicate allowlist entry: {relative}")
        seen.add(key)
        source = PROJECT_ROOT / relative
        if not source.is_file():
            raise BuildError(f"allowlisted application file is missing: {relative}")
        result.append(relative)
    if not result:
        raise BuildError("desktop application allowlist is empty")
    return result


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _resolved_inside(root: Path, path: Path) -> Path:
    root = root.resolve()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BuildError(f"broken or cyclic runtime path: {path}") from exc
    if not _inside(root, resolved):
        raise BuildError(f"runtime path escapes the bundle: {path} -> {resolved}")
    return resolved


def _validate_release_name(relative: Path) -> None:
    lowered_parts = {part.casefold() for part in relative.parts}
    if lowered_parts & FORBIDDEN_PARTS:
        raise BuildError(f"private/developer directory in desktop runtime: {relative}")
    lowered_name = relative.name.casefold()
    if lowered_name in FORBIDDEN_NAMES:
        raise BuildError(f"private state file in desktop runtime: {relative}")
    if any(lowered_name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise BuildError(f"forbidden generated file in desktop runtime: {relative}")


def _scan_text(path: Path) -> list[str]:
    if path.stat().st_size > 2 * 1024 * 1024:
        return []
    try:
        body = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return [label for label, pattern in SECRET_PATTERNS.items() if pattern.search(body)]


def verify_release_tree(root: Path) -> list[Path]:
    """Check containment, private-state exclusions, and high-confidence secrets.

    Relocatable Python and macOS Chromium both contain required internal
    symlinks. Relative symlinks are accepted only when their resolved target
    exists and remains inside the assembled runtime.
    """
    root = root.resolve()
    if not root.is_dir():
        raise BuildError(f"desktop runtime root does not exist: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        _validate_release_name(relative)
        if path.is_symlink():
            if Path(os.readlink(path)).is_absolute():
                raise BuildError(f"absolute symlink in desktop runtime: {relative}")
            _resolved_inside(root, path)
            continue
        if not path.is_file():
            continue
        _resolved_inside(root, path)
        leaks = _scan_text(path)
        if leaks:
            raise BuildError(
                f"sensitive content in desktop runtime file {relative}: {', '.join(leaks)}"
            )
        files.append(relative)
    if not files:
        raise BuildError("desktop runtime is empty")
    return files


def _hash_file(path: Path, digest: Any) -> None:
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)


def tree_sha256(root: Path, *, exclude: Iterable[str] = ()) -> str:
    excluded = set(exclude)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in excluded or path.is_dir():
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0")
            _hash_file(path, digest)
        digest.update(b"\0")
    return digest.hexdigest()


def _run(
    command: list[str], *, cwd: Path = PROJECT_ROOT, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    return subprocess.run(command, cwd=cwd, env=env, text=True, check=True)


def _uv_binary() -> str:
    override = os.environ.get("DOLA_UV", "").strip()
    candidate = override or shutil.which("uv")
    if not candidate:
        raise BuildError("uv is required on the builder machine (set DOLA_UV to its path)")
    return candidate


def _select_one_directory(parent: Path, prefix: str) -> Path:
    matches = [item for item in parent.iterdir() if item.is_dir() and item.name.startswith(prefix)]
    if len(matches) != 1:
        names = ", ".join(item.name for item in matches) or "none"
        raise BuildError(f"expected one {prefix} directory under {parent}, found {names}")
    return matches[0]


def _find_candidate(root: Path, candidates: Iterable[str], *, label: str) -> Path:
    for value in candidates:
        path = root / _safe_relative(value, label=label)
        if path.is_file():
            return path
    raise BuildError(f"could not find {label} under {root}")


def _copy_application(destination: Path) -> None:
    for relative in load_allowlist():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, target)


def _remove_generated_artifacts(root: Path) -> None:
    """Remove interpreter caches created while inspecting the staged runtime."""
    for directory in sorted(root.rglob("__pycache__"), reverse=True):
        if directory.is_dir() and not directory.is_symlink():
            shutil.rmtree(directory)
    for pattern in ("*.pyc", "*.pyo", ".DS_Store"):
        for path in root.rglob(pattern):
            if path.is_file() or path.is_symlink():
                path.unlink()


def _python_version(interpreter: Path) -> str:
    result = subprocess.run(
        [str(interpreter), "-c", "import platform; print(platform.python_version())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _package_version(interpreter: Path, package: str) -> str:
    code = "import importlib.metadata as m,sys; print(m.version(sys.argv[1]))"
    result = subprocess.run(
        [str(interpreter), "-c", code, package], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _patchright_browser_metadata(interpreter: Path) -> tuple[str, str]:
    code = r"""
import importlib.util, json, pathlib
spec = importlib.util.find_spec('patchright')
if not spec or not spec.origin:
    raise SystemExit('patchright is not installed')
path = pathlib.Path(spec.origin).resolve().parent / 'driver' / 'package' / 'browsers.json'
data = json.loads(path.read_text(encoding='utf-8'))
browser = next(item for item in data['browsers'] if item['name'] == 'chromium')
print(browser['revision'] + '|' + browser['browserVersion'])
"""
    result = subprocess.run(
        [str(interpreter), "-c", code], capture_output=True, text=True, check=True
    )
    parts = result.stdout.strip().split("|", 1)
    if len(parts) != 2:
        raise BuildError("could not read Patchright Chromium metadata")
    return parts[0], parts[1]


def _verify_native_binary(path: Path, target: str) -> None:
    body = path.read_bytes()[:4096]
    if target == "macos-arm64":
        if len(body) < 8:
            raise BuildError(f"not a Mach-O arm64 executable: {path}")
        magic = struct.unpack(">I", body[:4])[0]
        endian = ">" if magic in {0xFEEDFACE, 0xFEEDFACF} else "<"
        if magic not in {0xFEEDFACE, 0xFEEDFACF, 0xCEFAEDFE, 0xCFFAEDFE}:
            raise BuildError(f"not a Mach-O executable: {path}")
        cpu_type = struct.unpack(f"{endian}I", body[4:8])[0]
        if cpu_type != 0x0100000C:
            raise BuildError(f"not a native arm64 Mach-O executable: {path}")
        return
    if target == "windows-x64":
        if len(body) < 64 or body[:2] != b"MZ":
            raise BuildError(f"not a Windows executable: {path}")
        pe_offset = struct.unpack("<I", body[0x3C:0x40])[0]
        if pe_offset + 6 > len(body) or body[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise BuildError(f"invalid PE executable: {path}")
        machine = struct.unpack("<H", body[pe_offset + 4 : pe_offset + 6])[0]
        if machine != 0x8664:
            raise BuildError(f"not a native Windows x64 executable: {path}")


def _install_production_runtime(stage: Path, target: str, spec: dict[str, Any]) -> dict[str, Any]:
    uv = _uv_binary()
    build_cache = stage / ".build"
    installs = build_cache / "python-installs"
    uv_cache = build_cache / "uv-cache"
    installs.mkdir(parents=True)
    environment = os.environ.copy()
    environment.update(
        {
            "UV_CACHE_DIR": str(uv_cache),
            "UV_MANAGED_PYTHON": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    python_version = spec["python"]["version"]
    _run(
        [uv, "python", "install", "--managed-python", "--no-bin", "--install-dir", str(installs), python_version],
        env=environment,
    )
    installed = _select_one_directory(installs, "cpython-")
    python_root = stage / "python"
    shutil.copytree(installed, python_root, symlinks=True)
    # uv marks its managed base interpreter as externally managed. This copy is
    # an application-private, immutable runtime image, so remove only the copy's
    # marker before installing the locked application wheels into it.
    for marker in python_root.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()
    interpreter = _find_candidate(
        python_root, spec["targets"][target]["python_interpreters"], label="Python interpreter"
    )

    locked_requirements = build_cache / "requirements.locked.txt"
    _run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-annotate",
            "--python",
            str(interpreter),
            "--output-file",
            str(locked_requirements),
        ],
        env=environment,
    )
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--system",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--strict",
            "--link-mode",
            "copy",
            "--requirements",
            str(locked_requirements),
        ],
        env=environment,
    )

    actual_python = _python_version(interpreter)
    if actual_python != python_version:
        raise BuildError(f"expected Python {python_version}, assembled {actual_python}")
    actual_patchright = _package_version(interpreter, "patchright")
    if actual_patchright != spec["patchright"]["version"]:
        raise BuildError(
            f"expected Patchright {spec['patchright']['version']}, assembled {actual_patchright}"
        )
    revision, chromium_version = _patchright_browser_metadata(interpreter)
    if revision != spec["patchright"]["chromium_revision"]:
        raise BuildError(f"Patchright expects Chromium revision {revision}, spec pins a different revision")
    if chromium_version != spec["patchright"]["chromium_version"]:
        raise BuildError(f"Patchright expects Chromium {chromium_version}, spec pins a different version")

    browser_downloads = build_cache / "browser-downloads"
    browser_environment = environment.copy()
    browser_environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_downloads)
    _run([str(interpreter), "-m", "patchright", "install", "chromium"], env=browser_environment)
    downloaded_browser = browser_downloads / f"chromium-{revision}"
    if not downloaded_browser.is_dir():
        raise BuildError(f"Patchright did not install full Chromium revision {revision}")
    browsers_root = stage / "browsers"
    browsers_root.mkdir()
    browser_root = browsers_root / downloaded_browser.name
    shutil.copytree(downloaded_browser, browser_root, symlinks=True)
    browser = _find_candidate(
        browser_root,
        spec["targets"][target]["browser_executables"],
        label="full Chromium executable",
    )

    _verify_native_binary(interpreter, target)
    _verify_native_binary(browser, target)
    browser_version_result = subprocess.run(
        [str(browser), "--version"], capture_output=True, text=True, check=True, timeout=20
    )
    if chromium_version not in (browser_version_result.stdout + browser_version_result.stderr):
        raise BuildError(
            f"Chromium executable did not report expected version {chromium_version}"
        )

    shutil.rmtree(build_cache)
    return {
        "fixture": False,
        "python_version": actual_python,
        "patchright_version": actual_patchright,
        "chromium_revision": revision,
        "chromium_version": chromium_version,
        "interpreter": interpreter.relative_to(stage).as_posix(),
        "browser": browser.relative_to(stage).as_posix(),
        "uv_version": subprocess.run(
            [uv, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip(),
    }


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_fixture_runtime(stage: Path, target: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Create a tiny structurally valid tree for offline tests only."""
    interpreter_relative = spec["targets"][target]["python_interpreters"][0]
    browser_relative = spec["targets"][target]["browser_executables"][0]
    interpreter = stage / "python" / interpreter_relative
    browser = stage / "browsers" / f"chromium-{spec['patchright']['chromium_revision']}" / browser_relative
    if target == "windows-x64":
        _write_executable(interpreter, "fixture Windows Python; not distributable\n")
        _write_executable(browser, "fixture Windows Chromium; not distributable\n")
    else:
        _write_executable(interpreter, "#!/bin/sh\nprintf 'fixture Python; not distributable\\n'\n")
        _write_executable(browser, "#!/bin/sh\nprintf 'fixture Chromium; not distributable\\n'\n")
    return {
        "fixture": True,
        "python_version": spec["python"]["version"],
        "patchright_version": spec["patchright"]["version"],
        "chromium_revision": spec["patchright"]["chromium_revision"],
        "chromium_version": spec["patchright"]["chromium_version"],
        "interpreter": interpreter.relative_to(stage).as_posix(),
        "browser": browser.relative_to(stage).as_posix(),
        "uv_version": "fixture",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    _hash_file(path, digest)
    return digest.hexdigest()


def _write_manifest(stage: Path, target: str, details: dict[str, Any]) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "target": target,
        "fixture": details["fixture"],
        "app": {
            "directory": "app",
            "entrypoint": "server:app",
            "version": (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        },
        "python": {
            "provider": "uv-managed-python-build-standalone",
            "version": details["python_version"],
            "interpreter": details["interpreter"],
        },
        "patchright": {
            "version": details["patchright_version"],
            "chromium_revision": details["chromium_revision"],
            "chromium_version": details["chromium_version"],
            "browser_executable": details["browser"],
        },
        "provenance": {
            "uv": details["uv_version"],
            "uv_lock_sha256": _sha256(PROJECT_ROOT / "uv.lock"),
            "runtime_spec_sha256": _sha256(SPEC_PATH),
            "app_allowlist_sha256": _sha256(ALLOWLIST_PATH),
            "python_tree_sha256": tree_sha256(stage / "python"),
            "browser_tree_sha256": tree_sha256(stage / "browsers"),
            "app_tree_sha256": tree_sha256(stage / "app"),
        },
    }
    path = stage / "runtime-manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_runtime(root: Path, *, allow_fixture: bool = False) -> dict[str, Any]:
    root = root.resolve()
    verify_release_tree(root)
    manifest_path = root / "runtime-manifest.json"
    if not manifest_path.is_file():
        raise BuildError("desktop runtime does not contain runtime-manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise BuildError("unsupported assembled runtime manifest schema")
    target = manifest.get("target")
    spec = load_spec()
    if target not in spec["targets"]:
        raise BuildError(f"unsupported assembled runtime target: {target}")
    if manifest.get("fixture") and not allow_fixture:
        raise BuildError("fixture runtime is not distributable")

    values = {
        "application directory": manifest.get("app", {}).get("directory"),
        "Python interpreter": manifest.get("python", {}).get("interpreter"),
        "Chromium executable": manifest.get("patchright", {}).get("browser_executable"),
    }
    resolved: dict[str, Path] = {}
    for label, value in values.items():
        if not isinstance(value, str):
            raise BuildError(f"manifest is missing {label}")
        relative = _safe_relative(value, label=label)
        candidate = root / relative
        _resolved_inside(root, candidate)
        resolved[label] = candidate
    if not resolved["application directory"].is_dir():
        raise BuildError("manifest application path is not a directory")
    for label in ("Python interpreter", "Chromium executable"):
        if not resolved[label].is_file():
            raise BuildError(f"manifest {label} is not a file")
        if target == "macos-arm64" and not os.access(resolved[label], os.X_OK):
            raise BuildError(f"manifest {label} is not executable")

    if manifest["python"].get("version") != spec["python"]["version"]:
        raise BuildError("assembled Python version differs from runtime spec")
    for key in ("version", "chromium_revision", "chromium_version"):
        manifest_key = "version" if key == "version" else key
        if manifest["patchright"].get(manifest_key) != spec["patchright"][key]:
            raise BuildError(f"assembled Patchright {manifest_key} differs from runtime spec")
    expected_hashes = {
        "uv_lock_sha256": _sha256(PROJECT_ROOT / "uv.lock"),
        "runtime_spec_sha256": _sha256(SPEC_PATH),
        "app_allowlist_sha256": _sha256(ALLOWLIST_PATH),
        "python_tree_sha256": tree_sha256(root / "python"),
        "browser_tree_sha256": tree_sha256(root / "browsers"),
        "app_tree_sha256": tree_sha256(root / "app"),
    }
    provenance = manifest.get("provenance", {})
    for name, expected in expected_hashes.items():
        if provenance.get(name) != expected:
            raise BuildError(f"runtime provenance mismatch: {name}")
    return manifest


def smoke_runtime(root: Path) -> None:
    manifest = verify_runtime(root)
    interpreter = root / manifest["python"]["interpreter"]
    browser = root / manifest["patchright"]["browser_executable"]
    app_dir = root / manifest["app"]["directory"]
    code = """
import asyncio
import config
from patchright.async_api import async_playwright
import aiohttp, fastapi, pydantic, PIL, cv2, server

async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=__import__('os').environ['DOLA_BROWSER_EXECUTABLE'],
        )
        try:
            page = await browser.new_page()
            await page.set_content('<title>Dola offline smoke</title><p id="ready">ready</p>')
            assert await page.locator('#ready').text_content() == 'ready'
        finally:
            await browser.close()

asyncio.run(main())
print('offline desktop runtime smoke passed')
"""
    with tempfile.TemporaryDirectory(prefix="dola-desktop-smoke-") as state:
        env = os.environ.copy()
        env.update(
            {
                "DOLA_STATE_DIR": state,
                "DOLA_ENV_FILE": str(Path(state) / ".env.local"),
                "DOLA_BROWSER_EXECUTABLE": str(browser),
                "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            }
        )
        _run([str(interpreter), "-c", code], cwd=app_dir, env=env)


def _guard_output(output: Path) -> Path:
    output = output.expanduser().resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), PROJECT_ROOT.resolve()}
    if output in forbidden:
        raise BuildError(f"refusing broad desktop runtime output path: {output}")
    if output.exists() and output.is_symlink():
        raise BuildError(f"desktop runtime output must not be a symlink: {output}")
    return output


def build_runtime(
    target: str,
    output: Path,
    *,
    fixture: bool = False,
    force: bool = False,
    smoke: bool = False,
) -> Path:
    spec = load_spec()
    if target not in spec["targets"]:
        raise BuildError(f"unsupported target: {target}")
    if not fixture:
        require_native_target(target, spec=spec)
    if fixture and smoke:
        raise BuildError("offline fixture runtimes cannot run the production browser smoke test")
    output = _guard_output(output)
    if output.exists() and not force:
        raise BuildError(f"output already exists (pass --force to replace it): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-build-", dir=output.parent))
    try:
        _copy_application(temporary / "app")
        details = (
            _install_fixture_runtime(temporary, target, spec)
            if fixture
            else _install_production_runtime(temporary, target, spec)
        )
        _remove_generated_artifacts(temporary)
        _write_manifest(temporary, target, details)
        verify_runtime(temporary, allow_fixture=fixture)
        if smoke:
            smoke_runtime(temporary)
        if output.exists():
            shutil.rmtree(output)
        temporary.replace(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(f"assembled desktop runtime: {output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("macos-arm64", "windows-x64"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true", help="replace an existing output")
    parser.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke", action="store_true", help="run offline imports and Chromium launch")
    parser.add_argument("--verify-only", type=Path, help="verify an already assembled runtime")
    args = parser.parse_args()
    try:
        if args.verify_only:
            verify_runtime(args.verify_only, allow_fixture=args.fixture)
            if args.smoke:
                smoke_runtime(args.verify_only)
            print(f"verified desktop runtime: {args.verify_only.resolve()}")
            return
        target = args.target
        if not target:
            target = native_target() if not args.fixture else "macos-arm64"
        output = args.output or (DEFAULT_OUTPUT_ROOT / target)
        build_runtime(target, output, fixture=args.fixture, force=args.force, smoke=args.smoke)
    except (BuildError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"desktop runtime build failed: {exc}") from exc


if __name__ == "__main__":
    main()
