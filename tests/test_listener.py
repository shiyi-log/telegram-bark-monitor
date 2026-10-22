from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bark_monitor.listener import MessageListener


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, body):
        self.sent.append((title, body))
        return True


class FakeEvent:
    is_private = True
    is_group = False
    is_channel = False
    out = False

    def __init__(self, sender):
        self.message = SimpleNamespace(
            id=1,
            message="hello",
            date=datetime(2026, 1, 1, tzinfo=UTC),
            peer_id=None,
            media=None,
            forward=None,
            reply_to=None,
            out=False,
        )
        self.sender = sender

    async def get_sender(self):
        return self.sender


@pytest.mark.asyncio
async def test_listener_forwards_private_messages_only():
    notifier = FakeNotifier()
    listener = MessageListener(notifier, "UTC")

    await listener.handle(FakeEvent(SimpleNamespace(username="alice", bot=False)))
    group = FakeEvent(SimpleNamespace(username="bob", bot=False))
    group.is_private = False
    group.is_group = True
    await listener.handle(group)

    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "📨 来自 alice 的私聊"


@pytest.mark.asyncio
async def test_listener_skips_bot_and_outgoing_messages():
    notifier = FakeNotifier()
    listener = MessageListener(notifier, "UTC")

    await listener.handle(FakeEvent(SimpleNamespace(username="bot", bot=True)))
    outgoing = FakeEvent(SimpleNamespace(username="alice", bot=False))
    outgoing.out = True
    await listener.handle(outgoing)

    assert notifier.sent == []
