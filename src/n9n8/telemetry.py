from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class TelemetryHub:
    """Per-Telegram-user async fanout for the Mini App WebSocket."""
    def __init__(self, max_queue: int = 100):
        self.max_queue = max_queue
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, telegram_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        async with self._lock:
            self._subscribers[telegram_id].add(queue)
        return queue

    async def unsubscribe(self, telegram_id: int, queue: asyncio.Queue) -> None:
        async with self._lock:
            queues = self._subscribers.get(telegram_id)
            if queues:
                queues.discard(queue)
                if not queues:
                    self._subscribers.pop(telegram_id, None)

    async def publish(self, telegram_id: int, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = tuple(self._subscribers.get(telegram_id, ()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
