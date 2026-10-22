"""应用入口：读取配置并启动 Telegram 到 Bark 的转发任务。"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .auth import login_telegram
from .config import ConfigurationError, Settings
from .listener import MessageListener
from .logging_setup import configure_logging
from .notifier import BarkNotifier

logger = logging.getLogger(__name__)


async def run(settings: Settings | None = None, client: Any = None) -> None:
    settings = settings or Settings.from_env()
    async with BarkNotifier(settings.bark_push_url, settings.bark_timeout_seconds) as notifier:
        if client is None:
            from telethon import TelegramClient

            settings.session_path.parent.mkdir(parents=True, exist_ok=True)
            client = TelegramClient(settings.session_path, settings.api_id, settings.api_hash)

        try:
            listener = MessageListener(notifier, settings.timezone_name)
            await login_telegram(client, settings.phone, settings.telegram_password)
            listener.register(client)
            logger.info("开始监听 Telegram 私聊消息，按 Ctrl+C 停止")
            await client.run_until_disconnected()
        finally:
            # 先断开 Telethon，使待处理事件结束后再关闭 HTTP 客户端。
            await client.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监听 Telegram 私聊并转发到 Bark")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="配置文件路径，默认读取当前目录的 .env；环境变量优先",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="只校验环境变量，不连接 Telegram 或发送通知",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        env_file = args.env_file or Path.cwd() / ".env"
        load_dotenv(env_file, override=False, interpolate=False)
        # 先加载用户指定的 .env，再读取其中的 BARK_LOG_* 设置。
        configure_logging()
        settings = Settings.from_env()
        if args.check_config:
            print("配置有效")
            return 0
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("已停止")
    except ConfigurationError as exc:
        logger.error("配置错误: %s", exc)
        return 1
    except Exception as exc:
        # 异常文本和 traceback 可能包含手机号或密钥 URL，因此只记录异常类型。
        logger.error("程序启动或运行失败，错误类型: %s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
