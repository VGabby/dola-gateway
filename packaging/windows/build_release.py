"""Build a deterministic, allowlisted Windows ZIP without local runtime state."""

from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path


WINDOWS_DIR = Path(__file__).resolve().parent
PACKAGING_DIR = WINDOWS_DIR.parent
PROJECT_ROOT = WINDOWS_DIR.parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
STAGE_DIR = DIST_DIR / "windows-stage"
sys.path.insert(0, str(PACKAGING_DIR))

from release_common import (  # noqa: E402
    load_allowlist,
    release_version,
    sha256,
    verify_release_tree,
    write_checksums,
)


def write_deterministic_zip(source_root: Path, destination: Path) -> None:
    archive_root = source_root.name
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
            relative = Path(archive_root) / path.relative_to(source_root)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def verify_archive(archive_path: Path, release_name: str) -> None:
    """Verify every packaged file against the manifest stored in the ZIP."""
    with zipfile.ZipFile(archive_path) as archive:
        checksum_name = f"{release_name}/CHECKSUMS.txt"
        rows = archive.read(checksum_name).decode("utf-8").splitlines()
        expected = {}
        for row in rows:
            digest, relative = row.split("  ", 1)
            expected[relative] = digest
        for relative, digest in expected.items():
            body = archive.read(f"{release_name}/{relative}")
            actual = hashlib.sha256(body).hexdigest()
            if actual != digest:
                raise ValueError(f"checksum mismatch in built archive: {relative}")


def build() -> Path:
    version = release_version(PROJECT_ROOT)
    release_name = f"DolaGateway-{version}-windows-x64"
    release_root = STAGE_DIR / release_name
    archive_path = DIST_DIR / f"{release_name}.zip"

    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    if archive_path.exists():
        archive_path.unlink()
    release_root.mkdir(parents=True)

    for relative in load_allowlist(PROJECT_ROOT, WINDOWS_DIR / "release-files.txt"):
        destination = release_root / "app" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)

    shutil.copy2(WINDOWS_DIR / "requirements-windows.txt", release_root / "app" / "requirements-windows.txt")
    shutil.copytree(
        WINDOWS_DIR / "payload",
        release_root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    (release_root / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")
    verify_release_tree(release_root)
    write_checksums(release_root)
    verify_release_tree(release_root)
    write_deterministic_zip(release_root, archive_path)
    verify_archive(archive_path, release_name)
    archive_digest = sha256(archive_path)
    archive_path.with_suffix(".zip.sha256").write_text(
        f"{archive_digest}  {archive_path.name}\n", encoding="utf-8", newline="\n"
    )
    return archive_path


if __name__ == "__main__":
    output = build()
    print(output)
    print(f"sha256={sha256(output)}")
