from datetime import UTC, datetime
from types import SimpleNamespace

from bark_monitor.formatting import (
    build_message_info,
    classify_chat,
    sender_name,
    should_ignore,
)


def make_event(**kwargs):
    message = SimpleNamespace(
        id=7,
        message="你好",
        date=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        peer_id=None,
        media=None,
        forward=None,
        reply_to=None,
        out=False,
    )
    values = {
        "message": message,
        "is_private": True,
        "is_group": False,
        "is_channel": False,
        "out": False,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_classification_prefers_event_flags():
    assert classify_chat(make_event()) == "私聊"
    assert classify_chat(SimpleNamespace(is_group=True)) == "群组"
    assert classify_chat(SimpleNamespace(is_channel=True)) == "频道"


def test_sender_name_handles_missing_fields():
    assert sender_name(SimpleNamespace(username="alice")) == "alice"
    assert sender_name(SimpleNamespace(first_name="张", last_name="三")) == "张 三"
    assert sender_name(None) == "未知用户"


def test_message_info_formats_local_time_and_body():
    info = build_message_info(make_event(), SimpleNamespace(first_name="Alice"), "Asia/Shanghai")
    assert info.title == "📨 来自 Alice 的私聊"
    assert "2026-01-01 08:00:00" in info.body
    assert info.text == "你好"


def test_message_info_truncates_oversized_text():
    event = make_event()
    event.message.message = "x" * 4000
    info = build_message_info(event, SimpleNamespace(first_name="Alice"), "UTC")
    assert info.text.endswith("…[内容已截断]")
    assert len(info.text) < 4000


def test_ignore_bots_and_outgoing_messages():
    assert should_ignore(make_event(), SimpleNamespace(bot=True))
    assert should_ignore(make_event(out=True), SimpleNamespace(bot=False))
    assert not should_ignore(make_event(), SimpleNamespace(bot=False))
