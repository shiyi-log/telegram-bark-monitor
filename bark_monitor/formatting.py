"""纯函数式的消息分类与格式化工具。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

MAX_NOTIFICATION_TEXT_LENGTH = 3500


@dataclass(frozen=True, slots=True)
class MessageInfo:
    chat_type: str
    sender_name: str
    text: str
    local_time: datetime
    message_id: int | None
    has_media: bool
    is_forward: bool
    reply_to_id: int | None

    @property
    def title(self) -> str:
        return f"📨 来自 {self.sender_name} 的私聊"

    @property
    def body(self) -> str:
        suffix = f"\n\n🕒 {self.local_time:%Y-%m-%d %H:%M:%S}"
        return f"{self.text}{suffix}"


def classify_chat(event: Any, peer: Any = None) -> str:
    """分类 Telethon 事件，同时兼容字段不完整的测试替身。"""

    if getattr(event, "is_private", False):
        return "私聊"
    if getattr(event, "is_group", False):
        return "群组"
    if getattr(event, "is_channel", False):
        return "频道"

    peer_name = type(peer).__name__
    return {"PeerUser": "私聊", "PeerChat": "群组", "PeerChannel": "频道"}.get(peer_name, "未知")


def sender_name(sender: Any) -> str:
    if sender is None:
        return "未知用户"
    username = getattr(sender, "username", None)
    if username:
        return str(username)
    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    full_name = " ".join(part for part in (str(first).strip(), str(last).strip()) if part)
    if full_name:
        return full_name
    sender_id = getattr(sender, "id", None)
    return str(sender_id) if sender_id is not None else "未知用户"


def _message_date(message: Any, timezone_name: str) -> datetime:
    date = getattr(message, "date", None) or datetime.now(UTC)
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return date.astimezone(ZoneInfo(timezone_name))


def build_message_info(event: Any, sender: Any, timezone_name: str) -> MessageInfo:
    message = getattr(event, "message", event)
    reply = getattr(message, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None)
    raw_text = getattr(message, "message", None) or "[非文本消息]"
    if len(raw_text) > MAX_NOTIFICATION_TEXT_LENGTH:
        raw_text = raw_text[:MAX_NOTIFICATION_TEXT_LENGTH] + "…[内容已截断]"
    return MessageInfo(
        chat_type=classify_chat(event, getattr(message, "peer_id", None)),
        sender_name=sender_name(sender),
        text=raw_text,
        local_time=_message_date(message, timezone_name),
        message_id=getattr(message, "id", None),
        has_media=bool(getattr(message, "media", None)),
        is_forward=bool(getattr(message, "forward", None)),
        reply_to_id=reply_id,
    )


def should_ignore(event: Any, sender: Any) -> bool:
    """忽略自己发出的消息和机器人消息。"""

    message = getattr(event, "message", event)
    return bool(getattr(event, "out", False) or getattr(message, "out", False)) or bool(
        getattr(sender, "bot", False)
    )
