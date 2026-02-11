import httpx
import pytest

from src.poller.sender import WebhookSender, WebhookSendError


@pytest.mark.asyncio
async def test_send_success():
    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        sender = WebhookSender(http)
        await sender.send(
            "https://webhook.example.com/hook",
            {"order": "test"},
            webhook_secret="my-secret",
            connection_external_id="abc-123",
        )

    assert captured["headers"]["x-webhook-secret"] == "my-secret"
    assert captured["headers"]["x-odoo-connection-id"] == "abc-123"


@pytest.mark.asyncio
async def test_send_http_error():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(500, text="Internal Server Error")
    )
    async with httpx.AsyncClient(transport=transport) as http:
        sender = WebhookSender(http)
        with pytest.raises(WebhookSendError) as exc_info:
            await sender.send("https://webhook.example.com/hook", {})
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_send_without_secret():
    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        sender = WebhookSender(http)
        await sender.send("https://webhook.example.com/hook", {"test": True})

    assert "x-webhook-secret" not in captured["headers"]


def test_backoff_schedule():
    assert WebhookSender.calculate_next_retry(0) == 30
    assert WebhookSender.calculate_next_retry(1) == 60
    assert WebhookSender.calculate_next_retry(2) == 120
    assert WebhookSender.calculate_next_retry(3) == 240
    assert WebhookSender.calculate_next_retry(4) == 600
    assert WebhookSender.calculate_next_retry(10) == 600  # capped
