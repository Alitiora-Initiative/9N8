from __future__ import annotations

import json
import re
from typing import Any, Protocol


_KEY = re.compile(r"^[a-zA-Z0-9_./:-]{1,180}$")
_ALLOWED = ("market:", "research:")


class RedisLike(Protocol):
    async def get(self, name: str) -> bytes | str | None: ...
    async def set(self, name: str, value: str, ex: int) -> Any: ...
    async def delete(self, *names: str) -> Any: ...
    async def aclose(self) -> Any: ...


class RedisMarketCache:
    """Optional Redis cache restricted to ephemeral market/research data."""

    def __init__(self, client: RedisLike, *, prefix: str = "9n8:"):
        if not prefix or len(prefix) > 40 or not re.fullmatch(r"[a-zA-Z0-9:_-]+", prefix):
            raise ValueError("Redis key prefix is invalid")
        self._client, self._prefix = client, prefix

    @classmethod
    def from_url(cls, url: str, *, prefix: str = "9n8:") -> "RedisMarketCache":
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("Redis URL must use redis:// or rediss://")
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise RuntimeError("Install the optional Redis dependency profile") from exc
        return cls(Redis.from_url(url, decode_responses=True), prefix=prefix)

    @staticmethod
    def _key(key: str) -> str:
        if not _KEY.fullmatch(key) or not key.startswith(_ALLOWED):
            raise ValueError("Redis cache accepts market: or research: keys only")
        return key

    async def get_json(self, key: str) -> Any | None:
        value = await self._client.get(self._prefix + self._key(key))
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Redis cache entry is not valid JSON") from exc

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int = 15) -> None:
        if isinstance(ttl_seconds, bool) or not 1 <= int(ttl_seconds) <= 86400:
            raise ValueError("ttl_seconds must be between 1 and 86400")
        try:
            encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("cache value must be finite JSON data") from exc
        await self._client.set(self._prefix + self._key(key), encoded, ex=int(ttl_seconds))

    async def delete(self, key: str) -> None:
        await self._client.delete(self._prefix + self._key(key))

    async def close(self) -> None:
        await self._client.aclose()
