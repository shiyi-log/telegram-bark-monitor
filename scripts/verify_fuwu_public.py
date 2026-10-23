#!/usr/bin/env python3
"""从公开对象根地址验证 Fuwu 的 latest、manifest、校验和及发布资产。

验证器不需要登录凭据，也不会给任何请求添加 ``Authorization``。对象资产使用
逐块读取计算 SHA-256，适合验证较大的 Docker 归档。错误信息只包含固定的契约
字段，不回显地址、响应正文或任何可能携带秘密的值。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO


class PublicVerificationError(ValueError):
    """公开下载内容不符合 Fuwu 发布契约。"""


@dataclass
class HttpResponse:
    """传输层的最小响应对象；真实响应保留流以避免一次性载入大文件。"""

    status: int
    headers: Mapping[str, str]
    body: bytes | None = None
    stream: BinaryIO | None = None
    final_url: str | None = None

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        """按固定块迭代响应正文。"""
        if self.body is not None:
            for offset in range(0, len(self.body), chunk_size):
                yield self.body[offset : offset + chunk_size]
            return
        if self.stream is None:
            return
        while True:
            chunk = self.stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        """关闭底层流；测试响应没有流时也安全。"""
        if self.stream is not None:
            self.stream.close()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝重定向，避免公开校验被带到另一个主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise PublicVerificationError("公开对象请求不允许重定向")


class UrlTransport:
    """只执行无凭据 GET 的 urllib 传输。"""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout
        # 不继承代理环境中的凭据；公开读取不需要任何身份信息。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

    def get(self, url: str) -> HttpResponse:
        """获取一个对象；调用方负责关闭响应。"""
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json, text/plain, */*"},
            method="GET",
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except PublicVerificationError:
            raise
        except urllib.error.HTTPError as error:
            # 不读取或回显服务端正文，避免把秘密写入终端日志。
            raise PublicVerificationError(f"公开对象请求失败（状态 {error.code}）") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PublicVerificationError("公开对象请求失败") from None
        status = int(getattr(response, "status", response.getcode()))
        return HttpResponse(
            status=status,
            headers=dict(response.headers.items()),
            stream=response,
            final_url=response.geturl(),
        )


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_VERSION_RE = re.compile(r"^(0|[1-9])\.(0|[1-9])\.(0|[1-9])$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+~-]*$")
_CHUNK_SIZE = 1024 * 1024
_METADATA_LIMIT = 16 * 1024 * 1024


def _fail(message: str) -> None:
    raise PublicVerificationError(message)


def _validate_base_url(base_url: str) -> str:
    """校验对象根地址，只允许 HTTPS 或真正的 loopback HTTP。"""
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        _fail("对象根地址无效")
    if any(ord(char) <= 32 or ord(char) == 127 for char in base_url):
        _fail("对象根地址无效")
    try:
        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        _fail("对象根地址无效")
    if not parsed.scheme or not parsed.netloc or not host:
        _fail("对象根地址无效")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "?" in base_url
        or "#" in base_url
    ):
        _fail("对象根地址不得包含凭据、查询参数或片段")
    scheme = parsed.scheme.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if scheme != "https" and not (scheme == "http" and loopback):
        _fail("对象根地址必须使用 HTTPS 或 loopback HTTP")
    # urlsplit 允许空路径；统一为目录形式，确保保留服务的 path 前缀。
    path = parsed.path or "/"
    if path != "/":
        _validate_key(path.strip("/"), field="对象根路径")
        if path.startswith("//") or "//" in path:
            _fail("对象根路径不安全")
    if not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit((scheme, parsed.netloc, path, "", ""))


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        _fail("对象地址端口无效")
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _validate_key(key: Any, *, field: str) -> str:
    """验证 Fuwu 对象 key 为安全的相对路径。"""
    if not isinstance(key, str) or not key or key != key.strip() or "\\" in key:
        _fail(f"{field} 不安全")
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        _fail(f"{field} 不安全")
    parsed = urllib.parse.urlsplit(key)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or key.startswith("/"):
        _fail(f"{field} 不安全")
    parts = key.split("/")
    if any(
        not part
        or part in {".", ".."}
        or part.startswith(".")
        or _SEGMENT_RE.fullmatch(part) is None
        for part in parts
    ):
        _fail(f"{field} 不安全")
    return key


def _object_url(base_url: str, key: str) -> str:
    """在同一 origin 下拼接对象 key；key 已经过相对路径校验。"""
    encoded = urllib.parse.quote(key, safe="/-._:@+~")
    url = base_url + encoded
    if _origin(url) != _origin(base_url):
        _fail("对象 key 不在同一来源")
    return url


def _coerce_response(value: Any) -> HttpResponse:
    """兼容测试 fake transport 的 bytes、tuple 和响应对象。"""
    if isinstance(value, HttpResponse):
        return value
    if isinstance(value, (bytes, bytearray)):
        return HttpResponse(status=200, headers={}, body=bytes(value))
    if isinstance(value, tuple) and len(value) == 2:
        status, body = value
        if isinstance(status, int) and isinstance(body, (bytes, bytearray)):
            return HttpResponse(status=status, headers={}, body=bytes(body))
    status = getattr(value, "status", None)
    body = getattr(value, "body", None)
    if isinstance(status, int) and isinstance(body, (bytes, bytearray)):
        return HttpResponse(status=status, headers=getattr(value, "headers", {}), body=bytes(body))
    _fail("传输层响应格式无效")


def _get_response(transport: Any, url: str) -> HttpResponse:
    try:
        getter: Callable[[str], Any]
        getter = transport.get if hasattr(transport, "get") else transport
        response = _coerce_response(getter(url))
    except PublicVerificationError:
        raise
    except Exception:
        raise PublicVerificationError("公开对象请求失败") from None
    if response.status != 200:
        response.close()
        _fail(f"公开对象请求失败（状态 {response.status}）")
    if response.final_url is not None:
        try:
            if response.final_url != url:
                response.close()
                _fail("公开对象请求不允许重定向")
        except ValueError:
            response.close()
            _fail("公开对象请求不允许重定向")
    return response


def _read_bytes(transport: Any, url: str, *, limit: int = _METADATA_LIMIT) -> bytes:
    response = _get_response(transport, url)
    total = 0
    chunks: list[bytes] = []
    try:
        for chunk in response.iter_chunks(_CHUNK_SIZE):
            if not isinstance(chunk, bytes):
                chunk = bytes(chunk)
            total += len(chunk)
            if total > limit:
                _fail("公开元数据过大")
            chunks.append(chunk)
    except PublicVerificationError:
        raise
    except Exception:
        raise PublicVerificationError("公开对象读取失败") from None
    finally:
        response.close()
    return b"".join(chunks)


def _read_json(
    transport: Any, base_url: str, key: str, *, field: str
) -> tuple[dict[str, Any], bytes]:
    body = _read_bytes(transport, _object_url(base_url, key))
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(f"{field} 不是有效 JSON")
    if not isinstance(value, dict):
        _fail(f"{field} 必须是 JSON 对象")
    return value, body


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 字段，避免不同解析器对发布元数据产生歧义。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("公开 JSON 含重复字段")
        value[key] = item
    return value


def _valid_release_id(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool) and value > 0) or (
        isinstance(value, str) and bool(value.strip())
    )


def _validate_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(f"{field} 不是有效 SHA-256")
    return value.lower()


def _validate_metadata(
    latest: Mapping[str, Any], *, project: str, channel: str, version: str, revision: str
) -> str:
    if latest.get("schemaVersion") != 1:
        _fail("latest schemaVersion 无效")
    checks = {
        "project": project,
        "channel": channel,
        "version": version,
    }
    for field, expected in checks.items():
        if latest.get(field) != expected:
            _fail(f"latest {field} 不匹配")
    if str(latest.get("revision", "")).lower() != revision.lower():
        _fail("latest revision 不匹配")
    if latest.get("tag") != f"v{version}":
        _fail("latest tag 不匹配")
    if latest.get("immutable") is not True:
        _fail("latest immutable 标记无效")
    if not _valid_release_id(latest.get("releaseId")):
        _fail("latest releaseId 无效")
    return _validate_key(latest.get("manifestKey"), field="latest manifestKey")


def _asset_entries(
    manifest: Mapping[str, Any], *, project: str, version: str, revision: str
) -> list[dict[str, Any]]:
    if manifest.get("schemaVersion") != 1:
        _fail("manifest schemaVersion 无效")
    if manifest.get("project") != project or manifest.get("version") != version:
        _fail("manifest 核心字段不匹配")
    if str(manifest.get("revision", "")).lower() != revision.lower():
        _fail("manifest revision 不匹配")
    repository = manifest.get("repository")
    if (
        manifest.get("tag") != f"v{version}"
        or not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
    ):
        _fail("manifest 核心字段无效")
    if (
        not _valid_release_id(manifest.get("releaseId"))
        or manifest.get("draft") is not False
        or manifest.get("prerelease") is not False
    ):
        _fail("manifest 必须是正式发布")
    if manifest.get("channel") not in {"stable", "beta"}:
        _fail("manifest channel 无效")
    groups = manifest.get("assets")
    if not isinstance(groups, Mapping):
        _fail("manifest assets 无效")
    if set(groups) - {"docker", "python"}:
        _fail("manifest assets 含未知分组")
    entries: list[dict[str, Any]] = []
    for group in ("docker", "python"):
        values = groups.get(group, [])
        if isinstance(values, Mapping):
            values = [values]
        if not isinstance(values, list):
            _fail("manifest assets 无效")
        for item in values:
            if not isinstance(item, Mapping):
                _fail("manifest asset 无效")
            name = item.get("name")
            if not isinstance(name, str):
                _fail("manifest asset 名称无效")
            _validate_key(name, field="manifest asset name")
            if "/" in name:
                _fail("manifest asset 名称必须是文件名")
            key_value = item.get("key")
            object_key = item.get("objectKey")
            if key_value is not None and object_key is not None and key_value != object_key:
                _fail("manifest asset key 不一致")
            key = _validate_key(
                key_value if key_value is not None else object_key,
                field="manifest asset key",
            )
            if key.rsplit("/", 1)[-1] != name:
                _fail("manifest asset key 与名称不匹配")
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                _fail("manifest asset size 无效")
            digest = _validate_sha256(item.get("sha256"), field="manifest asset sha256")
            entries.append(
                {"group": group, "name": name, "key": key, "size": size, "sha256": digest}
            )
    if not entries:
        _fail("manifest 没有发布资产")
    names = [entry["name"] for entry in entries]
    keys = [entry["key"] for entry in entries]
    if len(names) != len(set(names)) or len(keys) != len(set(keys)):
        _fail("manifest 资产重复")
    return entries


def _parse_checksums(body: bytes) -> dict[str, str]:
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        _fail("SHA256SUMS 不是 ASCII 文本")
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+(?:\*)?([^\s]+)", line)
        if match is None:
            _fail("SHA256SUMS 格式无效")
        digest, name = match.groups()
        _validate_key(name, field="SHA256SUMS 路径")
        if name in parsed:
            _fail("SHA256SUMS 含重复资产")
        parsed[name] = digest.lower()
    if not parsed:
        _fail("SHA256SUMS 为空")
    return parsed


def verify_public_release(
    base_url: str,
    *,
    project: str,
    channel: str = "stable",
    version: str,
    revision: str,
    latest_key: str | None = None,
    transport: Any | None = None,
) -> dict[str, Any]:
    """验证一个公开 Fuwu 发布，返回可供 CI 使用的无秘密摘要。"""
    base = _validate_base_url(base_url)
    if (
        not isinstance(project, str)
        or _PROJECT_RE.fullmatch(project) is None
        or ".." in project
        or project.endswith((".", "-", "_"))
    ):
        _fail("project 无效")
    if (
        not isinstance(channel, str)
        or _CHANNEL_RE.fullmatch(channel) is None
        or channel not in {"stable", "beta"}
    ):
        _fail("channel 无效")
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        _fail("version 无效")
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        _fail("revision 无效")
    revision = revision.lower()
    latest = _validate_key(
        latest_key or f"releases/{project}/{channel}/latest.json", field="latest key"
    )
    client = transport or UrlTransport()
    pointer, pointer_body = _read_json(client, base, latest, field="latest")
    manifest_key = _validate_metadata(
        pointer, project=project, channel=channel, version=version, revision=revision
    )
    if not manifest_key.endswith(f"/{project}/v{version}/manifest.json"):
        _fail("manifestKey 不属于目标发布")
    immutable_prefix = manifest_key.removesuffix("manifest.json")
    manifest, manifest_body = _read_json(client, base, manifest_key, field="manifest")
    if (
        _validate_sha256(pointer.get("manifestSha256"), field="latest manifestSha256")
        != hashlib.sha256(manifest_body).hexdigest()
    ):
        _fail("manifestSha256 不匹配")
    entries = _asset_entries(manifest, project=project, version=version, revision=revision)
    if manifest.get("releaseId") != pointer.get("releaseId"):
        _fail("manifest releaseId 不匹配")
    if any(entry["key"] != immutable_prefix + entry["name"] for entry in entries):
        _fail("manifest asset key 不属于目标发布")
    checksums = manifest.get("checksums")
    if not isinstance(checksums, Mapping):
        _fail("manifest checksums 无效")
    checksum_key = _validate_key(checksums.get("key"), field="checksums key")
    if checksum_key != immutable_prefix + "SHA256SUMS":
        _fail("checksums key 不属于目标发布")
    if checksums.get("name") != "SHA256SUMS":
        _fail("checksums 名称无效")
    checksum_body = _read_bytes(client, _object_url(base, checksum_key))
    checksum_digest = hashlib.sha256(checksum_body).hexdigest()
    if _validate_sha256(checksums.get("sha256"), field="checksums sha256") != checksum_digest:
        _fail("SHA256SUMS 摘要不匹配")
    parsed_checksums = _parse_checksums(checksum_body)
    declared_entries = checksums.get("entries")
    if not isinstance(declared_entries, Mapping):
        _fail("manifest checksums entries 无效")
    normalized_declared = {
        name: _validate_sha256(value, field="checksums entries sha256")
        for name, value in declared_entries.items()
    }
    if normalized_declared != parsed_checksums:
        _fail("manifest checksums 与 SHA256SUMS 不一致")
    expected = {entry["name"]: entry["sha256"] for entry in entries}
    if parsed_checksums != expected:
        _fail("SHA256SUMS 与 manifest 资产不一致")

    results: list[dict[str, Any]] = []
    total_size = 0
    for entry in entries:
        response = _get_response(client, _object_url(base, entry["key"]))
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in response.iter_chunks(_CHUNK_SIZE):
                if not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                digest.update(chunk)
                size += len(chunk)
                if size > entry["size"]:
                    _fail("发布资产 size 不匹配")
        except PublicVerificationError:
            raise
        except Exception:
            raise PublicVerificationError("公开资产读取失败") from None
        finally:
            response.close()
        actual_digest = digest.hexdigest()
        if size != entry["size"]:
            _fail("发布资产 size 不匹配")
        if actual_digest != entry["sha256"]:
            _fail("发布资产 SHA-256 不匹配")
        total_size += size
        results.append(
            {"group": entry["group"], "name": entry["name"], "size": size, "sha256": actual_digest}
        )

    return {
        "status": "verified",
        "project": project,
        "channel": channel,
        "version": version,
        "revision": revision,
        "latestKey": latest,
        "manifestKey": manifest_key,
        "checksumsKey": checksum_key,
        "assetCount": len(results),
        "totalSize": total_size,
        "assets": results,
        "latestSha256": hashlib.sha256(pointer_body).hexdigest(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 Fuwu 公开发布对象")
    parser.add_argument("--base-url", required=True, help="对象存储公开根地址")
    parser.add_argument("--project", required=True, help="Fuwu 项目 ID")
    parser.add_argument("--channel", default="stable", help="发布通道，默认 stable")
    parser.add_argument("--version", required=True, help="三段式版本，例如 0.1.1")
    parser.add_argument("--revision", required=True, help="40 位 Git revision")
    parser.add_argument("--latest-key", help="latest.json 对象 key，默认按项目和通道推导")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = verify_public_release(
            args.base_url,
            project=args.project,
            channel=args.channel,
            version=args.version,
            revision=args.revision,
            latest_key=args.latest_key,
        )
    except PublicVerificationError as error:
        print(f"验证失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
