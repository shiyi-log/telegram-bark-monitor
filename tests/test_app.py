import asyncio
import logging
import os
from pathlib import Path

import pytest

import bark_monitor.app as app_module
from bark_monitor.config import Settings


def make_settings(session_name: str) -> Settings:
    return Settings(
        api_id=12345,
        api_hash="hash",
        bark_push_url="https://bark.test/device-key",
        phone="+8613800000000",
        session_name=session_name,
        timezone_name="UTC",
        bark_timeout_seconds=1,
    )


class FakeNotifier:
    instances = []

    def __init__(self, push_url, timeout_seconds):
        self.push_url = push_url
        self.timeout_seconds = timeout_seconds
        self.entered = False
        self.exited = False
        self.__class__.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def send(self, title, body):
        return True


class FakeClient:
    def __init__(self, *, cancel=False, login_error=None):
        self.cancel = cancel
        self.login_error = login_error
        self.connected = False
        self.disconnect_calls = 0
        self.handlers = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    async def is_user_authorized(self):
        if self.login_error:
            raise self.login_error
        return True

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    async def run_until_disconnected(self):
        if self.cancel:
            raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_run_creates_session_parent_registers_incoming_handler_and_disconnects(
    monkeypatch, tmp_path
):
    import telethon

    FakeNotifier.instances.clear()
    client = FakeClient()
    settings = make_settings(str(tmp_path / "nested" / "telegram_session"))
    constructor_calls = []

    def create_client(session_name, api_id, api_hash):
        assert Path(session_name).parent.is_dir()
        constructor_calls.append((Path(session_name), api_id, api_hash))
        return client

    monkeypatch.setattr(app_module, "BarkNotifier", FakeNotifier)
    monkeypatch.setattr(telethon, "TelegramClient", create_client)

    await app_module.run(settings)

    assert constructor_calls == [(Path(settings.session_name), 12345, "hash")]
    assert client.disconnect_calls == 1
    assert len(client.handlers) == 1
    _, event_builder = client.handlers[0]
    assert event_builder.incoming is True
    assert event_builder.outgoing is False
    assert FakeNotifier.instances[0].entered is True
    assert FakeNotifier.instances[0].exited is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_run_disconnects_after_normal_exit_or_cancellation(monkeypatch, tmp_path, cancel):
    FakeNotifier.instances.clear()
    client = FakeClient(cancel=cancel)
    settings = make_settings(str(tmp_path / "session"))
    monkeypatch.setattr(app_module, "BarkNotifier", FakeNotifier)

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await app_module.run(settings, client=client)
    else:
        await app_module.run(settings, client=client)

    assert client.disconnect_calls == 1
    assert FakeNotifier.instances[0].exited is True


@pytest.mark.asyncio
async def test_run_disconnects_when_authorization_fails(monkeypatch, tmp_path):
    client = FakeClient(login_error=RuntimeError("authorization failed"))
    settings = make_settings(str(tmp_path / "session"))
    monkeypatch.setattr(app_module, "BarkNotifier", FakeNotifier)

    with pytest.raises(RuntimeError, match="authorization failed"):
        await app_module.run(settings, client=client)

    assert client.disconnect_calls == 1
    assert client.handlers == []


@pytest.mark.asyncio
async def test_run_disconnects_telegram_before_closing_notifier(monkeypatch, tmp_path):
    client = FakeClient()

    class OrderedNotifier(FakeNotifier):
        async def __aexit__(self, exc_type, exc, traceback):
            assert client.disconnect_calls == 1
            assert client.connected is False
            await super().__aexit__(exc_type, exc, traceback)

    monkeypatch.setattr(app_module, "BarkNotifier", OrderedNotifier)
    await app_module.run(make_settings(str(tmp_path / "session")), client=client)


@pytest.fixture
def isolated_config_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "BARK_PUSH_URL",
        "TELEGRAM_PHONE",
        "TELEGRAM_PASSWORD",
        "TELEGRAM_SESSION",
        "APP_TIMEZONE",
        "BARK_TIMEOUT_SECONDS",
        "BARK_LOG_LEVEL",
        "BARK_LOG_FILE",
        "BARK_LOG_DIR",
        "BARK_LOG_MAX_BYTES",
        "BARK_LOG_BACKUP_COUNT",
        "BARK_LOG_TO_FILE",
        "BARK_DATA_DIR",
    ):
        os.environ.pop(name, None)
    root = logging.getLogger()
    initial_level = root.level
    yield
    # load_dotenv 会直接修改进程环境；清除本测试加载的值，再交由 monkeypatch 恢复原环境。
    for name in (
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "BARK_PUSH_URL",
        "TELEGRAM_PHONE",
        "TELEGRAM_PASSWORD",
        "TELEGRAM_SESSION",
        "APP_TIMEZONE",
        "BARK_TIMEOUT_SECONDS",
        "BARK_LOG_LEVEL",
        "BARK_LOG_FILE",
        "BARK_LOG_DIR",
        "BARK_LOG_MAX_BYTES",
        "BARK_LOG_BACKUP_COUNT",
        "BARK_LOG_TO_FILE",
        "BARK_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for handler in list(root.handlers):
        if getattr(handler, "_bark_monitor_handler", False):
            root.removeHandler(handler)
            handler.close()
    for handler in root.handlers:
        for log_filter in list(handler.filters):
            if getattr(log_filter, "_bark_monitor_filter", False):
                handler.removeFilter(log_filter)
    root.setLevel(initial_level)


def test_check_config_loads_default_env_file_without_network(
    monkeypatch, tmp_path, capsys, isolated_config_environment
):
    (tmp_path / ".env").write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=file-hash\n"
        "BARK_PUSH_URL=https://bark.test/file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        app_module.asyncio,
        "run",
        lambda _: pytest.fail("--check-config must not start Telegram"),
    )

    assert app_module.main(["--check-config"]) == 0
    assert "配置有效" in capsys.readouterr().out


def test_check_config_does_not_override_process_environment(
    monkeypatch, tmp_path, capsys, isolated_config_environment
):
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "TELEGRAM_API_ID=not-an-int\n"
        "TELEGRAM_API_HASH=file-hash\n"
        "BARK_PUSH_URL=https://bark.test/file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "process-hash")
    monkeypatch.setenv("BARK_PUSH_URL", "https://bark.test/process-key")
    monkeypatch.setattr(
        app_module.asyncio,
        "run",
        lambda _: pytest.fail("--check-config must not start Telegram"),
    )

    assert app_module.main(["--env-file", str(env_file), "--check-config"]) == 0
    assert "配置有效" in capsys.readouterr().out


def test_custom_env_file_configures_logging_after_loading_and_process_env_wins(
    monkeypatch, tmp_path, capsys, isolated_config_environment
):
    env_file = tmp_path / "custom.env"
    log_file = tmp_path / "nested" / "app.jsonl"
    env_file.write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=file-hash\n"
        "BARK_PUSH_URL=https://bark.test/file-key\n"
        f"BARK_LOG_FILE={log_file}\n"
        "BARK_LOG_LEVEL=ERROR\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BARK_LOG_LEVEL", "WARNING")

    assert app_module.main(["--env-file", str(env_file), "--check-config"]) == 0

    assert "配置有效" in capsys.readouterr().out
    assert log_file.is_file()
    assert logging.getLogger().level == logging.WARNING


def test_check_config_returns_failure_for_invalid_environment(
    monkeypatch, isolated_config_environment
):
    monkeypatch.setenv("TELEGRAM_API_ID", "not-an-int")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("BARK_PUSH_URL", "https://bark.test/key")

    assert app_module.main(["--check-config"]) == 1
