import pytest

from src.n9n8.vercel_queue import QueueMessage
from src.n9n8.worker_bridge import VercelWorkerBridge, WorkerBridgeError


@pytest.mark.asyncio
async def test_bridge_receive_decodes_worker_messages():
    bridge = VercelWorkerBridge("https://internal-api.example/", "a" * 64)

    async def fake_post(path, payload):
        assert path == "/api/internal/worker/queue/receive"
        assert payload == {"limit": 10}
        return {
            "messages": [
                {
                    "message_id": "msg_1",
                    "receipt_handle": "rh_1",
                    "delivery_count": 3,
                    "body": {"kind": "job", "job_id": 4},
                }
            ]
        }

    bridge._post = fake_post
    messages = await bridge.receive(limit=99)

    assert messages == [
        QueueMessage(
            message_id="msg_1",
            receipt_handle="rh_1",
            delivery_count=3,
            body={"kind": "job", "job_id": 4},
        )
    ]


@pytest.mark.asyncio
async def test_bridge_acknowledges_receipt_handle():
    bridge = VercelWorkerBridge("https://internal-api.example", "b" * 64)
    captured = {}

    async def fake_post(path, payload):
        captured.update(path=path, payload=payload)
        return {"ok": True}

    bridge._post = fake_post
    await bridge.acknowledge("rh_1")

    assert captured == {
        "path": "/api/internal/worker/queue/ack",
        "payload": {"receipt_handle": "rh_1"},
    }


def test_bridge_rejects_http_and_short_secret():
    with pytest.raises(ValueError):
        VercelWorkerBridge("http://internal-api.example", "a" * 64)
    with pytest.raises(ValueError):
        VercelWorkerBridge("https://internal-api.example", "short")
