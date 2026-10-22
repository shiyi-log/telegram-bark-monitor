import logging
from pathlib import Path

import pytest

from bark_monitor.logging_setup import SafeRedactionFilter, configure_logging, redact_text


def _owned_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_bark_monitor_handler", False)
    ]


@pytest.fixture(autouse=True)
def remove_logging_setup_handlers():
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_bark_monitor_handler", False):
            root.removeHandler(handler)
            handler.close()
    for handler in root.handlers:
        for log_filter in list(handler.filters):
            if getattr(log_filter, "_bark_monitor_filter", False):
                handler.removeFilter(log_filter)


def test_redaction_hides_url_bearer_phone_and_environment_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_HASH", "api-hash-secret")
    monkeypatch.setenv("TELEGRAM_PASSWORD", "password-secret")
    message = (
        "GET https://bark.example/device-key?token=token-secret "
        "Bearer bearer-secret phone +12025550123 "
        "hash=api-hash-secret password=password-secret body=private-body"
    )

    result = redact_text(message)

    assert "bark.example" not in result
    assert "device-key" not in result
    assert "bearer-secret" not in result
    assert "12025550123" not in result
    assert "api-hash-secret" not in result
    assert "password-secret" not in result
    assert "private-body" not in result
    assert "[已隐藏URL]" in result
    assert "[已隐藏手机号]" in result


def test_filter_hides_telethon_exception_details(monkeypatch):
    monkeypatch.setenv("TELEGRAM_PHONE", "+12025550123")
    record = logging.LogRecord("telethon.network", logging.ERROR, __file__, 1, "连接失败", (), None)
    try:
        raise RuntimeError("phone +12025550123 and session secret")
    except RuntimeError:
        record.exc_info = __import__("sys").exc_info()

    assert SafeRedactionFilter().filter(record)
    assert "phone +12025550123" not in record.getMessage()
    assert "异常详情已隐藏" in (record.exc_text or "")


def test_configured_json_logs_redact_sensitive_values(monkeypatch, tmp_path, capsys):
    log_file = tmp_path / "secure" / "monitor.jsonl"
    monkeypatch.setenv("BARK_LOG_FILE", str(log_file))
    monkeypatch.setenv("TELEGRAM_API_HASH", "api-hash-secret")
    monkeypatch.setenv("TELEGRAM_PHONE", "+12025550123")
    configure_logging()

    logging.getLogger("redaction-test").warning(
        "request=%s authorization=Bearer bearer-secret hash=api-hash-secret phone=%s",
        "https://bark.example/device-key",
        "+12025550123",
    )

    console_line = capsys.readouterr().err.strip().splitlines()[-1]
    file_line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    for line in (console_line, file_line):
        assert '"level":"WARNING"' in line
        assert "bark.example" not in line
        assert "device-key" not in line
        assert "bearer-secret" not in line
        assert "api-hash-secret" not in line
        assert "+12025550123" not in line


def test_configure_logging_is_idempotent_and_rotates(monkeypatch, tmp_path: Path):
    log_file = tmp_path / "logs" / "monitor.log"
    monkeypatch.setenv("BARK_LOG_FILE", str(log_file))
    monkeypatch.setenv("BARK_LOG_MAX_BYTES", "128")
    monkeypatch.setenv("BARK_LOG_BACKUP_COUNT", "2")
    monkeypatch.setenv("BARK_LOG_LEVEL", "INFO")
    configure_logging()
    first = _owned_handlers()
    configure_logging()
    second = _owned_handlers()
    assert len(first) == len(second) == 2
    assert len({id(handler) for handler in second}) == 2

    logger = logging.getLogger("logging-test")
    for index in range(30):
        logger.info("轮转消息 %s: %s", index, "x" * 80)

    assert log_file.exists()
    assert any(log_file.with_name(f"monitor.log.{index}").exists() for index in (1, 2))
    assert len(list(log_file.parent.glob("monitor.log*"))) <= 3
    assert log_file.stat().st_mode & 0o777 == 0o600
    assert log_file.parent.stat().st_mode & 0o777 == 0o700


def test_existing_log_parent_permissions_are_not_changed(monkeypatch, tmp_path: Path):
    existing_parent = tmp_path / "shared"
    existing_parent.mkdir(mode=0o755)
    log_file = existing_parent / "monitor.log"
    monkeypatch.setenv("BARK_LOG_FILE", str(log_file))

    configure_logging()

    assert existing_parent.stat().st_mode & 0o777 == 0o755
    assert log_file.stat().st_mode & 0o777 == 0o600
