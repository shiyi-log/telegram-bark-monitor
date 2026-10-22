"""Telegram 监听服务的生命周期、授权状态和运行统计。"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from telethon.errors import SessionPasswordNeededError

from .config import ConfigurationError, Settings
from .listener import MessageListener
from .notifier import BarkNotifier

logger = logging.getLogger(__name__)

ALLOWED_CONFIG_KEYS = {
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "TELEGRAM_PASSWORD",
    "BARK_PUSH_URL",
    "TELEGRAM_SESSION",
    "APP_TIMEZONE",
    "BARK_TIMEOUT_SECONDS",
}


class ServiceError(RuntimeError):
    """向 Web 层报告可理解的服务状态错误。"""


class ConfigStore:
    """以 0600 权限保存配置，环境变量始终优先于文件内容。"""

    def __init__(self, path: Path | None = None):
        configured_path = os.environ.get("BARK_CONFIG_FILE", "").strip()
        data_dir = Path(os.environ.get("BARK_DATA_DIR", ".")).expanduser()
        if path is not None:
            self.path = path
        elif configured_path:
            self.path = Path(configured_path).expanduser()
        else:
            self.path = data_dir / ".env" if data_dir != Path(".") else Path.cwd() / ".env"

    def _file_values(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return {
            key: str(value)
            for key, value in dotenv_values(self.path).items()
            if value is not None and key in ALLOWED_CONFIG_KEYS
        }

    def values(self) -> dict[str, str]:
        values = self._file_values()
        # 进程环境优先，方便 Docker secret、CI 和 systemd 注入配置。
        values.update({key: os.environ[key] for key in ALLOWED_CONFIG_KEYS if key in os.environ})
        return values

    def settings(self) -> Settings:
        return Settings.from_env(self.values())

    def public(self) -> dict[str, Any]:
        values = self.values()
        try:
            settings = Settings.from_env(values)
        except ConfigurationError:
            settings = None
        api_id: int | None = None
        if values.get("TELEGRAM_API_ID", "").isdigit():
            api_id = int(values["TELEGRAM_API_ID"])
        return {
            "configured": settings is not None,
            "telegram_api_id": api_id,
            "telegram_api_hash_set": bool(values.get("TELEGRAM_API_HASH", "")),
            "telegram_phone_set": bool(values.get("TELEGRAM_PHONE", "")),
            "telegram_password_set": bool(values.get("TELEGRAM_PASSWORD", "")),
            "bark_push_url_set": bool(values.get("BARK_PUSH_URL", "")),
            "telegram_session": values.get("TELEGRAM_SESSION", "telegram_session"),
            "app_timezone": values.get("APP_TIMEZONE", "Asia/Shanghai"),
            "bark_timeout_seconds": values.get("BARK_TIMEOUT_SECONDS", "10"),
        }

    def update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(updates) - ALLOWED_CONFIG_KEYS
        if unknown:
            raise ServiceError(f"不支持的配置项: {', '.join(sorted(unknown))}")
        current = self._file_values()
        for key, value in updates.items():
            if value is None:
                continue
            text = str(value)
            if (
                key
                in {
                    "TELEGRAM_API_HASH",
                    "TELEGRAM_PHONE",
                    "TELEGRAM_PASSWORD",
                    "BARK_PUSH_URL",
                }
                and not text
            ):
                # 空字符串代表保留原值，避免前端表单覆盖密钥。
                continue
            current[key] = text
        try:
            environment = {key: os.environ[key] for key in ALLOWED_CONFIG_KEYS if key in os.environ}
            Settings.from_env({**current, **environment})
        except ConfigurationError as exc:
            raise ServiceError(str(exc)) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(
            f"{key}={_quote_dotenv(value)}\n" for key, value in sorted(current.items())
        )
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temporary_name, self.path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
        logger.info("配置已保存: path=%s keys=%s", self.path, ",".join(sorted(updates)))
        return self.public()


def _quote_dotenv(value: str) -> str:
    """安全写入 .env，避免空格、# 和换行破坏配置语法。"""

    if value and all(char.isalnum() or char in "_./:+-@" for char in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class MonitorService:
    """在一个 asyncio 循环中管理 Telegram 客户端和 Bark 客户端。"""

    def __init__(
        self,
        config_store: ConfigStore | None = None,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.config_store = config_store or ConfigStore()
        self.client_factory = client_factory
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._client_settings: tuple[int, str, Path] | None = None
        self._task: asyncio.Task[None] | None = None
        self._phone: str | None = None
        self._auth_state = "未授权"
        self._status = "已停止"
        self._last_error: str | None = None
        self._last_event_at: str | None = None
        self._received = 0
        self._delivered = 0
        self._failed = 0

    def status(self) -> dict[str, Any]:
        client = self._client
        connected = bool(client and client.is_connected())
        return {
            "status": self._status,
            "configured": self.config_store.public()["configured"],
            "connected": connected,
            "authorized": self._auth_state == "已授权",
            "listening": self._status == "运行中",
            "auth_state": self._auth_state,
            "stats": {
                "received": self._received,
                "delivered": self._delivered,
                "failed": self._failed,
            },
            "last_event_at": self._last_event_at,
            "last_error": self._last_error,
        }

    async def _ensure_client(self, settings: Settings) -> Any:
        client_settings = (settings.api_id, settings.api_hash, settings.session_path)
        if self._client is not None and self._client_settings != client_settings:
            if self._task and not self._task.done():
                raise ServiceError("Telegram 连接配置已变更，请先停止监听")
            await self._client.disconnect()
            self._client = None
            self._phone = None
            self._auth_state = "未授权"
        if self._client is None:
            if self.client_factory is None:
                from telethon import TelegramClient

                factory = TelegramClient
            else:
                factory = self.client_factory
            settings.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._client = factory(settings.session_path, settings.api_id, settings.api_hash)
            self._client_settings = client_settings
            session_file = Path(f"{settings.session_path}.session")
            if session_file.exists():
                with suppress(OSError):
                    session_file.chmod(0o600)
        if not self._client.is_connected():
            await self._client.connect()
        return self._client

    async def request_code(self, phone: str | None = None) -> dict[str, Any]:
        async with self._lock:
            settings = self.config_store.settings()
            selected_phone = (phone or settings.phone or "").strip()
            if not selected_phone:
                raise ServiceError("请先填写 TELEGRAM_PHONE")
            client = await self._ensure_client(settings)
            if await client.is_user_authorized():
                self._auth_state = "已授权"
                return self.status()
            await client.send_code_request(selected_phone)
            self._phone = selected_phone
            self._auth_state = "等待验证码"
            logger.info("Telegram 验证码已发送")
            return self.status()

    async def sign_in_code(self, code: str) -> dict[str, Any]:
        async with self._lock:
            if self._client is None or not self._phone:
                raise ServiceError("请先请求 Telegram 验证码")
            if not code.strip():
                raise ServiceError("验证码不能为空")
            try:
                await self._client.sign_in(self._phone, code.strip())
            except SessionPasswordNeededError:
                self._auth_state = "等待二步验证密码"
                logger.info("Telegram 需要二步验证密码")
                return self.status()
            self._auth_state = "已授权"
            logger.info("Telegram 已完成验证码登录")
            return self.status()

    async def sign_in_password(self, password: str) -> dict[str, Any]:
        async with self._lock:
            if self._client is None:
                raise ServiceError("请先请求 Telegram 验证码")
            if not password:
                raise ServiceError("二步验证密码不能为空")
            await self._client.sign_in(password=password)
            self._auth_state = "已授权"
            logger.info("Telegram 已完成二步验证登录")
            return self.status()

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self._task and not self._task.done():
                return self.status()
            settings = self.config_store.settings()
            client = await self._ensure_client(settings)
            if not await client.is_user_authorized():
                self._auth_state = "未授权"
                raise ServiceError("Telegram 尚未授权，请先请求验证码并完成登录")
            self._auth_state = "已授权"
            self._status = "启动中"
            self._last_error = None
            self._task = asyncio.create_task(
                self._run(settings, client), name="bark-telegram-monitor"
            )
            logger.info("监听任务已启动")
            return self.status()

    async def _run(self, settings: Settings, client: Any) -> None:
        async with BarkNotifier(settings.bark_push_url, settings.bark_timeout_seconds) as notifier:
            listener = MessageListener(notifier, settings.timezone_name, self._record_message)
            listener.register(client)
            self._status = "运行中"
            try:
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = type(exc).__name__
                self._status = "错误"
                logger.error("监听任务异常: error_type=%s", type(exc).__name__)
            finally:
                listener.unregister(client)
                if client.is_connected():
                    await client.disconnect()
                if self._status == "运行中":
                    self._status = "已停止"

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            task = self._task
            self._status = "停止中" if task and not task.done() else "已停止"
            if self._client is not None and self._client.is_connected():
                await self._client.disconnect()
            if task and not task.done():
                task.cancel()
        if task and not task.done():
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._client = None
        self._client_settings = None
        self._phone = None
        self._status = "已停止"
        logger.info("监听任务已停止")
        return self.status()

    async def test_bark(self) -> bool:
        settings = self.config_store.settings()
        async with BarkNotifier(settings.bark_push_url, settings.bark_timeout_seconds) as notifier:
            accepted = await notifier.send("Bark 测试通知", "Telegram Bark Monitor 测试成功")
        logger.info("Bark 测试完成: accepted=%s", accepted)
        return accepted

    def _record_message(self, _info: Any, delivered: bool | None = None) -> None:
        self._received += 1
        self._last_event_at = datetime.now(UTC).isoformat()
        if delivered is True:
            self._delivered += 1
        elif delivered is False:
            self._failed += 1

    async def close(self) -> None:
        await self.stop()
