from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


class VercelQueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueMessage:
    message_id: str
    receipt_handle: str
    delivery_count: int
    body: dict


class VercelQueueClient:
    """Small async REST client for Vercel Queues poll mode.

    Messages carry wake-up hints only. Neon remains the authoritative job
    store; a queue delivery must never contain a wallet secret or transaction.
    """

    def __init__(self, region: str = "iad1", timeout_seconds: float = 15):
        self.region = region.strip().lower()
        if not self.region or not self.region.isalnum():
            raise ValueError("VERCEL_QUEUE_REGION must be a valid Vercel region code.")
        self.base_url = f"https://{self.region}.vercel-queue.com/api/v3"
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        allow_empty: bool = False,
    ) -> tuple[int, bytes]:
        if not token:
            raise VercelQueueError("Vercel OIDC token is not available to this runtime.")
        await self.start()
        assert self._session is not None
        merged = {"Authorization": f"Bearer {token}"}
        merged.update(headers or {})
        try:
            async with self._session.request(
                method,
                self.base_url + path,
                headers=merged,
                data=body,
            ) as response:
                data = await response.read()
                if allow_empty and response.status == 204:
                    return response.status, data
                if response.status not in ({200, 201, 202, 204} if allow_empty else {200, 201, 202, 204}):
                    logger.warning("Vercel Queue API returned HTTP %s", response.status)
                    raise VercelQueueError(f"Vercel Queue API request failed (HTTP {response.status}).")
                return response.status, data
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise VercelQueueError(f"Vercel Queue API transport failed: {type(exc).__name__}.") from exc

    async def send(
        self,
        topic: str,
        payload: dict,
        token: str,
        idempotency_key: str,
        retention_seconds: int = 86400,
    ) -> str | None:
        if not idempotency_key:
            raise ValueError("Queue send requires an idempotency key.")
        if not 60 <= int(retention_seconds) <= 86400:
            raise ValueError("Vercel Queue retention must be between 60 and 86400 seconds.")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        status, raw = await self._request(
            "POST",
            f"/topic/{quote(topic, safe='')}",
            token,
            headers={
                "Content-Type": "application/json",
                "Vqs-Retention-Seconds": str(int(retention_seconds)),
                "Vqs-Idempotency-Key": idempotency_key,
            },
            body=body,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if status == 202 and data.get("deferred"):
            logger.warning("Vercel Queue deferred a wake-up message during regional failover.")
        return str(data.get("messageId") or "") or None

    async def receive(
        self,
        topic: str,
        consumer: str,
        token: str,
        limit: int = 10,
        visibility_timeout_seconds: int = 60,
    ) -> list[QueueMessage]:
        limit = max(1, min(int(limit), 10))
        visibility_timeout_seconds = max(0, min(int(visibility_timeout_seconds), 3600))
        status, raw = await self._request(
            "POST",
            f"/topic/{quote(topic, safe='')}/consumer/{quote(consumer, safe='')}",
            token,
            headers={
                "Accept": "application/x-ndjson",
                "Vqs-Max-Messages": str(limit),
                "Vqs-Visibility-Timeout-Seconds": str(visibility_timeout_seconds),
            },
            allow_empty=True,
        )
        if status == 204 or not raw:
            return []
        result: list[QueueMessage] = []
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                body_raw = base64.b64decode(item.get("body") or "", validate=True)
                body = json.loads(body_raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("Queue payload must be a JSON object.")
                result.append(
                    QueueMessage(
                        message_id=str(item["messageId"]),
                        receipt_handle=str(item["receiptHandle"]),
                        delivery_count=int(item.get("deliveryCount") or 1),
                        body=body,
                    )
                )
            except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("Discarding malformed Vercel Queue delivery (%s).", type(exc).__name__)
        return result

    async def acknowledge(
        self,
        topic: str,
        consumer: str,
        receipt_handle: str,
        token: str,
    ) -> None:
        await self._request(
            "DELETE",
            (
                f"/topic/{quote(topic, safe='')}/consumer/{quote(consumer, safe='')}"
                f"/lease/{quote(receipt_handle, safe='')}"
            ),
            token,
            allow_empty=True,
        )
