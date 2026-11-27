import pytest

from src.n9n8.v2.cache import RedisMarketCache


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}

    async def get(self, name):
        return self.values.get(name)

    async def set(self, name, value, ex):
        self.values[name] = value
        self.expiries[name] = ex

    async def delete(self, *names):
        for name in names:
            self.values.pop(name, None)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_cache_only_accepts_ephemeral_market_and_research_namespaces():
    redis = FakeRedis()
    cache = RedisMarketCache(redis)
    await cache.set_json("market:SOL/USDC:5m", {"close": "200"}, ttl_seconds=20)
    assert await cache.get_json("market:SOL/USDC:5m") == {"close": "200"}
    assert redis.expiries["9n8:market:SOL/USDC:5m"] == 20
    await cache.delete("market:SOL/USDC:5m")
    assert await cache.get_json("market:SOL/USDC:5m") is None
    with pytest.raises(ValueError, match="market: or research:"):
        await cache.set_json("wallet:private-key", {"secret": "not-allowed"})


@pytest.mark.asyncio
async def test_cache_rejects_bad_ttls_nonfinite_json_and_invalid_url():
    cache = RedisMarketCache(FakeRedis())
    with pytest.raises(ValueError, match="ttl_seconds"):
        await cache.set_json("research:test", {"a": 1}, ttl_seconds=0)
    with pytest.raises(ValueError, match="finite JSON"):
        await cache.set_json("market:test", {"nan": float("nan")})
    with pytest.raises(ValueError, match="redis://"):
        RedisMarketCache.from_url("https://cache.example")
