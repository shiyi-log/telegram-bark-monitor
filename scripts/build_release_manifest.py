#!/usr/bin/env python3
"""为 Bark 发布产物生成 fuwu 可消费的清单和校验摘要。

脚本只处理 ``dist`` 根目录中的发布文件。清单本身和摘要文件会在每次
运行时重新生成，因此重复运行不会把上一次生成的文件加入资产集合。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """发布清单输入或资产不符合约定。"""


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_PROJECT_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_RELEASE_ID_RE = re.compile(r"^[1-9][0-9]*$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARCHIVE_VERSION = r"\d+(?:\.\d+){2}"
_WHEEL_RE = re.compile(
    rf"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)-(?P<version>{_ARCHIVE_VERSION})-(?P<tags>[A-Za-z0-9_.-]+)\.whl$"
)
_SDIST_RE = re.compile(
    rf"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)-(?P<version>{_ARCHIVE_VERSION})\.(?:tar\.gz|zip)$"
)
_DOCKER_ARCHIVE_RE = re.compile(
    rf"^telegram_bark_monitor-(?P<version>{_ARCHIVE_VERSION})-linux-(?P<arch>amd64|arm64)\.docker\.tar\.gz$"
)

_IGNORED_NAMES = frozenset({".gitignore", "manifest.json", "SHA256SUMS"})
_CHUNK_SIZE = 1024 * 1024


def _fail(message: str) -> None:
    raise ManifestError(message)


def _validate_inputs(repository: str, tag: str, revision: str, project: str) -> str:
    """校验并返回标签对应的三段式版本号。"""

    if not _REPOSITORY_RE.fullmatch(repository):
        _fail("repository 必须是 owner/repo 格式")
    tag_match = _TAG_RE.fullmatch(tag)
    if tag_match is None:
        _fail("tag 必须是 vX.Y.Z，且每段版本号为数字")
    version = tag_match.group("version")
    if not _VERSION_RE.fullmatch(version) or any(len(part) != 1 for part in version.split(".")):
        _fail("version 必须是三段且每段恰好一位数字")
    if _REVISION_RE.fullmatch(revision) is None:
        _fail("revision 必须是 40 位十六进制字符串")
    if not _PROJECT_RE.fullmatch(project):
        _fail("project 含有不安全字符")
    return version


def _validate_release_id(release_id: str | None) -> str | None:
    """校验可选的外部 Release ID；缺省时使用可复现的本地标识。"""

    if release_id is None:
        return None
    if _RELEASE_ID_RE.fullmatch(release_id) is None:
        _fail("release-id 含有不安全字符")
    return release_id


def _ensure_dist_directory(dist: Path) -> None:
    """确认 dist 是真实目录，而不是可跟随的符号链接。"""

    try:
        mode = dist.lstat().st_mode
    except FileNotFoundError:
        _fail(f"dist 目录不存在: {dist}")
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        _fail(f"dist 必须是目录且不能是符号链接: {dist}")


def _iter_entries(dist: Path) -> Iterable[Path]:
    """枚举根目录条目，并拒绝任何根级符号链接。"""

    try:
        entries = sorted(dist.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        _fail(f"无法读取 dist: {exc}")
    for path in entries:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            _fail(f"无法检查资产 {path.name}: {exc}")
        if stat.S_ISLNK(mode):
            _fail(f"不允许符号链接资产: {path.name}")
        yield path


def _hash_file(path: Path) -> tuple[int, str]:
    """通过不跟随符号链接的文件描述符计算实际大小和 SHA-256。"""

    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow)
    except OSError as exc:
        _fail(f"无法打开资产 {path.name}: {exc}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail(f"资产必须是普通文件: {path.name}")
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=True) as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()
    except Exception:
        # fdopen 成功后负责关闭 fd；如果在 fdopen 前失败，显式关闭仍然安全。
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _asset(path: Path, *, format_name: str | None = None) -> dict[str, Any]:
    """读取单个根级资产的稳定元数据。"""

    if path.name != Path(path.name).name or path.name in {"", ".", ".."}:
        _fail(f"资产文件名不安全: {path.name!r}")
    size, digest = _hash_file(path)
    result: dict[str, Any] = {
        "name": path.name,
        "path": path.name,
        "size": size,
        "sha256": digest,
    }
    if format_name is not None:
        result["format"] = format_name
    return result


def _atomic_write(path: Path, data: bytes) -> None:
    """原子写入生成文件，并拒绝覆盖预先存在的符号链接。"""

    try:
        existing_mode = path.lstat().st_mode
    except FileNotFoundError:
        existing_mode = None
    except OSError as exc:
        _fail(f"无法检查输出文件 {path.name}: {exc}")
    if existing_mode is not None and stat.S_ISLNK(existing_mode):
        _fail(f"输出文件不能是符号链接: {path.name}")

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        _fail(f"无法写入 {path.name}: {exc}")
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _validate_metadata(path: Path, repository: str, version: str, revision: str) -> None:
    """校验镜像元数据 JSON 的项目身份、源码和双架构 digest。"""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        _fail(f"无法安全读取 container-image.json: {exc}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _fail("container-image.json 必须是普通文件")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            value = json.loads(handle.read())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"container-image.json 不是有效 JSON: {exc}")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    if not isinstance(value, dict):
        _fail("container-image.json 须为 JSON 对象")
    expected_repository = f"ghcr.io/{repository.lower()}"
    if value.get("schemaVersion") != 1:
        _fail("container-image.json schemaVersion 必须为 1")
    if value.get("repository") != expected_repository:
        _fail("container-image.json repository 与当前 GitHub 仓库不一致")
    if value.get("version") != version:
        _fail("container-image.json version 与当前标签不一致")
    if str(value.get("revision", "")).lower() != revision.lower():
        _fail("container-image.json revision 与当前源码不一致")
    if (
        not isinstance(value.get("digest"), str)
        or _IMAGE_DIGEST_RE.fullmatch(value["digest"]) is None
    ):
        _fail("container-image.json digest 必须是 sha256 十六进制摘要")
    images = value.get("images")
    if not isinstance(images, list) or len(images) != 2:
        _fail("container-image.json images 必须包含 amd64 和 arm64 两个镜像")
    image_digests: dict[str, str] = {}
    for image in images:
        if not isinstance(image, dict):
            _fail("container-image.json images 项必须是对象")
        platform = image.get("platform")
        digest = image.get("digest")
        if platform not in {"linux/amd64", "linux/arm64"}:
            _fail("container-image.json images 含不支持的平台")
        if platform in image_digests:
            _fail(f"container-image.json images 平台重复: {platform}")
        if not isinstance(digest, str) or _IMAGE_DIGEST_RE.fullmatch(digest) is None:
            _fail(f"container-image.json {platform} digest 无效")
        image_digests[platform] = digest
    if set(image_digests) != {"linux/amd64", "linux/arm64"}:
        _fail("container-image.json images 必须同时包含 amd64 和 arm64")


def _collect_assets(
    dist: Path, repository: str, version: str, revision: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """扫描并分类 Python 与 Docker 资产。"""

    wheels: list[Path] = []
    sdists: list[Path] = []
    docker_archives: list[tuple[str, Path]] = []
    docker_arches: set[str] = set()
    docker_metadata: Path | None = None

    for path in _iter_entries(dist):
        if not path.is_file():
            # dist 中的目录不会进入清单，且不需要递归扫描。
            continue
        name = path.name
        if name in _IGNORED_NAMES:
            continue
        if name == "container-image.json":
            if docker_metadata is not None:
                _fail("重复的 container-image.json")
            _validate_metadata(path, repository, version, revision)
            docker_metadata = path
            continue

        docker_match = _DOCKER_ARCHIVE_RE.fullmatch(name)
        if docker_match is not None:
            archive_version = docker_match.group("version")
            if archive_version != version:
                _fail(f"Docker 归档版本 {archive_version} 与当前版本 {version} 不一致: {name}")
            architecture = docker_match.group("arch")
            if architecture in docker_arches:
                _fail(f"同一 Docker 架构存在重复归档: {architecture}")
            docker_arches.add(architecture)
            docker_archives.append((architecture, path))
            continue

        wheel_match = _WHEEL_RE.fullmatch(name)
        if wheel_match is not None:
            archive_version = wheel_match.group("version")
            if archive_version != version:
                _fail(f"wheel 版本 {archive_version} 与当前版本 {version} 不一致: {name}")
            wheels.append(path)
            continue

        sdist_match = _SDIST_RE.fullmatch(name)
        if sdist_match is not None:
            archive_version = sdist_match.group("version")
            if archive_version != version:
                _fail(f"sdist 版本 {archive_version} 与当前版本 {version} 不一致: {name}")
            sdists.append(path)
            continue

        # 允许 README、构建日志等未知普通文件存在，但永远不把它们声明为资产。

    if len(wheels) != 1:
        _fail(f"必须恰好有一个当前版本 wheel，实际为 {len(wheels)} 个")
    if len(sdists) != 1:
        _fail(f"必须恰好有一个当前版本 sdist，实际为 {len(sdists)} 个")
    package_names = {
        _WHEEL_RE.fullmatch(wheels[0].name).group("name"),  # type: ignore[union-attr]
        _SDIST_RE.fullmatch(sdists[0].name).group("name"),  # type: ignore[union-attr]
    }
    normalized_names = {re.sub(r"[-_.]+", "-", name).lower() for name in package_names}
    if len(normalized_names) != 1:
        _fail("wheel 与 sdist 的项目名称不一致")

    python_assets = [_asset(wheels[0]), _asset(sdists[0])]
    python_assets.sort(key=lambda item: item["path"])
    docker_assets = [
        _asset(path, format_name="docker-archive") for _, path in sorted(docker_archives)
    ]
    if docker_metadata is not None:
        docker_assets.append(_asset(docker_metadata, format_name="docker-image-metadata"))
    docker_assets.sort(key=lambda item: item["path"])
    return python_assets, docker_assets


def build_manifest(
    dist: str | os.PathLike[str],
    repository: str,
    tag: str,
    revision: str,
    project: str = "bark",
    release_id: str | None = None,
    require_docker: bool = False,
) -> dict[str, Any]:
    """构建并写出 fuwu v1 发布清单，返回写入的字典。"""

    version = _validate_inputs(repository, tag, revision, project)
    validated_release_id = _validate_release_id(release_id)
    dist_path = Path(dist)
    _ensure_dist_directory(dist_path)
    python_assets, docker_assets = _collect_assets(dist_path, repository, version, revision)
    docker_names = {asset["name"] for asset in docker_assets}
    if require_docker and not {
        "container-image.json",
        f"telegram_bark_monitor-{version}-linux-amd64.docker.tar.gz",
        f"telegram_bark_monitor-{version}-linux-arm64.docker.tar.gz",
    }.issubset(docker_names):
        _fail("--require-docker 要求 container-image.json、amd64 和 arm64 归档全部存在")
    canonical_revision = revision.lower()
    manifest: dict[str, Any] = {
        "project": project,
        "schemaVersion": 1,
        "repository": repository,
        "version": version,
        "tag": tag,
        "revision": canonical_revision,
        "releaseId": validated_release_id or f"{repository}@{canonical_revision}/{tag}",
        "draft": False,
        "prerelease": False,
        "assets": {"python": python_assets, "docker": docker_assets},
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    checksum_assets = sorted(python_assets + docker_assets, key=lambda item: item["path"])
    checksum_bytes = "".join(
        f"{item['sha256']}  {item['path']}\n" for item in checksum_assets
    ).encode("ascii")
    _atomic_write(dist_path / "manifest.json", manifest_bytes)
    _atomic_write(dist_path / "SHA256SUMS", checksum_bytes)
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Bark 的 fuwu 发布清单和 SHA256SUMS")
    parser.add_argument("--dist", default="dist", help="发布资产目录，默认 dist")
    parser.add_argument("--repository", required=True, help="GitHub 仓库，格式 owner/repo")
    parser.add_argument("--tag", required=True, help="发布标签，例如 v0.1.1")
    parser.add_argument("--revision", required=True, help="40 位源码 revision")
    parser.add_argument("--project", default="bark", help="fuwu 项目标识，默认 bark")
    parser.add_argument("--release-id", help="可选的正式 Release ID；省略时使用稳定字符串")
    parser.add_argument(
        "--require-docker",
        action="store_true",
        help="要求 container-image.json 及 amd64/arm64 Docker 归档全部存在",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口；输入不合法时返回 2，不输出敏感信息。"""

    args = _build_parser().parse_args(argv)
    try:
        manifest = build_manifest(
            args.dist,
            args.repository,
            args.tag,
            args.revision,
            args.project,
            release_id=args.release_id,
            require_docker=args.require_docker,
        )
    except ManifestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
