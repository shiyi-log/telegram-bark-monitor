"""配置读取与校验。

密钥统一从环境变量读取，不写入源代码；Telegram session 文件也可放在仓库之外。
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    """必需配置缺失或格式错误时抛出的异常。"""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"缺少必需配置项: {name}")
    return value


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} 必须是正整数") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} 必须是正整数")
    return parsed


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} 必须是正数") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f"{name} 必须是正数")
    return parsed


def _validate_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        _ = parsed.port  # 启动时主动拒绝格式错误或越界的端口。
    except ValueError:
        valid = False
    if not valid or any(char.isspace() or ord(char) < 32 for char in value):
        raise ConfigurationError("BARK_PUSH_URL 必须是完整的 http(s) URL") from None
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("BARK_PUSH_URL 不应包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("BARK_PUSH_URL 不应包含 query 或 fragment")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class Settings:
    """监听器运行时配置。"""

    api_id: int
    api_hash: str = field(repr=False)
    bark_push_url: str = field(repr=False)
    phone: str | None = field(default=None, repr=False)
    telegram_password: str | None = field(default=None, repr=False)
    session_name: str = "telegram_session"
    timezone_name: str = "Asia/Shanghai"
    bark_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        phone = env.get("TELEGRAM_PHONE", "").strip() or None
        password = env.get("TELEGRAM_PASSWORD", "") or None
        session_name = env.get("TELEGRAM_SESSION", "telegram_session").strip()
        timezone_name = env.get("APP_TIMEZONE", "Asia/Shanghai").strip()
        if not session_name:
            raise ConfigurationError("TELEGRAM_SESSION 不能为空")
        if not timezone_name:
            raise ConfigurationError("APP_TIMEZONE 不能为空")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError("APP_TIMEZONE 必须是有效的 IANA 时区名称") from None

        return cls(
            api_id=_positive_int(_required(env, "TELEGRAM_API_ID"), "TELEGRAM_API_ID"),
            api_hash=_required(env, "TELEGRAM_API_HASH"),
            bark_push_url=_validate_url(_required(env, "BARK_PUSH_URL")),
            phone=phone,
            telegram_password=password,
            session_name=session_name,
            timezone_name=timezone_name,
            bark_timeout_seconds=_positive_float(
                env.get("BARK_TIMEOUT_SECONDS", "10"), "BARK_TIMEOUT_SECONDS"
            ),
        )

    @property
    def session_path(self) -> Path:
        """返回 session 路径，不创建或修改对应文件。"""

        return Path(self.session_name).expanduser()
