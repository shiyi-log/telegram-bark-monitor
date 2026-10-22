import logging
import os
import sys
from pathlib import Path
from types import ModuleType

from fastapi.testclient import TestClient

import bark_monitor.dashboard as dashboard_module
from bark_monitor.dashboard import create_app
from bark_monitor.service import ConfigStore


def make_env(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=secret-hash\n"
        "TELEGRAM_PHONE=+12025550123\n"
        "BARK_PUSH_URL=https://api.day.app/device-key\n"
        "TELEGRAM_SESSION=telegram_session\n"
        "APP_TIMEZONE=UTC\n"
        "BARK_TIMEOUT_SECONDS=4\n",
        encoding="utf-8",
    )


def test_health_is_public_and_config_is_redacted(tmp_path):
    config_file = tmp_path / ".env"
    make_env(config_file)
    app = create_app()
    app.state.monitor.config_store = ConfigStore(config_file)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["telegram_api_hash_set"] is True
    assert payload["bark_push_url_set"] is True
    assert "secret-hash" not in response.text
    assert "device-key" not in response.text
    assert "config_file" not in payload


def test_config_update_writes_private_file_without_echoing_secret(tmp_path):
    config_file = tmp_path / "data" / "config.env"
    make_env(config_file)
    app = create_app()
    app.state.monitor.config_store = ConfigStore(config_file)

    with TestClient(app) as client:
        response = client.put(
            "/api/config",
            json={"APP_TIMEZONE": "Asia/Shanghai", "TELEGRAM_API_HASH": "new-secret"},
        )

    assert response.status_code == 200
    assert "new-secret" not in response.text
    assert "APP_TIMEZONE=Asia/Shanghai" in config_file.read_text(encoding="utf-8")
    assert "TELEGRAM_API_HASH=new-secret" in config_file.read_text(encoding="utf-8")
    assert config_file.stat().st_mode & 0o077 == 0


def test_dashboard_token_protects_api_but_not_healthz(monkeypatch, tmp_path):
    config_file = tmp_path / ".env"
    make_env(config_file)
    monkeypatch.setenv("BARK_DASHBOARD_TOKEN", "admin-secret")
    app = create_app()
    app.state.monitor.config_store = ConfigStore(config_file)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/status").status_code == 401
        assert (
            client.get(
                "/api/status",
                headers={"Authorization": "Bearer admin-secret"},
            ).status_code
            == 200
        )


def test_log_endpoint_redacts_url_and_phone(tmp_path):
    config_file = tmp_path / ".env"
    make_env(config_file)
    app = create_app()
    app.state.monitor.config_store = ConfigStore(config_file)
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "POST https://api.day.app/device-key +12025550123",
        (),
        None,
    )
    app.state.log_handler.emit(record)

    with TestClient(app) as client:
        response = client.get("/api/logs")

    assert response.status_code == 200
    text = response.text
    assert "device-key" not in text
    assert "12025550123" not in text
    assert "已隐藏" in text


def test_dashboard_env_file_configures_logging_before_server_start(monkeypatch, tmp_path):
    env_file = tmp_path / "dashboard.env"
    log_file = tmp_path / "logs" / "dashboard.jsonl"
    env_file.write_text(
        f"BARK_LOG_FILE={log_file}\nBARK_LOG_LEVEL=ERROR\n",
        encoding="utf-8",
    )
    for key in ("BARK_LOG_FILE", "BARK_LOG_LEVEL", "BARK_CONFIG_FILE"):
        monkeypatch.delenv(key, raising=False)
    root = logging.getLogger()
    previous_level = root.level
    fake_uvicorn = ModuleType("uvicorn")
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)

    fake_uvicorn.run = fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(dashboard_module, "create_app", lambda: object())

    try:
        assert dashboard_module.main(["--env-file", str(env_file)]) == 0
        assert log_file.is_file()
        assert root.level == logging.ERROR
        assert captured["log_level"] is None
        assert captured["log_config"] is None
        assert captured["access_log"] is False
    finally:
        for key in ("BARK_LOG_FILE", "BARK_LOG_LEVEL", "BARK_CONFIG_FILE"):
            os.environ.pop(key, None)
        for handler in list(root.handlers):
            if getattr(handler, "_bark_monitor_handler", False):
                root.removeHandler(handler)
                handler.close()
            else:
                for log_filter in list(handler.filters):
                    if getattr(log_filter, "_bark_monitor_filter", False):
                        handler.removeFilter(log_filter)
        root.setLevel(previous_level)
