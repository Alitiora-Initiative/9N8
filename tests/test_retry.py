import asyncio

import pytest

from connectors.retry import RetryableReadError, retry_read


@pytest.mark.asyncio
async def test_retry_read_retries_transient_failures_with_bounded_attempts():
    calls = 0

    @retry_read(attempts=3, timeout_seconds=1, base_delay_seconds=0.001, max_delay_seconds=0.002)
    async def transient_read():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RetryableReadError("temporary 503")
        return {"price": 123.4}

    result = await transient_read()
    assert result == {"price": 123.4}
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_read_does_not_retry_non_transient_errors():
    calls = 0

    @retry_read(attempts=3, timeout_seconds=1, base_delay_seconds=0.001)
    async def invalid_read():
        nonlocal calls
        calls += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError, match="bad request"):
        await invalid_read()
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_read_applies_outer_timeout():
    calls = 0

    @retry_read(attempts=2, timeout_seconds=0.01, base_delay_seconds=0.001)
    async def slow_read():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)

    with pytest.raises(asyncio.TimeoutError):
        await slow_read()
    assert calls == 2
