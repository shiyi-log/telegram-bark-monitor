"""Fuwu 公开发布验证器的离线契约与真实 loopback HTTP 测试。"""

from __future__ import annotations

import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from scripts.verify_fuwu_public import (
    HttpResponse,
    PublicVerificationError,
    UrlTransport,
    main,
    verify_public_release,
)

BASE = "https://objects.example/"
REVISION = "0123456789abcdef" * 2 + "01234567"
PREFIX = "releases/bark/v0.1.1/"
LATEST = "releases/bark/stable/latest.json"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class FakeTransport:
    """只接收公开 URL 的简易 fake transport。"""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.requests: list[str] = []

    def get(self, url: str) -> bytes:
        self.requests.append(url)
        key = url.removeprefix(BASE)
        if key not in self.objects:
            return HttpResponse(status=404, headers={}, body=b"server token=private")  # type: ignore[return-value]
        return self.objects[key]


class ApiTransport(FakeTransport):
    """把 Fuwu 匿名 API 路径映射到同一组测试对象。"""

    def get(self, url: str) -> bytes:
        self.requests.append(url)
        path = url.removeprefix(BASE)
        if path == "api/downloads/bark/stable/latest.json":
            key = LATEST
        elif path.startswith("api/downloads/bark/0.1.1/"):
            key = PREFIX + path.rsplit("/", 1)[-1]
        else:
            key = path
        if key not in self.objects:
            return HttpResponse(status=404, headers={}, body=b"server token=private")  # type: ignore[return-value]
        return self.objects[key]


def _bundle(
    *,
    prefix: str = PREFIX,
    latest_key: str = LATEST,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    # 单条 docker 使用 dict，多个 python 使用 list，覆盖真实 Fuwu 输出形式。
    files = {
        "telegram_bark_monitor-0.1.1-linux-amd64.docker.tar.gz": b"docker archive bytes",
        "telegram_bark_monitor-0.1.1-py3-none-any.whl": b"wheel bytes",
        "telegram_bark_monitor-0.1.1.tar.gz": b"source bytes",
    }
    assets = {
        name: {"name": name, "key": prefix + name, "size": len(body), "sha256": _digest(body)}
        for name, body in files.items()
    }
    checksums = "".join(
        f"{_digest(body)}  {name}\n" for name, body in sorted(files.items())
    ).encode()
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "project": "bark",
        "channel": "stable",
        "version": "0.1.1",
        "tag": "v0.1.1",
        "revision": REVISION,
        "repository": "shiyi-log/telegram-bark-monitor",
        "releaseId": 101,
        "draft": False,
        "prerelease": False,
        "checksums": {
            "name": "SHA256SUMS",
            "key": prefix + "SHA256SUMS",
            "sha256": _digest(checksums),
            "entries": {name: _digest(body) for name, body in files.items()},
        },
        "assets": {
            "docker": assets[next(iter(files))],
            "python": [
                item
                for name, item in assets.items()
                if name.endswith((".whl", ".tar.gz")) and "docker" not in name
            ],
        },
    }
    manifest_body = _json(manifest)
    pointer = {
        "schemaVersion": 1,
        "project": "bark",
        "channel": "stable",
        "version": "0.1.1",
        "tag": "v0.1.1",
        "revision": REVISION,
        "releaseId": 101,
        "manifestKey": prefix + "manifest.json",
        "manifestSha256": _digest(manifest_body),
        "immutable": True,
    }
    objects = {prefix + name: body for name, body in files.items()}
    objects.update(
        {
            prefix + "SHA256SUMS": checksums,
            prefix + "manifest.json": manifest_body,
            latest_key: _json(pointer),
        }
    )
    return objects, manifest, pointer


def _save_manifest(
    objects: dict[str, bytes],
    manifest: dict[str, Any],
    pointer: dict[str, Any],
    *,
    latest_key: str = LATEST,
) -> None:
    manifest_body = _json(manifest)
    objects[pointer["manifestKey"]] = manifest_body
    pointer["manifestSha256"] = _digest(manifest_body)
    objects[latest_key] = _json(pointer)


def _verify(
    objects: dict[str, bytes], *, transport: Any | None = None, **kwargs: Any
) -> dict[str, Any]:
    return verify_public_release(
        BASE,
        project="bark",
        channel="stable",
        version="0.1.1",
        revision=REVISION,
        transport=transport or FakeTransport(objects),
        **kwargs,
    )


def test_verifies_latest_manifest_checksums_and_every_asset() -> None:
    objects, _, _ = _bundle()
    transport = FakeTransport(objects)

    result = _verify(objects, transport=transport)

    assert result["status"] == "verified"
    assert result["assetCount"] == 3
    assert result["totalSize"] == sum(
        len(objects[BASELESS])
        for BASELESS in objects
        if not BASELESS.endswith(("json", "SHA256SUMS"))
    )
    assert {item["group"] for item in result["assets"]} == {"docker", "python"}
    assert len(transport.requests) == 6
    assert transport.requests[0] == BASE + LATEST
    assert all(url.startswith(BASE) for url in transport.requests)


def test_custom_latest_key_and_storage_prefix_are_supported() -> None:
    prefix = "tenant/releases/bark/v0.1.1/"
    latest_key = "tenant/releases/bark/stable/latest.json"
    objects, _, _ = _bundle(prefix=prefix, latest_key=latest_key)

    result = _verify(objects, latest_key=latest_key)

    assert result["manifestKey"] == prefix + "manifest.json"
    assert result["latestKey"] == latest_key


def test_fuwu_api_mode_uses_latest_and_versioned_anonymous_routes() -> None:
    objects, _, _ = _bundle()
    transport = ApiTransport(objects)

    result = verify_public_release(
        BASE,
        project="bark",
        channel="stable",
        version="0.1.1",
        revision=REVISION,
        download_mode="api",
        transport=transport,
    )

    assert result["status"] == "verified"
    assert result["downloadMode"] == "api"
    assert result["latestKey"] == "api/downloads/bark/stable/latest.json"
    assert transport.requests[0] == BASE + "api/downloads/bark/stable/latest.json"
    assert all("releases/" not in url for url in transport.requests)
    assert any(url.endswith("/manifest.json") for url in transport.requests)
    assert any(url.endswith("/SHA256SUMS") for url in transport.requests)


def test_api_mode_rejects_a_custom_latest_object_key() -> None:
    objects, _, _ = _bundle()
    with pytest.raises(PublicVerificationError, match="api 模式"):
        verify_public_release(
            BASE,
            project="bark",
            channel="stable",
            version="0.1.1",
            revision=REVISION,
            latest_key="../private/latest.json",
            download_mode="api",
            transport=ApiTransport(objects),
        )


def test_fuwu_api_mode_rejects_beta_before_network_requests() -> None:
    objects, _, _ = _bundle()
    transport = ApiTransport(objects)

    with pytest.raises(PublicVerificationError, match="只支持 stable"):
        verify_public_release(
            BASE,
            project="bark",
            channel="beta",
            version="0.1.1",
            revision=REVISION,
            download_mode="api",
            transport=transport,
        )

    assert transport.requests == []


@pytest.mark.parametrize(
    "base",
    [
        "http://objects.example",
        "https://user:secret@objects.example",
        "https://objects.example?token=secret",
        "https://objects.example/#secret",
        "https://objects.example?",
        "https://objects.example/#",
        "https://objects.example:bad",
        "https://objects.example/../private",
        "https://objects.example/%2e%2e/private",
        "http://127.0.0.1.attacker.example",
        "http://2130706433",
        "https://objects.example/\nsecret",
    ],
)
def test_unsafe_base_url_is_rejected_without_echoing_input(base: str) -> None:
    with pytest.raises(PublicVerificationError) as error:
        verify_public_release(base, project="bark", version="0.1.1", revision=REVISION)
    assert "secret" not in str(error.value)
    assert "objects.example" not in str(error.value)


@pytest.mark.parametrize(
    "key",
    [
        "../private",
        "/private",
        "//attacker.example/x",
        "https://attacker.example/x",
        "x?token=secret",
        "x#secret",
        "x/%2e%2e/y",
        "x\\y",
        "x//y",
        "x/./y",
        "x/..hidden/y",
        "x/.. /y",
    ],
)
def test_unsafe_object_keys_are_rejected_before_download(key: str) -> None:
    objects, _, pointer = _bundle()
    pointer["manifestKey"] = key
    objects[LATEST] = _json(pointer)
    transport = FakeTransport(objects)

    with pytest.raises(PublicVerificationError):
        _verify(objects, transport=transport)
    assert transport.requests == [BASE + LATEST]


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.1.2"),
        ("project", "other"),
        ("channel", "beta"),
        ("revision", "f" * 40),
        ("tag", "v0.1.2"),
        ("immutable", False),
        ("releaseId", True),
    ],
)
def test_pointer_expected_identity_is_checked(field: str, value: Any) -> None:
    objects, _, pointer = _bundle()
    pointer[field] = value
    objects[LATEST] = _json(pointer)

    with pytest.raises(PublicVerificationError, match="latest"):
        _verify(objects)


def test_manifest_digest_must_match_exact_bytes() -> None:
    objects, _, _ = _bundle()
    objects[PREFIX + "manifest.json"] += b"\n"

    with pytest.raises(PublicVerificationError, match="manifestSha256"):
        _verify(objects)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.1.2"),
        ("revision", "f" * 40),
        ("releaseId", 102),
        ("draft", True),
        ("prerelease", True),
        ("channel", "unknown"),
    ],
)
def test_manifest_core_fields_are_checked(field: str, value: Any) -> None:
    objects, manifest, pointer = _bundle()
    manifest[field] = value
    _save_manifest(objects, manifest, pointer)

    with pytest.raises(PublicVerificationError, match="manifest"):
        _verify(objects)


def test_promoted_release_can_retain_original_manifest_channel() -> None:
    objects, manifest, pointer = _bundle()
    # Fuwu 提升通道时复用不可变 manifest，里面可保留源 beta 通道。
    manifest["channel"] = "beta"
    _save_manifest(objects, manifest, pointer)

    assert _verify(objects)["status"] == "verified"


def test_checksums_file_digest_is_verified() -> None:
    objects, _, _ = _bundle()
    objects[PREFIX + "SHA256SUMS"] += b"\n"

    with pytest.raises(PublicVerificationError, match="SHA256SUMS 摘要"):
        _verify(objects)


@pytest.mark.parametrize("kind", ["extra", "missing", "duplicate", "wrong_digest"])
def test_checksum_asset_set_is_exact(kind: str) -> None:
    objects, manifest, pointer = _bundle()
    original = objects[PREFIX + "SHA256SUMS"]
    if kind == "extra":
        body = original + f"{'0' * 64}  extra.bin\n".encode()
    elif kind == "missing":
        body = b"\n".join(original.splitlines()[1:]) + b"\n"
    elif kind == "duplicate":
        body = original + original.splitlines()[0] + b"\n"
    else:
        body = original.replace(original[:64], b"f" * 64, 1)
    manifest["checksums"]["sha256"] = _digest(body)
    objects[PREFIX + "SHA256SUMS"] = body
    _save_manifest(objects, manifest, pointer)

    with pytest.raises(PublicVerificationError, match="SHA256SUMS"):
        _verify(objects)


def test_checksum_manifest_entries_cannot_be_silently_ignored() -> None:
    objects, manifest, pointer = _bundle()
    manifest["checksums"]["entries"] = {}
    _save_manifest(objects, manifest, pointer)

    with pytest.raises(PublicVerificationError, match="checksums"):
        _verify(objects)


@pytest.mark.parametrize(
    "kind", ["wrong_digest", "wrong_size", "longer", "cross_release", "duplicate", "unknown_group"]
)
def test_asset_metadata_and_real_bytes_are_verified(kind: str) -> None:
    objects, manifest, pointer = _bundle()
    asset = manifest["assets"]["docker"]
    if kind == "wrong_digest":
        objects[asset["key"]] = b"different bytes!!!!!"
        asset["size"] = len(objects[asset["key"]])
    elif kind == "wrong_size":
        asset["size"] += 1
    elif kind == "longer":
        objects[asset["key"]] += b"extra"
    elif kind == "cross_release":
        asset["key"] = "releases/other/v0.1.1/" + asset["name"]
    elif kind == "duplicate":
        manifest["assets"]["python"].append(dict(asset))
    else:
        manifest["assets"]["other"] = dict(asset)
    _save_manifest(objects, manifest, pointer)

    with pytest.raises(PublicVerificationError):
        _verify(objects)


def test_fake_transport_redirect_is_rejected_without_loading_its_body() -> None:
    objects, _, _ = _bundle()

    class RedirectTransport(FakeTransport):
        def get(self, url: str) -> HttpResponse:
            return HttpResponse(
                status=200,
                headers={},
                body=b"secret",
                final_url="https://attacker.example/private?token=secret",
            )

    with pytest.raises(PublicVerificationError, match="重定向") as error:
        _verify(objects, transport=RedirectTransport(objects))
    assert "secret" not in str(error.value)


def test_asset_is_hashed_as_stream_and_closed() -> None:
    objects, _, _ = _bundle()
    streams: list[io.BytesIO] = []

    class BoundedStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    class StreamTransport(FakeTransport):
        def get(self, url: str) -> HttpResponse:
            stream = BoundedStream(self.objects[url.removeprefix(BASE)])
            streams.append(stream)
            return HttpResponse(status=200, headers={}, stream=stream)

    assert _verify(objects, transport=StreamTransport(objects))["assetCount"] == 3
    assert streams and all(stream.closed for stream in streams)


def test_network_error_body_and_url_are_not_printed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_get(self, url: str) -> HttpResponse:
        raise OSError("private token=do-not-log https://private.example")

    monkeypatch.setattr(UrlTransport, "get", failed_get)
    result = main(
        ["--base-url", BASE, "--project", "bark", "--version", "0.1.1", "--revision", REVISION]
    )

    assert result == 1
    stderr = capsys.readouterr().err
    assert "公开对象请求失败" in stderr
    assert "do-not-log" not in stderr
    assert "private.example" not in stderr


def test_real_loopback_http_is_anonymous_and_redirects_fail() -> None:
    objects, _, _ = _bundle()
    headers_seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            headers_seen.append(dict(self.headers.items()))
            key = self.path.removeprefix("/")
            if key == "redirect/latest.json":
                self.send_response(302)
                self.send_header("Location", "/" + LATEST)
                self.end_headers()
                return
            body = objects.get(key)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"private response")

        def log_message(self, format, *args) -> None:  # noqa: A002
            # 测试服务不输出可能包含地址的访问日志。
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        result = verify_public_release(base, project="bark", version="0.1.1", revision=REVISION)
        assert result["assetCount"] == 3
        with pytest.raises(PublicVerificationError, match="重定向"):
            verify_public_release(
                base,
                project="bark",
                version="0.1.1",
                revision=REVISION,
                latest_key="redirect/latest.json",
            )
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()
    assert headers_seen
    assert all(
        headers.get("User-Agent") == "telegram-bark-monitor-release-verifier/1"
        for headers in headers_seen
    )
    assert all(
        not any(
            name.lower() in {"authorization", "proxy-authorization", "cookie"} for name in headers
        )
        for headers in headers_seen
    )
