from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin

import aiohttp

from .vercel_queue import QueueMessage, VercelQueueError

logger = logging.getLogger(__name__)


class WorkerBridgeError(RuntimeError):
    pass


class VercelWorkerBridge:
    """Call the private Vercel API service, which adds its request-scoped OIDC token.

    This avoids assuming a Function-only OIDC request header is exported as a
    long-lived process environment variable inside a Daemon container.
    """

    def __init__(self, api_url: str, shared_secret: str, timeout_seconds: float = 35):
        self.api_url = api_url.rstrip("/") + "/"
        self.shared_secret = shared_secret
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        if not self.api_url.startswith("https://"):
            raise ValueError("N9N8_API_URL must be an HTTPS Vercel service binding URL.")
        if len(shared_secret) < 32:
            raise ValueError("N9N8_WORKER_BRIDGE_SECRET must contain at least 32 characters.")

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, path: str, payload: dict) -> dict:
        await self.start()
        assert self._session is not None
        url = urljoin(self.api_url, path.lstrip("/"))
        try:
            async with self._session.post(
                url,
                json=payload,
                headers={
                    "X-N9N8-Worker-Auth": self.shared_secret,
                    "Content-Type": "application/json",
                },
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    raise WorkerBridgeError(
                        f"Private Vercel API bridge returned HTTP {response.status}."
                    )
                if not isinstance(data, dict):
                    raise WorkerBridgeError("Private Vercel API bridge returned an invalid response.")
                return data
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise WorkerBridgeError(
                f"Private Vercel API bridge transport failed ({type(exc).__name__})."
            ) from exc

    async def receive(self, limit: int = 10) -> list[QueueMessage]:
        data = await self._post("/api/internal/worker/queue/receive", {"limit": max(1, min(int(limit), 10))})
        result: list[QueueMessage] = []
        for item in data.get("messages") or []:
            try:
                result.append(
                    QueueMessage(
                        message_id=str(item["message_id"]),
                        receipt_handle=str(item["receipt_handle"]),
                        delivery_count=int(item.get("delivery_count") or 1),
                        body=dict(item["body"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Dropping malformed worker bridge message (%s).", type(exc).__name__)
        return result

    async def acknowledge(self, receipt_handle: str) -> None:
        await self._post(
            "/api/internal/worker/queue/ack",
            {"receipt_handle": receipt_handle},
        )
