"""Build allowlisted, unsigned macOS DMGs for Apple Silicon and Intel."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


MACOS_DIR = Path(__file__).resolve().parent
PACKAGING_DIR = MACOS_DIR.parent
PROJECT_ROOT = MACOS_DIR.parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
sys.path.insert(0, str(PACKAGING_DIR))

from release_common import (  # noqa: E402
    load_allowlist,
    release_version,
    sha256,
    verify_release_tree,
    write_checksums,
)

TARGETS = {
    "arm64": {
        "uv_target": "aarch64-apple-darwin",
        "uv_sha256": "388029510fdf64771745e9fb85cd6ec042580678a9e61c90fe355301f1c42f1e",
    },
    "x86_64": {
        "uv_target": "x86_64-apple-darwin",
        "uv_sha256": "a7d9ae35ce2d192cb0356f07439cfc6768d4dff8e95ae69f821e8fbe7bcb0e09",
    },
}
UV_VERSION = "0.9.15"


def build_target(target: str) -> Path:
    version = release_version(PROJECT_ROOT)
    metadata = TARGETS[target]
    stage_dir = DIST_DIR / f"macos-stage-{target}"
    dmg_root = stage_dir / "dmg-root"
    app_bundle = dmg_root / "Dola Gateway.app"
    resources = app_bundle / "Contents" / "Resources"
    app_dir = resources / "app"
    dmg_path = DIST_DIR / f"DolaGateway-{version}-macos-{target}.dmg"

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    if dmg_path.exists():
        dmg_path.unlink()
    dmg_root.mkdir(parents=True)

    shutil.copytree(MACOS_DIR / "payload" / "app-template", app_bundle)
    shutil.copytree(
        MACOS_DIR / "payload" / "launcher",
        resources / "launcher",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(MACOS_DIR / "payload" / "dmg-root", dmg_root, dirs_exist_ok=True)
    app_dir.mkdir(parents=True)
    for relative in load_allowlist(PROJECT_ROOT, MACOS_DIR / "release-files.txt"):
        destination = app_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copy2(
        MACOS_DIR / "requirements-macos.txt",
        app_dir / "requirements-macos.txt",
    )

    info_path = app_bundle / "Contents" / "Info.plist"
    info = info_path.read_text(encoding="utf-8").replace("__VERSION__", version)
    info_path.write_text(info, encoding="utf-8", newline="\n")
    (resources / "ARCHITECTURE").write_text(
        target + "\n", encoding="utf-8", newline="\n"
    )
    (resources / "VERSION").write_text(
        version + "\n", encoding="utf-8", newline="\n"
    )
    uv_target = metadata["uv_target"]
    uv_url = (
        f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
        f"uv-{uv_target}.tar.gz"
    )
    (resources / "launcher" / "target.env").write_text(
        f"UV_VERSION={UV_VERSION}\n"
        f"UV_TARGET={uv_target}\n"
        f"UV_URL={uv_url}\n"
        f"UV_SHA256={metadata['uv_sha256']}\n",
        encoding="utf-8",
        newline="\n",
    )
    (dmg_root / "VERSION").write_text(
        version + "\n", encoding="utf-8", newline="\n"
    )
    os.symlink("/Applications", dmg_root / "Applications")

    executables = [app_bundle / "Contents" / "MacOS" / "dola-gateway"]
    executables.extend(dmg_root.glob("*.command"))
    executables.extend((resources / "launcher").glob("*.sh"))
    executables.extend((resources / "launcher").glob("*.command"))
    for path in executables:
        path.chmod(0o755)

    verify_release_tree(dmg_root)
    subprocess.run(
        [
            "/usr/bin/codesign", "--force", "--deep", "--sign", "-",
            "--timestamp=none", str(app_bundle),
        ],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_bundle)],
        check=True,
    )
    write_checksums(dmg_root)
    verify_release_tree(dmg_root)
    subprocess.run(
        [
            "/usr/bin/hdiutil", "create", "-quiet",
            "-volname", f"Dola Gateway {version} {target}",
            "-srcfolder", str(dmg_root), "-ov", "-format", "UDZO", str(dmg_path),
        ],
        check=True,
    )
    digest = sha256(dmg_path)
    dmg_path.with_suffix(".dmg.sha256").write_text(
        f"{digest}  {dmg_path.name}\n", encoding="utf-8", newline="\n"
    )
    print(dmg_path)
    print(f"sha256={digest}")
    return dmg_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS))
    args = parser.parse_args()
    for target in ([args.target] if args.target else TARGETS):
        build_target(target)


if __name__ == "__main__":
    main()
