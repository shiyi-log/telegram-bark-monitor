import asyncio
from dataclasses import replace

from bark_monitor.config import Settings
from bark_monitor.service import MonitorService


class FakeClient:
    def __init__(self, *_args):
        self.connected = False
        self.handlers = []
        self.disconnected = asyncio.Event()

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        self.disconnected.clear()

    async def disconnect(self):
        self.connected = False
        self.disconnected.set()

    async def is_user_authorized(self):
        return True

    def add_event_handler(self, callback, event):
        self.handlers.append(callback)

    def remove_event_handler(self, callback):
        self.handlers.remove(callback)

    async def run_until_disconnected(self):
        await self.disconnected.wait()


class FakeNotifier:
    def __init__(self, *_args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


def make_settings(tmp_path):
    return Settings(
        api_id=12345,
        api_hash="fake-api-hash",
        bark_push_url="https://bark.test/fake-key",
        session_name=str(tmp_path / "telegram_session"),
    )


class FakeConfigStore:
    def __init__(self, settings):
        self._settings = settings

    def settings(self):
        return self._settings

    def public(self):
        return {"configured": True}


async def test_restart_unregisters_previous_handler(monkeypatch, tmp_path):
    monkeypatch.setattr("bark_monitor.service.BarkNotifier", FakeNotifier)
    clients = []

    def factory(*args):
        client = FakeClient(*args)
        clients.append(client)
        return client

    service = MonitorService(FakeConfigStore(make_settings(tmp_path)), factory)
    for _ in range(2):
        await service.start()
        await asyncio.sleep(0)
        assert len(clients[-1].handlers) == 1
        await service.stop()
        assert clients[-1].handlers == []
        assert service.status()["listening"] is False
    assert len(clients) == 2


async def test_changed_telegram_configuration_recreates_client(tmp_path):
    service = MonitorService(client_factory=FakeClient)
    settings = make_settings(tmp_path)
    first = await service._ensure_client(settings)
    second = await service._ensure_client(replace(settings, api_id=67890))

    assert second is not first
    assert first.is_connected() is False
    assert second.is_connected() is True
    await service.close()
