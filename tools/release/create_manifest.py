#!/usr/bin/env python3
"""Assemble or verify a complete dual-native unsigned desktop release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from versioning import require_semver


TARGETS = ("macos-arm64", "windows-x64")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_metadata(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"invalid build metadata: {path}")
    return value


def _validate_build(metadata_path: Path, expected_version: str | None = None) -> dict:
    metadata = _read_metadata(metadata_path)
    target = metadata.get("target")
    if target not in TARGETS:
        raise ValueError(f"unsupported target in {metadata_path}: {target!r}")
    version = require_semver(str(metadata.get("application_version", "")))
    if expected_version and version != expected_version:
        raise ValueError(f"{target} version {version} does not match {expected_version}")
    if metadata.get("signed_for_distribution") is not False:
        raise ValueError(f"{target} metadata must explicitly describe an unsigned build")
    artifacts = metadata.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError(f"{target} metadata must contain exactly one release artifact")
    artifact = artifacts[0]
    extension = "dmg" if target == "macos-arm64" else "exe"
    expected_name = f"dola-gateway-v{version}-{target}.{extension}"
    if artifact.get("name") != expected_name:
        raise ValueError(f"non-canonical {target} artifact name: {artifact.get('name')!r}")
    artifact_path = metadata_path.parent / expected_name
    checksum_name = expected_name + ".sha256"
    checksum_path = metadata_path.parent / checksum_name
    if artifact.get("checksum") != checksum_name or not checksum_path.is_file():
        raise ValueError(f"missing canonical checksum for {expected_name}")
    if not artifact_path.is_file():
        raise ValueError(f"missing release artifact {artifact_path}")
    actual_hash = sha256(artifact_path)
    if artifact.get("sha256") != actual_hash:
        raise ValueError(f"metadata checksum mismatch for {expected_name}")
    if artifact.get("size") != artifact_path.stat().st_size:
        raise ValueError(f"metadata size mismatch for {expected_name}")
    checksum_parts = checksum_path.read_text(encoding="utf-8").strip().split()
    if checksum_parts != [actual_hash, expected_name]:
        raise ValueError(f"checksum file mismatch for {expected_name}")
    return {
        "target": target,
        "version": version,
        "name": expected_name,
        "checksum": checksum_name,
        "sha256": actual_hash,
        "size": artifact_path.stat().st_size,
        "metadata": metadata_path.name,
        "source_dir": metadata_path.parent,
    }


def create_release_bundle(inputs: list[Path], output: Path, *, expected_version: str | None = None) -> dict:
    if expected_version:
        require_semver(expected_version)
    metadata_paths: list[Path] = []
    for source in inputs:
        metadata_paths.extend(sorted(source.rglob("*.metadata.json")))
    builds = [_validate_build(path, expected_version) for path in metadata_paths]
    by_target = {build["target"]: build for build in builds}
    if len(builds) != len(TARGETS) or set(by_target) != set(TARGETS):
        raise ValueError("release bundle requires exactly one macos-arm64 and one windows-x64 build")
    versions = {build["version"] for build in builds}
    if len(versions) != 1:
        raise ValueError(f"native build versions differ: {sorted(versions)}")
    version = versions.pop()

    output.mkdir(parents=True, exist_ok=True)
    records = []
    for target in TARGETS:
        build = by_target[target]
        source = build.pop("source_dir")
        canonical_metadata = f"dola-gateway-v{version}-{target}.metadata.json"
        for name in (build["name"], build["checksum"]):
            shutil.copy2(source / name, output / name)
        shutil.copy2(source / build["metadata"], output / canonical_metadata)
        build["metadata"] = canonical_metadata
        records.append(build)

    manifest = {
        "schema_version": 1,
        "application_version": version,
        "complete": True,
        "signed_for_distribution": False,
        "artifacts": records,
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['name']}\n" for record in records),
        encoding="utf-8",
    )
    verify_release_bundle(output, expected_version=expected_version)
    return manifest


def verify_release_bundle(bundle: Path, *, expected_version: str | None = None) -> dict:
    manifest_path = bundle / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("complete") is not True:
        raise ValueError("release manifest is not marked complete")
    version = require_semver(str(manifest.get("application_version", "")))
    if expected_version and version != expected_version:
        raise ValueError(f"release bundle version {version} does not match {expected_version}")
    if manifest.get("signed_for_distribution") is not False:
        raise ValueError("release manifest must describe unsigned artifacts")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or {item.get("target") for item in artifacts} != set(TARGETS):
        raise ValueError("release manifest does not contain both native targets")
    if len(artifacts) != len(TARGETS):
        raise ValueError("release manifest contains duplicate native targets")
    checksum_lines = []
    for record in artifacts:
        target = record["target"]
        extension = "dmg" if target == "macos-arm64" else "exe"
        expected_name = f"dola-gateway-v{version}-{target}.{extension}"
        if record.get("name") != expected_name:
            raise ValueError(f"non-canonical release manifest name for {target}")
        path = bundle / expected_name
        actual_hash = sha256(path)
        if actual_hash != record.get("sha256") or path.stat().st_size != record.get("size"):
            raise ValueError(f"release artifact integrity mismatch for {expected_name}")
        checksum = bundle / record["checksum"]
        if checksum.read_text(encoding="utf-8").strip().split() != [actual_hash, expected_name]:
            raise ValueError(f"per-artifact checksum mismatch for {expected_name}")
        metadata = _validate_build(bundle / record["metadata"], version)
        if metadata["sha256"] != actual_hash:
            raise ValueError(f"build metadata mismatch for {expected_name}")
        checksum_lines.append(f"{actual_hash}  {expected_name}\n")
    if (bundle / "SHA256SUMS").read_text(encoding="utf-8") != "".join(checksum_lines):
        raise ValueError("combined SHA256SUMS does not match the release manifest")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-only", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    if bool(args.verify_only) == bool(args.output):
        parser.error("choose exactly one of --output or --verify-only")
    if args.verify_only:
        manifest = verify_release_bundle(args.verify_only, expected_version=args.expected_version)
    else:
        if not args.input:
            parser.error("--input is required when assembling a bundle")
        manifest = create_release_bundle(
            args.input, args.output, expected_version=args.expected_version
        )
    print(
        f"verified complete unsigned desktop release {manifest['application_version']} "
        f"for {', '.join(TARGETS)}"
    )


if __name__ == "__main__":
    main()
