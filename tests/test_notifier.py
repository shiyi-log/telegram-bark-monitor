import json
import logging

import httpx
import pytest

from bark_monitor.app import configure_logging
from bark_monitor.notifier import BarkNotifier


@pytest.mark.asyncio
async def test_bark_notifier_posts_json_and_accepts_success():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with BarkNotifier("https://bark.test/key", 4, client) as notifier:
            assert await notifier.send("标题", "内容") is True
        assert not client.is_closed  # An injected client remains caller-owned.

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://bark.test/key"
    assert json.loads(requests[0].content) == {"title": "标题", "body": "内容"}
    assert requests[0].extensions["timeout"]["read"] == 4


@pytest.mark.parametrize(
    "status,payload",
    [
        (200, {"code": 400}),
        (200, {}),
        (200, []),
        (503, {"code": 200}),
        (302, {"code": 200}),
    ],
)
@pytest.mark.asyncio
async def test_bark_notifier_requires_http_and_application_success(status, payload):
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = BarkNotifier("https://bark.test/key", client=client)
        assert await notifier.send("标题", "内容") is False


@pytest.mark.asyncio
async def test_bark_notifier_rejects_non_json_response():
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="<html>proxy</html>"))
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = BarkNotifier("https://bark.test/key", client=client)
        assert await notifier.send("标题", "内容") is False


@pytest.mark.asyncio
async def test_bark_notifier_does_not_log_secret_url(caplog):
    def fail(request):
        raise httpx.ReadTimeout(f"request to {request.url} failed", request=request)

    transport = httpx.MockTransport(fail)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = BarkNotifier("https://bark.test/secret", client=client)
        assert await notifier.send("标题", "内容") is False
    assert "ReadTimeout" in caplog.text
    assert "bark.test" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_httpx_success_log_does_not_expose_device_key(caplog):
    caplog.set_level(logging.INFO)
    configure_logging()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"code": 200}))
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = BarkNotifier("https://bark.test/secret", client=client)
        assert await notifier.send("private-title", "private-body") is True
    assert "Bark" in caplog.text
    for value in ("secret", "bark.test", "private-title", "private-body"):
        assert value not in caplog.text


@pytest.mark.asyncio
async def test_owned_client_is_closed():
    notifier = BarkNotifier("https://bark.test/key")
    async with notifier:
        client = notifier._client
        assert not client.is_closed
    assert client.is_closed
    with pytest.raises(RuntimeError):
        await notifier.send("title", "body")
