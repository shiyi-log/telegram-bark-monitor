import pytest

from bark_monitor.config import ConfigurationError, Settings


def env(**overrides):
    values = {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "hash",
        "BARK_PUSH_URL": "https://api.day.app/device-key",
    }
    values.update(overrides)
    return values


def test_settings_from_env_parses_optional_values():
    settings = Settings.from_env(
        env(
            TELEGRAM_PHONE=" +8613800000000 ",
            TELEGRAM_SESSION="data/telegram",
            APP_TIMEZONE="UTC",
            BARK_TIMEOUT_SECONDS="4.5",
        )
    )
    assert settings.api_id == 12345
    assert settings.phone == "+8613800000000"
    assert settings.session_path.as_posix() == "data/telegram"
    assert settings.bark_timeout_seconds == 4.5


@pytest.mark.parametrize("key", ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "BARK_PUSH_URL"])
def test_required_settings_are_checked(key):
    values = env()
    values[key] = ""
    with pytest.raises(ConfigurationError):
        Settings.from_env(values)


def test_invalid_api_id_and_url_are_rejected():
    with pytest.raises(ConfigurationError):
        Settings.from_env(env(TELEGRAM_API_ID="0"))
    with pytest.raises(ConfigurationError):
        Settings.from_env(env(BARK_PUSH_URL="device-key"))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1", "invalid"])
def test_timeout_must_be_finite_and_positive(value):
    with pytest.raises(ConfigurationError, match="BARK_TIMEOUT_SECONDS"):
        Settings.from_env(env(BARK_TIMEOUT_SECONDS=value))


@pytest.mark.parametrize("value", ["Mars/Base", "/etc/passwd", "", "../UTC"])
def test_invalid_timezone_is_rejected(value):
    with pytest.raises(ConfigurationError, match="APP_TIMEZONE"):
        Settings.from_env(env(APP_TIMEZONE=value))


@pytest.mark.parametrize(
    "value",
    [
        "https://[broken/key",
        "https://bark.test:wrong/key",
        "https://bark.test:99999/key",
        "https://user:password@bark.test/key",
        "https://bark.test/key?title=secret",
        "https://bark.test/key#fragment",
        "https://bark.test/a key",
    ],
)
def test_malformed_or_ambiguous_url_is_rejected_without_echoing_secrets(value):
    with pytest.raises(ConfigurationError, match="BARK_PUSH_URL") as error:
        Settings.from_env(env(BARK_PUSH_URL=value))
    assert value not in str(error.value)


def test_password_preserves_spaces_and_secrets_are_hidden_from_repr():
    settings = Settings.from_env(
        env(
            TELEGRAM_API_HASH="secret-hash",
            TELEGRAM_PASSWORD=" password with spaces ",
            TELEGRAM_PHONE="+8613800000000",
        )
    )
    assert settings.telegram_password == " password with spaces "
    for value in ("secret-hash", "password with spaces", "+8613800000000", "device-key"):
        assert value not in repr(settings)
