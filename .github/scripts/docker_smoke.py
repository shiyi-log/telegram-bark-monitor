"""在隔离的临时数据卷中验证 Docker 管理面板，完全不连接 Telegram/Bark。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key in {"src", "href"} and value and value.startswith("/assets/"):
                self.assets.add(value)


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], check=check, capture_output=True, text=True)
    return result.stdout.strip()


def main() -> None:
    image = sys.argv[1]
    suffix = uuid.uuid4().hex[:12]
    name = f"bark-smoke-{suffix}"
    volume = f"bark-smoke-data-{suffix}"
    try:
        # 使用与 Compose 相同的只读根文件系统和最小权限，并让应用生成持久令牌。
        docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--publish",
            "127.0.0.1::8787",
            "--volume",
            f"{volume}:/data",
            image,
        )
        port = docker("port", name, "8787/tcp").rsplit(":", 1)[1]
        base = [f"http://127.0.0.1:{port}"]

        def request(path: str, token: str = "", payload: dict | None = None) -> bytes:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(payload).encode()
            method = "PUT" if payload is not None else "GET"
            with urlopen(
                Request(base[0] + path, data=data, headers=headers, method=method), timeout=5
            ) as response:
                return response.read()

        def wait_for_health() -> None:
            for _ in range(30):
                try:
                    assert json.loads(request("/healthz")) == {"status": "ok"}
                    return
                except (URLError, HTTPError, ConnectionError):
                    time.sleep(1)
            raise RuntimeError("dashboard health check timed out")

        wait_for_health()
        try:
            request("/api/status")
        except HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("API accepted a request without the dashboard token")

        token = docker("exec", name, "cat", "/data/dashboard.token")
        assert len(token) >= 32
        assert docker("exec", name, "stat", "-c", "%a", "/data/dashboard.token") == "600"
        assert json.loads(request("/api/status", token))["configured"] is False

        parser = AssetParser()
        parser.feed(request("/").decode())
        assert any(asset.endswith(".js") for asset in parser.assets)
        assert any(asset.endswith(".css") for asset in parser.assets)
        for asset in parser.assets:
            assert request(asset)

        # 只保存虚构配置，确认 UI 写入数据卷且重启后生效。
        config = json.loads(
            request(
                "/api/config",
                token,
                {
                    "TELEGRAM_API_ID": "12345",
                    "TELEGRAM_API_HASH": "smoke-fake-hash",
                    "BARK_PUSH_URL": "https://bark.example.test/smoke-fake-key",
                    "APP_TIMEZONE": "UTC",
                },
            )
        )
        assert config["configured"] is True
        assert config["telegram_session"] == "/data/telegram_session"
        assert docker("exec", name, "stat", "-c", "%a", "/data/.env") == "600"
        docker("restart", name)
        port = docker("port", name, "8787/tcp").rsplit(":", 1)[1]
        base[0] = f"http://127.0.0.1:{port}"
        wait_for_health()
        assert docker("exec", name, "cat", "/data/dashboard.token") == token
        config = json.loads(request("/api/config", token))
        assert config["configured"] is True
        assert config["app_timezone"] == "UTC"
        assert config["telegram_session"] == "/data/telegram_session"
        print(
            "Docker smoke passed: health, token auth, static assets, config and restart persistence"
        )
    except Exception:
        print(docker("logs", name, check=False), file=sys.stderr)
        raise
    finally:
        docker("stop", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
