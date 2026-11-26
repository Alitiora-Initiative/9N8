import base64
import json

import pytest

from src.n9n8.vercel_queue import VercelQueueClient


@pytest.mark.asyncio
async def test_receive_decodes_ndjson_payload_and_receipt():
    payload = {"kind": "job", "job_id": 17, "telegram_id": 42}
    line = {
        "messageId": "msg_123",
        "receiptHandle": "rh/123",
        "deliveryCount": 2,
        "body": base64.b64encode(json.dumps(payload).encode()).decode(),
    }
    client = VercelQueueClient(region="iad1")

    async def fake_request(*args, **kwargs):
        return 200, (json.dumps(line) + "\n").encode()

    client._request = fake_request
    messages = await client.receive("9n8-worker", "n9n8-daemon", "token", limit=10)

    assert len(messages) == 1
    assert messages[0].message_id == "msg_123"
    assert messages[0].receipt_handle == "rh/123"
    assert messages[0].delivery_count == 2
    assert messages[0].body == payload


@pytest.mark.asyncio
async def test_receive_handles_empty_topic():
    client = VercelQueueClient(region="iad1")

    async def fake_request(*args, **kwargs):
        return 204, b""

    client._request = fake_request
    assert await client.receive("9n8-worker", "n9n8-daemon", "token") == []


@pytest.mark.asyncio
async def test_send_uses_idempotency_and_raw_json_payload():
    client = VercelQueueClient(region="iad1")
    captured = {}

    async def fake_request(method, path, token, **kwargs):
        captured.update(method=method, path=path, token=token, **kwargs)
        return 201, b'{"messageId":"msg_456"}'

    client._request = fake_request
    message_id = await client.send(
        "9n8-worker",
        {"kind": "sync_user", "telegram_id": 42},
        "oidc-token",
        "wake:42:123",
    )

    assert message_id == "msg_456"
    assert captured["method"] == "POST"
    assert captured["path"] == "/topic/9n8-worker"
    assert captured["headers"]["Vqs-Idempotency-Key"] == "wake:42:123"
    assert json.loads(captured["body"]) == {"kind": "sync_user", "telegram_id": 42}


@pytest.mark.asyncio
async def test_acknowledgement_url_encodes_receipt_handle():
    client = VercelQueueClient(region="iad1")
    captured = {}

    async def fake_request(method, path, token, **kwargs):
        captured.update(method=method, path=path, token=token)
        return 204, b""

    client._request = fake_request
    await client.acknowledge("9n8-worker", "n9n8-daemon", "rh/with spaces", "oidc-token")

    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/lease/rh%2Fwith%20spaces")
    assert captured["token"] == "oidc-token"
