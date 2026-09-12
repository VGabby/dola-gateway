from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = PROJECT_ROOT / "tools" / "release"
sys.path.insert(0, str(RELEASE_DIR))

from create_manifest import create_release_bundle, sha256, verify_release_bundle  # noqa: E402


def _native_build(root: Path, target: str, version: str = "1.2.3") -> Path:
    output = root / target
    output.mkdir(parents=True)
    extension = "dmg" if target == "macos-arm64" else "exe"
    name = f"dola-gateway-v{version}-{target}.{extension}"
    artifact = output / name
    artifact.write_bytes((target + "\n").encode("utf-8"))
    digest = sha256(artifact)
    checksum_name = name + ".sha256"
    (output / checksum_name).write_text(f"{digest}  {name}\n", encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "target": target,
        "application_version": version,
        "runtime_manifest_sha256": "0" * 64,
        "artifacts": [{
            "name": name,
            "checksum": checksum_name,
            "sha256": digest,
            "size": artifact.stat().st_size,
        }],
        "signed_for_distribution": False,
    }
    (output / f"dola-gateway-v{version}-{target}.metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return output


def test_dual_native_manifest_normalizes_names_checksums_and_metadata(tmp_path):
    inputs = [
        _native_build(tmp_path / "inputs", "macos-arm64"),
        _native_build(tmp_path / "inputs", "windows-x64"),
    ]
    bundle = tmp_path / "release"
    manifest = create_release_bundle(inputs, bundle, expected_version="1.2.3")
    assert manifest["complete"] is True
    assert manifest["signed_for_distribution"] is False
    assert {item["target"] for item in manifest["artifacts"]} == {
        "macos-arm64", "windows-x64"
    }
    assert verify_release_bundle(bundle, expected_version="1.2.3") == manifest
    assert (bundle / "SHA256SUMS").read_text(encoding="utf-8").count("\n") == 2


def test_incomplete_or_mismatched_native_build_cannot_form_release(tmp_path):
    mac = _native_build(tmp_path / "only", "macos-arm64")
    with pytest.raises(ValueError, match="exactly one macos-arm64 and one windows-x64"):
        create_release_bundle([mac], tmp_path / "incomplete")

    windows = _native_build(tmp_path / "mismatch", "windows-x64", "1.2.4")
    with pytest.raises(ValueError, match="versions differ"):
        create_release_bundle([mac, windows], tmp_path / "mismatched")


def test_combined_manifest_detects_post_assembly_tampering(tmp_path):
    inputs = [
        _native_build(tmp_path / "inputs", "macos-arm64"),
        _native_build(tmp_path / "inputs", "windows-x64"),
    ]
    bundle = tmp_path / "release"
    create_release_bundle(inputs, bundle)
    (bundle / "dola-gateway-v1.2.3-windows-x64.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_release_bundle(bundle)
