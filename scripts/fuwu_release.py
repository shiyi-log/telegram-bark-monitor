"""通过 fuwu 管理接口发布 Bark 的 GitHub Release。

脚本只依赖标准库。凭据仅从环境读取，错误信息使用固定中文消息；HTTP
传输可注入，便于离线测试。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit

_REVISION = re.compile(r"^[0-9a-fA-F]{40}$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION = re.compile(r"^[0-9]\.[0-9]\.[0-9]$")
_JOB_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_PROJECT_FIELDS = frozenset(
    {"id", "name", "repository", "description", "public_api", "build_instructions"}
)
_SAFE_CODES = frozenset(
    {
        "account_required",
        "authentication_required",
        "unauthorized",
        "forbidden",
        "invalid_credentials",
        "login_throttled",
        "invalid_project",
        "project_not_found",
        "project_inactive",
        "project_repository_mismatch",
        "project_conflict",
        "invalid_repository",
        "invalid_project_request",
        "invalid_channel",
        "invalid_version",
        "invalid_tag",
        "invalid_revision",
        "invalid_manifest",
        "manifest_mismatch",
        "invalid_asset_path",
        "invalid_remote_request",
        "invalid_publish_request",
        "invalid_staging_reference",
        "staging_conflict",
        "staging_not_found",
        "release_conflict",
        "release_not_found",
        "idempotency_conflict",
        "checksum_mismatch",
        "size_mismatch",
        "storage_verification_failed",
        "storage_unavailable",
        "github_api_error",
        "github_download_failed",
        "github_authorization_required",
        "remote_download_failed",
        "remote_asset_unavailable",
        "remote_checksums_missing",
        "remote_checksums_invalid",
        "remote_checksum_mismatch",
        "remote_size_mismatch",
        "invalid_checksums",
        "not_found",
        "method_not_allowed",
        "internal_error",
        "invalid_json",
        "request_too_large",
    }
)
_MAX_RESPONSE = 1_048_576


class FuwuError(RuntimeError):
    """可安全展示的客户端错误。"""

    def __init__(self, message: str, *, code: str = "fuwu_error", status: int | None = None):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """HTTP 响应的最小表示。"""

    status: int
    body: bytes = b""


class Transport(Protocol):
    """可注入的 HTTP 传输接口。"""

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    """禁止代理和自动重定向，避免凭据转发到第三方主机。"""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, new_url):
            return None

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), self._NoRedirect()
        )

    def request(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse:
        try:
            request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(int(response.status), response.read(_MAX_RESPONSE))
        except HTTPError as error:
            try:
                body = error.read(_MAX_RESPONSE)
            except Exception:
                body = b""
            finally:
                error.close()
            return HttpResponse(int(error.code), body)
        except Exception:
            raise FuwuError("无法连接 fuwu 服务", code="network_error") from None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json_body(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise FuwuError("fuwu 响应格式无效", code="invalid_response") from None


def _error_from_response(response: HttpResponse) -> FuwuError:
    code = "http_error"
    try:
        payload = _json_body(response.body)
        candidate = payload.get("code") if isinstance(payload, Mapping) else None
        if isinstance(candidate, str) and candidate in _SAFE_CODES:
            code = candidate
    except FuwuError:
        pass
    if 300 <= response.status < 400:
        code = "redirect_rejected"
    return FuwuError(
        f"fuwu 请求失败：HTTP {response.status}（{code}）", code=code, status=response.status
    )


def _validate_base_url(value: str) -> str:
    """只接受 HTTPS 或明确的本机 loopback HTTP 地址。"""

    if not isinstance(value, str) or not value.strip():
        raise FuwuError("FUWU_BASE_URL 未配置", code="configuration_error")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise FuwuError("FUWU_BASE_URL 无效", code="configuration_error") from None
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        raise FuwuError("FUWU_BASE_URL 无效", code="configuration_error")
    if parsed.scheme == "https":
        return value
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.casefold() == "localhost"
    if parsed.scheme != "http" or not loopback:
        raise FuwuError("FUWU_BASE_URL 必须使用 HTTPS 或 loopback HTTP", code="configuration_error")
    return value


def _project(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", value)
        or ".." in value
        or value.endswith((".", "-", "_"))
    ):
        raise FuwuError("项目标识无效", code="invalid_project")
    return value


def _repository(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 201:
        raise FuwuError("仓库字段无效", code="invalid_repository")
    parts = value.split("/")
    if len(parts) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", part)
        or ".." in part
        or part.endswith(".")
        for part in parts
    ):
        raise FuwuError("仓库字段无效", code="invalid_repository")
    return value.casefold()


def _asset_entries(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """提取带大小和摘要的根级资产，拒绝路径与重复文件名。"""

    assets = manifest.get("assets")
    if not isinstance(assets, Mapping) or set(assets) - {"docker", "python"}:
        raise FuwuError("manifest 资产字段无效", code="invalid_manifest")
    entries: list[Mapping[str, Any]] = []
    for group in ("docker", "python"):
        values = assets.get(group, [])
        if isinstance(values, Mapping):
            values = [values]
        if not isinstance(values, list):
            raise FuwuError("manifest 资产字段无效", code="invalid_manifest")
        for item in values:
            if not isinstance(item, Mapping):
                raise FuwuError("manifest 资产字段无效", code="invalid_manifest")
            name, size, digest = item.get("path"), item.get("size"), item.get("sha256")
            if (
                not isinstance(name, str)
                or not _ASSET_NAME.fullmatch(name)
                or name in {"SHA256SUMS", "manifest.json"}
            ):
                raise FuwuError("manifest 资产必须是根级文件名", code="invalid_manifest")
            if (
                type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)
            ):
                raise FuwuError("manifest 资产摘要或大小无效", code="invalid_manifest")
            entries.append(item)
    names = [entry["path"] for entry in entries]
    if not names or len(names) != len(set(names)):
        raise FuwuError("manifest 资产字段无效", code="invalid_manifest")
    return entries


def validate_manifest(manifest: Any, *, project: str) -> dict[str, Any]:
    """验证由本地构建生成的 fuwu v1 清单。"""

    _project(project)
    if not isinstance(manifest, Mapping):
        raise FuwuError("manifest 必须是 JSON 对象", code="invalid_manifest")
    result = dict(manifest)
    if type(result.get("schemaVersion")) is not int or result["schemaVersion"] != 1:
        raise FuwuError("manifest schemaVersion 必须为 1", code="invalid_manifest")
    repository = _repository(result.get("repository"))
    version, revision = result.get("version"), result.get("revision")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise FuwuError("manifest 版本号必须是三段一位数字", code="invalid_manifest")
    if result.get("tag") != f"v{version}":
        raise FuwuError("manifest tag 与版本号不匹配", code="invalid_manifest")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise FuwuError("manifest revision 无效", code="invalid_manifest")
    release_id = result.get("releaseId")
    if not (
        (type(release_id) is int and release_id > 0)
        or (isinstance(release_id, str) and bool(release_id.strip()))
    ):
        raise FuwuError("manifest releaseId 无效", code="invalid_manifest")
    if result.get("draft") is not False or result.get("prerelease") is not False:
        raise FuwuError("manifest 必须是正式发行", code="invalid_manifest")
    if result.get("project") not in (None, project):
        raise FuwuError("manifest 项目与请求不匹配", code="manifest_mismatch")
    _asset_entries(result)
    result.update(repository=repository, revision=revision.lower())
    return result


def _project_metadata(project: str, metadata: Any) -> dict[str, Any]:
    _project(project)
    if not isinstance(metadata, Mapping) or set(metadata) - _PROJECT_FIELDS:
        raise FuwuError("项目文件字段无效", code="invalid_input")
    if metadata.get("id", project) != project:
        raise FuwuError("项目文件标识不匹配", code="invalid_project")
    result = dict(metadata)
    result.update(id=project, repository=_repository(result.get("repository")))
    for key, limit in {
        "name": 120,
        "description": 2000,
        "public_api": 12000,
        "build_instructions": 12000,
    }.items():
        value = result.get(key, "")
        if (
            not isinstance(value, str)
            or len(value.strip()) > limit
            or (key == "name" and (not value.strip() or "\n" in value or "\t" in value))
            or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value)
        ):
            raise FuwuError("项目文件字段无效", code="invalid_input")
        result[key] = value.strip()
    return result


def _project_repository(project: str, metadata: Any) -> str:
    """从项目元数据读取绑定仓库，允许已存在项目省略展示字段。"""

    _project(project)
    if not isinstance(metadata, Mapping) or set(metadata) - _PROJECT_FIELDS:
        raise FuwuError("项目文件字段无效", code="invalid_input")
    if metadata.get("id", project) != project:
        raise FuwuError("项目文件标识不匹配", code="invalid_project")
    return _repository(metadata.get("repository"))


@dataclass(frozen=True, slots=True)
class PublishResult:
    """发布或幂等跳过的结果。"""

    status: str
    release: Mapping[str, Any]


class FuwuClient:
    """提供项目登记、远端 staging、发布和结果校验。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 3600
        ):
            raise FuwuError("FUWU_TIMEOUT_SECONDS 无效", code="configuration_error")
        self.base_url = _validate_base_url(base_url)
        self.token = self._validated_token(token) if token else None
        self.username, self.password = username, password
        self.transport, self.timeout = transport or UrllibTransport(), float(timeout)

    @staticmethod
    def _validated_token(token: Any) -> str:
        if (
            not isinstance(token, str)
            or not token
            or any(char.isspace() for char in token)
            or any(ord(char) < 32 or ord(char) >= 127 for char in token)
        ):
            raise FuwuError("fuwu 认证配置无效", code="configuration_error")
        return token

    @classmethod
    def from_env(cls, *, transport: Transport | None = None) -> FuwuClient:
        try:
            timeout = float(os.environ.get("FUWU_TIMEOUT_SECONDS", "30"))
        except (ValueError, TypeError):
            raise FuwuError("FUWU_TIMEOUT_SECONDS 无效", code="configuration_error") from None
        return cls(
            os.environ.get("FUWU_BASE_URL", ""),
            token=os.environ.get("FUWU_TOKEN"),
            username=os.environ.get("FUWU_USERNAME"),
            password=os.environ.get("FUWU_PASSWORD"),
            transport=transport,
            timeout=timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if auth:
            if not self.token:
                self._login()
            headers["Authorization"] = "Bearer " + self.token
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            url += "?" + urlencode(query)
        try:
            payload = _canonical_json(body) if body is not None else None
            if body is not None:
                headers["Content-Type"] = "application/json"
            response = self.transport.request(method, url, headers, payload, self.timeout)
        except Exception:
            raise FuwuError("无法连接 fuwu 服务", code="network_error") from None
        if (
            not isinstance(response, HttpResponse)
            or type(response.status) is not int
            or not 100 <= response.status <= 599
            or not isinstance(response.body, bytes)
        ):
            raise FuwuError("fuwu 响应格式无效", code="invalid_response")
        if not 200 <= response.status < 300:
            raise _error_from_response(response)
        return _json_body(response.body) if response.body else {}

    def _login(self) -> None:
        if (
            not isinstance(self.username, str)
            or not self.username
            or not isinstance(self.password, str)
        ):
            raise FuwuError("未配置 FUWU_TOKEN 或账号密码", code="configuration_error")
        payload = self._request(
            "POST",
            "/api/auth/login",
            body={"username": self.username, "password": self.password},
            auth=False,
        )
        try:
            self.token = self._validated_token(payload["data"]["accessToken"])
        except (KeyError, TypeError, FuwuError):
            raise FuwuError("fuwu 登录响应无效", code="invalid_response") from None

    def get_project(self, project: str) -> Mapping[str, Any] | None:
        _project(project)
        try:
            value = self._request("GET", f"/api/projects/{quote(project, safe='')}")
        except FuwuError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(value, Mapping):
            raise FuwuError("fuwu 项目响应无效", code="invalid_response")
        return value

    @staticmethod
    def _check_project(project: str, record: Mapping[str, Any], repository: str) -> None:
        if record.get("id") != project or record.get("repository") != repository:
            raise FuwuError("fuwu 项目仓库不匹配", code="project_repository_mismatch")
        if record.get("active") is not True:
            raise FuwuError("fuwu 项目已停用或状态无效", code="project_inactive")

    def ensure_existing_project(self, project: str, repository: str) -> Mapping[str, Any]:
        record = self.get_project(project)
        if record is None:
            raise FuwuError(
                "fuwu 项目不存在，请先执行 register", code="project_not_found", status=404
            )
        self._check_project(project, record, repository)
        return record

    def register_project(self, project: str, metadata: Any) -> Mapping[str, Any]:
        """缺失时登记公开元数据，已存在时只核对仓库和启用状态。"""

        repository = _project_repository(project, metadata)
        record = self.get_project(project)
        if record is not None:
            self._check_project(project, record, repository)
            return record
        body = _project_metadata(project, metadata)
        if record is None:
            record = self._request("POST", "/api/projects", body=body)
        if not isinstance(record, Mapping):
            raise FuwuError("fuwu 项目响应无效", code="invalid_response")
        self._check_project(project, record, body["repository"])
        return record

    def _releases(self, project: str, channel: str) -> list[Any]:
        result = self._request(
            "GET", "/api/releases", query={"project": project, "channel": channel}
        )
        if not isinstance(result, Mapping) or not isinstance(result.get("items"), list):
            raise FuwuError("fuwu 发行响应无效", code="invalid_response")
        return result["items"]

    @staticmethod
    def _verify_release(
        record: Mapping[str, Any],
        manifest: Mapping[str, Any],
        project: str,
        channel: str,
        *,
        require_latest: bool,
    ) -> None:
        expected = {
            "project_id": project,
            "channel": channel,
            "repository": manifest["repository"],
            "version": manifest["version"],
            "tag": manifest["tag"],
            "revision": manifest["revision"],
            "release_id": str(manifest["releaseId"]),
            "status": "published",
            "objectHeadStatus": "verified",
        }
        if any(str(record.get(key)) != value for key, value in expected.items()):
            raise FuwuError("fuwu 发布结果校验失败", code="publish_verification_failed")
        if require_latest and record.get("latest") is not True:
            raise FuwuError("fuwu latest 校验失败", code="publish_verification_failed")
        entries = _asset_entries(manifest)
        wanted = {entry["path"]: (entry["size"], entry["sha256"].lower()) for entry in entries}
        sums = "".join(
            f"{digest}  {name}\n" for name, (_, digest) in sorted(wanted.items())
        ).encode()
        wanted["SHA256SUMS"] = (len(sums), hashlib.sha256(sums).hexdigest())
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != len(wanted):
            raise FuwuError("fuwu 发布资产校验失败", code="publish_verification_failed")
        seen: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise FuwuError("fuwu 发布资产校验失败", code="publish_verification_failed")
            name = artifact.get("name")
            if (
                not isinstance(name, str)
                or name not in wanted
                or name in seen
                or type(artifact.get("size")) is not int
                or (artifact["size"], artifact.get("sha256")) != wanted[name]
                or not isinstance(artifact.get("key"), str)
                or not artifact["key"]
            ):
                raise FuwuError("fuwu 发布资产校验失败", code="publish_verification_failed")
            seen.add(name)

    def publish(
        self,
        manifest: Mapping[str, Any],
        *,
        project: str = "bark",
        channel: str = "stable",
        staging_ref: str | None = None,
    ) -> PublishResult:
        """核对项目后远端下载并发布，最后验证完整资产和 latest。"""

        if channel not in {"stable", "beta"}:
            raise FuwuError("发布渠道无效", code="invalid_channel")
        verified = validate_manifest(manifest, project=project)
        self.ensure_existing_project(project, verified["repository"])
        for item in self._releases(project, channel):
            if not isinstance(item, Mapping) or item.get("version") != verified["version"]:
                continue
            if item.get("revision") != verified["revision"]:
                raise FuwuError("同一版本已有不同 revision", code="release_conflict", status=409)
            self._verify_release(item, verified, project, channel, require_latest=False)
            return PublishResult("already_published", item)
        ref = staging_ref or self._staging_ref(verified["version"])
        if not isinstance(ref, str) or not _JOB_REF.fullmatch(ref):
            raise FuwuError("staging_ref 无效", code="invalid_staging_reference")
        assets = _asset_entries(verified)
        self._request(
            "POST",
            "/api/release-storage/stage-remote",
            body={
                "project": project,
                "repository": verified["repository"],
                "tag": verified["tag"],
                "staging_ref": ref,
                "assets": sorted(item["path"] for item in assets),
                "manifest": verified,
            },
        )
        identity = {"project": project, "channel": channel, "manifest": verified}
        key = "bark-" + hashlib.sha256(_canonical_json(identity)).hexdigest()[:48]
        self._request(
            "POST",
            "/api/release-storage/publish",
            body={
                "project": project,
                "channel": channel,
                "version": verified["version"],
                "tag": verified["tag"],
                "revision": verified["revision"],
                "staging_ref": ref,
            },
            idempotency_key=key,
        )
        published = next(
            (
                item
                for item in self._releases(project, channel)
                if isinstance(item, Mapping) and item.get("version") == verified["version"]
            ),
            None,
        )
        if published is None:
            raise FuwuError("fuwu 发布结果缺失", code="publish_verification_failed")
        self._verify_release(published, verified, project, channel, require_latest=True)
        return PublishResult("published", published)

    @staticmethod
    def _staging_ref(version: str) -> str:
        run, attempt = (
            os.environ.get("GITHUB_RUN_ID", "local"),
            os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", run):
            run = "local"
        if not re.fullmatch(r"[0-9]{1,10}", attempt):
            attempt = "1"
        return f"bark-{version.replace('.', '-')}-{run}-{attempt}-{secrets.token_hex(8)}"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise FuwuError("JSON 文件读取失败", code="invalid_input") from None


def main(argv: list[str] | None = None) -> int:
    """命令行入口只输出固定结果，不接受凭据参数。"""

    class SafeArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            # 参数错误不输出原始参数，避免误传的凭据进入日志。
            self.exit(2, "参数无效，请使用 --help 查看用法。\n")

    parser = SafeArgumentParser(description="将 Bark Release 发布到 fuwu")
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=SafeArgumentParser
    )
    register = subparsers.add_parser("register", help="登记或核对 fuwu 项目")
    register.add_argument("--project", default="bark")
    register.add_argument("--project-file", type=Path, required=True)
    publish = subparsers.add_parser("publish", help="远端 staging 并发布 Bark Release")
    publish.add_argument("--project", default="bark")
    publish.add_argument("--channel", choices=("stable", "beta"), default="stable")
    publish.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        client = FuwuClient.from_env()
        if args.command == "register":
            client.register_project(args.project, _read_json(args.project_file))
            print("fuwu 项目已核对或登记")
        else:
            result = client.publish(
                _read_json(args.manifest), project=args.project, channel=args.channel
            )
            print("fuwu 发布已完成" if result.status == "published" else "fuwu 发行已存在，已跳过")
        return 0
    except FuwuError as error:
        print(error.message, file=sys.stderr)
        return 1
    except Exception:
        print("fuwu 操作失败，请检查配置与输入", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
