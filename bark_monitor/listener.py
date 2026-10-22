"""Telegram 事件监听与 Bark 转发策略。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from .formatting import build_message_info, should_ignore

logger = logging.getLogger(__name__)


class MessageListener:
    def __init__(
        self,
        notifier: Any,
        timezone_name: str,
        on_message: Callable[..., Any] | None = None,
    ):
        self.notifier = notifier
        self.timezone_name = timezone_name
        self.on_message = on_message

    async def handle(self, event: Any) -> None:
        sender = await event.get_sender()
        if should_ignore(event, sender):
            logger.debug("忽略机器人或自己发出的消息")
            return

        info = build_message_info(event, sender, self.timezone_name)
        logger.info(
            "收到消息: type=%s id=%s media=%s forward=%s reply_to=%s",
            info.chat_type,
            info.message_id,
            info.has_media,
            info.is_forward,
            info.reply_to_id,
        )
        delivered: bool | None = None
        if info.chat_type == "私聊":
            delivered = await self.notifier.send(info.title, info.body)
        if self.on_message is not None:
            result = self.on_message(info, delivered)
            if inspect.isawaitable(result):
                await result

    def register(self, client: Any) -> None:
        from telethon import events

        client.add_event_handler(self.handle, events.NewMessage(incoming=True))

    def unregister(self, client: Any) -> None:
        """监听停止后移除处理器，避免再次启动时重复推送。"""

        remove_handler = getattr(client, "remove_event_handler", None)
        if remove_handler is not None:
            remove_handler(self.handle)
