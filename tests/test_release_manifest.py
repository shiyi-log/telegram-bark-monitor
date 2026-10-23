"""发布清单生成器的契约测试。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.build_release_manifest import ManifestError, build_manifest

REVISION = "0123456789abcdef" * 2 + "01234567"


def _write_python_assets(dist: Path, *, version: str = "0.1.1") -> tuple[Path, Path]:
    wheel = dist / f"telegram_bark_monitor-{version}-py3-none-any.whl"
    sdist = dist / f"telegram_bark_monitor-{version}.tar.gz"
    wheel.write_bytes(b"wheel bytes\x00\x01")
    sdist.write_bytes(b"source bytes\n")
    return wheel, sdist


def _build(dist: Path, *, tag: str = "v0.1.1", revision: str = REVISION) -> dict:
    return build_manifest(dist, "owner/repo", tag, revision)


def _write_image_metadata(dist: Path, *, revision: str = REVISION) -> None:
    metadata = {
        "schemaVersion": 1,
        "repository": "ghcr.io/owner/repo",
        "version": "0.1.1",
        "revision": revision,
        "digest": "sha256:" + "a" * 64,
        "images": [
            {"platform": "linux/arm64", "digest": "sha256:" + "b" * 64},
            {"platform": "linux/amd64", "digest": "sha256:" + "c" * 64},
        ],
    }
    (dist / "container-image.json").write_text(
        json.dumps(metadata, sort_keys=True), encoding="utf-8"
    )


def test_manifest_uses_actual_bytes_and_exact_checksum_set(tmp_path: Path) -> None:
    wheel, sdist = _write_python_assets(tmp_path)
    _write_image_metadata(tmp_path)
    (tmp_path / "telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz").write_bytes(
        b"docker amd64"
    )
    (tmp_path / "release-notes.txt").write_text("ignored", encoding="utf-8")

    manifest = _build(tmp_path)

    assert manifest["schemaVersion"] == 1
    assert manifest["releaseId"] == f"owner/repo@{REVISION}/v0.1.1"
    assert manifest["draft"] is False
    assert manifest["prerelease"] is False
    assert {asset["name"] for asset in manifest["assets"]["python"]} == {wheel.name, sdist.name}
    docker_assets = {asset["name"]: asset for asset in manifest["assets"]["docker"]}
    assert docker_assets["container-image.json"]["format"] == "docker-image-metadata"
    assert docker_assets["telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz"]["format"] == (
        "docker-archive"
    )

    expected_paths = [
        wheel,
        sdist,
        tmp_path / "container-image.json",
        tmp_path / "telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz",
    ]
    expected = {
        path.name: {
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in expected_paths
    }
    actual_assets = {
        asset["name"]: {"size": asset["size"], "sha256": asset["sha256"]}
        for group in manifest["assets"].values()
        for asset in group
    }
    assert actual_assets == expected

    checksum_lines = (tmp_path / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert [line.split("  ", 1)[1] for line in checksum_lines] == sorted(expected)
    assert {line.split("  ", 1)[0] for line in checksum_lines} == {
        metadata["sha256"] for metadata in expected.values()
    }
    assert "manifest.json" not in "\n".join(checksum_lines)
    assert "SHA256SUMS" not in "\n".join(checksum_lines)

    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert saved == manifest


def test_old_version_asset_is_rejected(tmp_path: Path) -> None:
    _write_python_assets(tmp_path)
    (tmp_path / "telegram_bark_monitor-0.1.0.tar.gz").write_bytes(b"old")

    with pytest.raises(ManifestError, match="sdist 版本 0.1.0"):
        _build(tmp_path)


def test_docker_archive_version_is_rejected_and_unknown_copy_is_ignored(tmp_path: Path) -> None:
    _write_python_assets(tmp_path)
    (tmp_path / "telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz").write_bytes(b"one")
    (tmp_path / "telegram_bark_monitor-0.1.1-linux-arm64.docker.tar.gz").write_bytes(b"two")

    # 精确文件名保证普通 tar.gz 不会被误判为 Docker 归档。
    duplicate = tmp_path / "telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz.copy"
    duplicate.write_bytes(b"duplicate")
    manifest = _build(tmp_path)
    assert duplicate.name not in {
        asset["name"] for group in manifest["assets"].values() for asset in group
    }

    (tmp_path / "telegram_bark_monitor-0.1.0-linux-arm64.docker.tar.gz").write_bytes(b"old")
    with pytest.raises(ManifestError, match="Docker 归档版本 0.1.0"):
        _build(tmp_path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="当前平台不支持符号链接")
def test_symlink_asset_is_rejected(tmp_path: Path) -> None:
    _write_python_assets(tmp_path)
    os.symlink(tmp_path / "telegram_bark_monitor-0.1.1.tar.gz", tmp_path / "link.tar.gz")

    with pytest.raises(ManifestError, match="符号链接"):
        _build(tmp_path)


def test_repeated_build_is_byte_for_byte_stable(tmp_path: Path) -> None:
    _write_python_assets(tmp_path)
    first = _build(tmp_path)
    manifest_bytes = (tmp_path / "manifest.json").read_bytes()
    checksum_bytes = (tmp_path / "SHA256SUMS").read_bytes()

    second = _build(tmp_path)

    assert second == first
    assert (tmp_path / "manifest.json").read_bytes() == manifest_bytes
    assert (tmp_path / "SHA256SUMS").read_bytes() == checksum_bytes


def test_release_id_override_and_require_docker(tmp_path: Path) -> None:
    _write_python_assets(tmp_path)
    _write_image_metadata(tmp_path)
    (tmp_path / "telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz").write_bytes(b"amd64")
    (tmp_path / "telegram_bark_monitor-0.1.1-linux-arm64.docker.tar.gz").write_bytes(b"arm64")

    manifest = build_manifest(
        tmp_path,
        "owner/repo",
        "v0.1.1",
        REVISION,
        release_id="123456789",
        require_docker=True,
    )

    assert manifest["releaseId"] == "123456789"


def test_require_docker_rejects_missing_metadata(tmp_path: Path) -> None:
    _write_python_assets(tmp_path)

    with pytest.raises(ManifestError, match="require-docker"):
        build_manifest(tmp_path, "owner/repo", "v0.1.1", REVISION, require_docker=True)


@pytest.mark.parametrize(
    ("tag", "revision", "message"),
    [
        ("v0.1.10", REVISION, "version"),
        ("v0.01.1", REVISION, "version"),
        ("v0.1.1", "abc", "revision"),
        ("v0.1.1", "g" * 40, "revision"),
    ],
)
def test_invalid_version_or_revision_is_rejected(
    tmp_path: Path, tag: str, revision: str, message: str
) -> None:
    _write_python_assets(tmp_path)

    with pytest.raises(ManifestError, match=message):
        _build(tmp_path, tag=tag, revision=revision)


def test_missing_python_distribution_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "telegram_bark_monitor-0.1.1-py3-none-any.whl").write_bytes(b"wheel")

    with pytest.raises(ManifestError, match="恰好有一个当前版本 sdist"):
        _build(tmp_path)
