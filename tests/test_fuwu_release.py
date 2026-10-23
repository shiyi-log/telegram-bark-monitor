"""fuwu 发布客户端的离线契约测试。"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.fuwu_release import (
    FuwuClient,
    FuwuError,
    HttpResponse,
    UrllibTransport,
    validate_manifest,
)

REVISION = "a" * 40
REPOSITORY = "shiyi-log/telegram-bark-monitor"
WHEEL = {"path": "telegram_bark_monitor-0.1.1-py3-none-any.whl", "size": 11, "sha256": "1" * 64}
SDIST = {"path": "telegram_bark_monitor-0.1.1.tar.gz", "size": 13, "sha256": "2" * 64}


def manifest(**overrides):
    value = {
        "project": "bark",
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "version": "0.1.1",
        "tag": "v0.1.1",
        "revision": REVISION,
        "releaseId": f"{REPOSITORY}@{REVISION}/v0.1.1",
        "draft": False,
        "prerelease": False,
        "assets": {"python": [WHEEL, SDIST], "docker": []},
    }
    value.update(overrides)
    return value


def sums_artifact():
    body = (f"{WHEEL['sha256']}  {WHEEL['path']}\n{SDIST['sha256']}  {SDIST['path']}\n").encode()

    return {
        "name": "SHA256SUMS",
        "key": "releases/bark/0.1.1/SHA256SUMS",
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def published_release(*, revision=REVISION, latest=True, status="published"):
    return {
        "project_id": "bark",
        "version": "0.1.1",
        "channel": "stable",
        "repository": REPOSITORY,
        "tag": "v0.1.1",
        "revision": revision,
        "release_id": f"{REPOSITORY}@{REVISION}/v0.1.1",
        "status": status,
        "latest": latest,
        "objectHeadStatus": "verified",
        "artifacts": [
            {
                "name": WHEEL["path"],
                "key": "releases/bark/0.1.1/" + WHEEL["path"],
                "size": WHEEL["size"],
                "sha256": WHEEL["sha256"],
            },
            {
                "name": SDIST["path"],
                "key": "releases/bark/0.1.1/" + SDIST["path"],
                "size": SDIST["size"],
                "sha256": SDIST["sha256"],
            },
            sums_artifact(),
        ],
    }


class FakeTransport:
    """按顺序返回响应并记录请求；不会接触网络。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body, timeout))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(value, status=200):
    return HttpResponse(status, json.dumps(value).encode())


def client(fake, **kwargs):
    return FuwuClient("https://fuwu.example.test", token="token-canary", transport=fake, **kwargs)


def test_validate_manifest_rejects_bool_schema_and_bad_asset_metadata():
    with pytest.raises(FuwuError, match="schemaVersion"):
        validate_manifest(manifest(schemaVersion=True), project="bark")
    with pytest.raises(FuwuError, match="摘要或大小"):
        validate_manifest(
            manifest(assets={"python": [{**WHEEL, "size": True}], "docker": []}), project="bark"
        )
    with pytest.raises(FuwuError, match="根级"):
        validate_manifest(
            manifest(assets={"python": [{**WHEEL, "path": "../secret"}], "docker": []}),
            project="bark",
        )


@pytest.mark.parametrize(
    "value",
    [
        "http://remote.example",
        "ftp://localhost",
        "http://user:pass@127.0.0.1",
        "https://host/x?token=secret",
        "https://host/x#secret",
    ],
)
def test_base_url_rejects_credentials_query_or_non_loopback_http(value):
    with pytest.raises(FuwuError) as error:
        FuwuClient(value, token="token")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:8000", "http://[::1]:8000", "http://localhost:8000", "https://fuwu.example"],
)
def test_base_url_accepts_https_or_loopback_http(value):
    assert FuwuClient(value, token="token").base_url == value


def test_token_precedes_password_and_login_response_token_is_used():
    fake = FakeTransport(
        [response({"data": {"accessToken": "fresh-token"}}), response({"items": []})]
    )
    c = FuwuClient("https://fuwu.example", username="user", password="password", transport=fake)
    assert c._releases("bark", "stable") == []
    assert fake.requests[0][0] == "POST"
    assert json.loads(fake.requests[0][3]) == {"password": "password", "username": "user"}
    assert fake.requests[1][2]["Authorization"] == "Bearer fresh-token"

    token_fake = FakeTransport([response({"items": []})])
    FuwuClient(
        "https://fuwu.example",
        token="existing",
        username="bad",
        password="bad",
        transport=token_fake,
    )._releases("bark", "stable")
    assert token_fake.requests[0][0] == "GET"
    assert token_fake.requests[0][2]["Authorization"] == "Bearer existing"


def test_register_existing_project_only_checks_and_missing_project_posts_public_metadata():
    existing = FakeTransport([response({"id": "bark", "repository": REPOSITORY, "active": True})])
    c = client(existing)
    c.register_project("bark", {"id": "bark", "name": "Bark", "repository": REPOSITORY})
    assert [r[0] for r in existing.requests] == ["GET"]

    missing = FakeTransport(
        [
            response({"code": "project_not_found"}, 404),
            response({"id": "bark", "repository": REPOSITORY, "active": True}, 201),
        ]
    )
    client(missing).register_project("bark", {"name": "Bark", "repository": REPOSITORY})
    assert [r[0] for r in missing.requests] == ["GET", "POST"]
    assert json.loads(missing.requests[1][3])["id"] == "bark"


def test_publish_does_not_register_project_and_preserves_call_order():
    fake = FakeTransport(
        [
            response({"id": "bark", "repository": REPOSITORY, "active": True}),
            response({"items": []}),
            response({"status": "staged"}, 201),
            response({"status": "published"}, 201),
            response({"items": [published_release()]}),
        ]
    )
    result = client(fake).publish(manifest(), staging_ref="ci-run-1")
    assert result.status == "published"
    assert [r[0] for r in fake.requests] == ["GET", "GET", "POST", "POST", "GET"]
    stage = json.loads(fake.requests[2][3])
    assert stage["project"] == "bark"
    assert stage["repository"] == REPOSITORY
    assert stage["assets"] == [WHEEL["path"], SDIST["path"]]
    assert json.loads(fake.requests[3][3])["staging_ref"] == "ci-run-1"
    assert fake.requests[3][2]["Idempotency-Key"].startswith("bark-")


def test_publish_missing_project_fails_without_post_or_release_lookup():
    fake = FakeTransport([response({"code": "project_not_found"}, 404)])
    with pytest.raises(FuwuError, match="项目不存在"):
        client(fake).publish(manifest())
    assert [r[0] for r in fake.requests] == ["GET"]


def test_published_same_revision_skips_and_conflicting_revision_fails():
    skip = FakeTransport(
        [
            response({"id": "bark", "repository": REPOSITORY, "active": True}),
            response({"items": [published_release()]}),
        ]
    )
    assert client(skip).publish(manifest()).status == "already_published"
    assert [r[0] for r in skip.requests] == ["GET", "GET"]

    conflict = FakeTransport(
        [
            response({"id": "bark", "repository": REPOSITORY, "active": True}),
            response({"items": [published_release(revision="b" * 40)]}),
        ]
    )
    with pytest.raises(FuwuError, match="不同 revision"):
        client(conflict).publish(manifest())


def test_http_error_and_network_error_do_not_reveal_secret():
    secret = "token-secret-canary"
    http = FakeTransport([response({"code": secret, "message": secret}, 500)])
    with pytest.raises(FuwuError) as error:
        client(http)._releases("bark", "stable")
    assert secret not in str(error.value)
    assert "HTTP 500" in str(error.value)

    network = FakeTransport([RuntimeError(secret)])
    with pytest.raises(FuwuError) as error:
        client(network)._releases("bark", "stable")
    assert secret not in str(error.value)


def test_staging_reference_has_random_suffix_and_ci_context(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    first = FuwuClient._staging_ref("0.1.1")
    second = FuwuClient._staging_ref("0.1.1")
    assert first.startswith("bark-0-1-1-123-2-")
    assert first != second


def test_urllib_transport_does_not_follow_redirect():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/leaked")
            self.end_headers()

        def log_message(self, *args):
            # 测试服务不打印包含认证信息的请求。
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        result = UrllibTransport().request(
            "GET", base_url + "/api", {"Authorization": "Bearer token"}, None, 1
        )
        assert result.status == 302
        assert requests == [("/api", "Bearer token")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_register_cli_does_not_accept_secret_arguments(tmp_path: Path, monkeypatch, capsys):
    project_file = tmp_path / "project.json"
    project_file.write_text(
        json.dumps({"name": "Bark", "repository": REPOSITORY}), encoding="utf-8"
    )
    monkeypatch.setenv("FUWU_BASE_URL", "https://fuwu.example")
    monkeypatch.setenv("FUWU_TOKEN", "token-secret")
    # CLI 仅接收公开项目元数据和文件路径。
    from scripts import fuwu_release

    monkeypatch.setattr(
        fuwu_release.FuwuClient,
        "register_project",
        lambda self, project, metadata: {"active": True},
    )
    assert fuwu_release.main(["register", "--project-file", str(project_file)]) == 0
    assert "token-secret" not in capsys.readouterr().out


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), 3601, "30"])
def test_invalid_timeouts_are_rejected(timeout):
    with pytest.raises(FuwuError, match="FUWU_TIMEOUT_SECONDS"):
        FuwuClient("https://fuwu.example", timeout=timeout)


def test_from_env_uses_token_precedence_and_safe_timeout_error(monkeypatch):
    monkeypatch.setenv("FUWU_BASE_URL", "https://fuwu.example")
    monkeypatch.setenv("FUWU_TOKEN", "session-canary")
    monkeypatch.setenv("FUWU_USERNAME", "unused-user")
    monkeypatch.setenv("FUWU_PASSWORD", "unused-password")
    monkeypatch.setenv("FUWU_TIMEOUT_SECONDS", "42.5")
    fake = FakeTransport([response({"items": []})])
    FuwuClient.from_env(transport=fake)._releases("bark", "stable")
    assert fake.requests[0][2]["Authorization"] == "Bearer session-canary"
    assert fake.requests[0][4] == 42.5
    monkeypatch.setenv("FUWU_TIMEOUT_SECONDS", "secret-canary")
    with pytest.raises(FuwuError) as error:
        FuwuClient.from_env(transport=fake)
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize("project", ["bark..evil", "bark-", "../bark", "Bark", ""])
def test_invalid_project_fails_before_network(project):
    fake = FakeTransport([])
    with pytest.raises(FuwuError):
        client(fake).get_project(project)
    assert not fake.requests


@pytest.mark.parametrize(
    "repository,active", [("other/repo", True), (REPOSITORY, False), (REPOSITORY, 1)]
)
def test_project_binding_and_active_are_required(repository, active):
    fake = FakeTransport([response({"id": "bark", "repository": repository, "active": active})])
    with pytest.raises(FuwuError):
        client(fake).publish(manifest())
    assert [request[0] for request in fake.requests] == ["GET"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"id": "other", "name": "Bark", "repository": REPOSITORY},
        {"name": "Bark", "repository": REPOSITORY, "token": "secret-canary"},
        {"name": "Bark", "repository": "../bad"},
    ],
)
def test_register_rejects_mismatched_or_private_metadata(metadata):
    fake = FakeTransport([])
    with pytest.raises(FuwuError) as error:
        client(fake).register_project("bark", metadata)
    assert not fake.requests
    assert "secret-canary" not in str(error.value)


def publish_transport(record=None):
    return FakeTransport(
        [
            response({"id": "bark", "repository": REPOSITORY, "active": True}),
            response({"items": []}),
            response({"status": "staged"}, 201),
            response({"status": "published"}, 201),
            response({"items": [record or published_release()]}),
        ]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("latest", False),
        ("status", "staged"),
        ("objectHeadStatus", "unverified"),
        ("repository", "other/repo"),
        ("channel", "beta"),
        ("release_id", "different"),
    ],
)
def test_post_publish_release_fields_are_verified(field, value):
    record = published_release()
    record[field] = value
    with pytest.raises(FuwuError, match="校验失败"):
        client(publish_transport(record)).publish(manifest())


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "duplicate", "size", "digest", "checksum"]
)
def test_complete_artifact_set_and_digests_are_verified(mutation):
    record = published_release()
    artifacts = record["artifacts"]
    if mutation == "missing":
        artifacts.pop()
    elif mutation == "extra":
        artifacts.append({**artifacts[0], "name": "unexpected.whl"})
    elif mutation == "duplicate":
        artifacts[1] = artifacts[0]
    elif mutation == "size":
        artifacts[0]["size"] += 1
    elif mutation == "digest":
        artifacts[0]["sha256"] = "0" * 64
    else:
        artifacts[2]["sha256"] = "0" * 64
    with pytest.raises(FuwuError, match="资产校验失败"):
        client(publish_transport(record)).publish(manifest())


def test_previously_published_old_version_skips_without_changing_latest():
    fake = FakeTransport(
        [
            response({"id": "bark", "repository": REPOSITORY, "active": True}),
            response({"items": [published_release(latest=False)]}),
        ]
    )
    assert client(fake).publish(manifest()).status == "already_published"
    assert len(fake.requests) == 2


def test_same_manifest_has_stable_idempotency_key_with_different_staging_refs():
    first, second = publish_transport(), publish_transport()
    client(first).publish(manifest(), staging_ref="first-ref")
    client(second).publish(manifest(), staging_ref="second-ref")
    assert first.requests[3][2]["Idempotency-Key"] == second.requests[3][2]["Idempotency-Key"]
    assert (
        json.loads(first.requests[2][3])["staging_ref"]
        != json.loads(second.requests[2][3])["staging_ref"]
    )


@pytest.mark.parametrize(
    "body", [b"secret-canary", b'{"code": ["secret-canary"]}', b'{"code": "forbidden"}']
)
def test_http_error_never_echoes_arbitrary_response_values(body):
    fake = FakeTransport([HttpResponse(403, body)])
    with pytest.raises(FuwuError) as error:
        client(fake)._releases("bark", "stable")
    assert "secret-canary" not in str(error.value)
    assert "HTTP 403" in str(error.value)


def test_injected_safe_error_is_normalized_without_details():
    fake = FakeTransport([FuwuError("secret-canary")])
    with pytest.raises(FuwuError) as error:
        client(fake)._releases("bark", "stable")
    assert error.value.code == "network_error"
    assert "secret-canary" not in str(error.value)


def test_cli_parse_failure_does_not_echo_accidentally_supplied_credentials(capsys):
    from scripts.fuwu_release import main

    with pytest.raises(SystemExit) as result:
        main(["publish", "--manifest", "manifest.json", "--token", "secret-canary"])
    assert result.value.code == 2
    output = capsys.readouterr()
    assert "secret-canary" not in output.err


def test_cli_runtime_error_is_fixed_safe_message(tmp_path, monkeypatch, capsys):
    from scripts import fuwu_release

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")

    def fail():
        raise RuntimeError("secret-canary")

    monkeypatch.setattr(fuwu_release.FuwuClient, "from_env", fail)
    assert fuwu_release.main(["publish", "--manifest", str(path)]) == 1
    output = capsys.readouterr()
    assert "secret-canary" not in output.err
    assert "fuwu 操作失败" in output.err
