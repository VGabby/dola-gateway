from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = PROJECT_ROOT / "tools" / "release"
sys.path.insert(0, str(RELEASE_DIR))

from changelog import extract_release_notes  # noqa: E402
from versioning import collect_versions, require_semver, validate_version_alignment  # noqa: E402


VERSION_POINTS = tuple(collect_versions(PROJECT_ROOT))


def _copy_version_fixture(destination: Path) -> None:
    paths = [
        "VERSION",
        "src/dola_gateway/VERSION",
        "desktop/package.json",
        "desktop/package-lock.json",
        "desktop/src-tauri/tauri.conf.json",
        "desktop/src-tauri/Cargo.toml",
        "desktop/src-tauri/Cargo.lock",
    ]
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, target)


def _misalign(root: Path, point: str) -> None:
    bad = "9.9.9"
    if point in {"VERSION", "src/dola_gateway/VERSION"}:
        (root / point).write_text(bad + "\n", encoding="utf-8")
        return
    if point.startswith("desktop/package-lock.json"):
        path = root / "desktop/package-lock.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if point.endswith(" root"):
            value["packages"][""]["version"] = bad
        else:
            value["version"] = bad
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return
    if point.endswith(".json"):
        path = root / point
        value = json.loads(path.read_text(encoding="utf-8"))
        value["version"] = bad
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return
    path = root / point
    body = path.read_text(encoding="utf-8")
    if point.endswith("Cargo.toml"):
        body = re.sub(r'(?m)^version = "[^"]+"$', f'version = "{bad}"', body, count=1)
    else:
        body = re.sub(
            r'(\[\[package\]\]\nname = "dola-gateway-desktop"\nversion = ")[^"]+',
            rf"\g<1>{bad}",
            body,
            count=1,
        )
    path.write_text(body, encoding="utf-8")


@pytest.mark.parametrize("point", VERSION_POINTS)
def test_every_version_propagation_point_is_checked(tmp_path, point):
    _copy_version_fixture(tmp_path)
    _misalign(tmp_path, point)
    with pytest.raises(ValueError, match=re.escape(point)):
        validate_version_alignment(tmp_path)


@pytest.mark.parametrize(
    "version",
    ["0.0.0", "1.2.3", "1.2.3-rc.1", "1.2.3-alpha.1+build.5"],
)
def test_strict_semver_accepts_valid_versions(version):
    assert require_semver(version) == version


@pytest.mark.parametrize(
    "version",
    ["v1.2.3", "01.2.3", "1.02.3", "1.2", "1.2.3-01", "1.2.3_rc1", "1.2.3-"],
)
def test_strict_semver_rejects_invalid_versions(version):
    with pytest.raises(ValueError, match="strict SemVer"):
        require_semver(version)


def test_bump_dry_run_is_deterministic_and_does_not_write():
    command = [
        sys.executable,
        str(RELEASE_DIR / "bump_version.py"),
        "9.8.7-rc.1",
        "--date",
        "2030-01-02",
        "--dry-run",
    ]
    before = collect_versions(PROJECT_ROOT)
    first = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stdout == second.stdout
    assert "9.8.7-rc.1" in first.stdout
    assert "desktop/src-tauri/Cargo.lock" in first.stdout
    assert collect_versions(PROJECT_ROOT) == before


def test_tag_parity_and_changelog_extraction():
    version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    notes = extract_release_notes(version)
    assert notes.startswith("- ")
    assert "Release notes pending" not in notes
    accepted = subprocess.run(
        [sys.executable, "tools/release/check_release.py", "--tag", f"v{version}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    rejected = subprocess.run(
        [sys.executable, "tools/release/check_release.py", "--tag", "v9.9.9"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert rejected.returncode != 0
    assert "does not match VERSION" in rejected.stderr
