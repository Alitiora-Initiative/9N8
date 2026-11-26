from decimal import Decimal

from src.n9n8.launchpad_sniper import LaunchpadSniper, PUMP_INITIAL_REAL_TOKEN_RESERVES


def test_pump_curve_binary_state_decode():
    discriminator = b"12345678"
    virtual_tokens = (1_000_000).to_bytes(8, "little")
    virtual_sol = (2_000_000).to_bytes(8, "little")
    real_tokens = (PUMP_INITIAL_REAL_TOKEN_RESERVES // 2).to_bytes(8, "little")
    real_sol = (500_000).to_bytes(8, "little")
    total_supply = (1_000_000_000).to_bytes(8, "little")
    complete = b"\x00"
    raw = discriminator + virtual_tokens + virtual_sol + real_tokens + real_sol + total_supply + complete

    state = LaunchpadSniper._curve_from_raw("curve-address", raw)
    assert state.virtual_token_reserves == 1_000_000
    assert state.virtual_sol_reserves == 2_000_000
    assert state.complete is False
    assert state.progress_pct == Decimal("50")


def test_pump_curve_progress_is_bounded():
    data = (
        b"12345678"
        + (10).to_bytes(8, "little")
        + (20).to_bytes(8, "little")
        + (PUMP_INITIAL_REAL_TOKEN_RESERVES + 100).to_bytes(8, "little")
        + (30).to_bytes(8, "little")
        + (40).to_bytes(8, "little")
        + b"\x00"
    )
    state = LaunchpadSniper._curve_from_raw("pda", data)
    assert state.progress_pct == Decimal("0")


def test_signature_deduplication_evicts_oldest_without_clearing_all():
    sniper = LaunchpadSniper(helius=None)
    for index in range(20_001):
        sniper._remember_signature(f"signature-{index}")

    assert len(sniper._seen) == 20_000
    assert "signature-0" not in sniper._seen
    assert "signature-1" in sniper._seen
    assert "signature-20000" in sniper._seen


def test_launch_events_are_processed_concurrently_and_duplicates_are_skipped():
    import asyncio

    from src.n9n8.launchpad_sniper import LaunchCandidate

    class FakeHelius:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def logs(self, programs):
            for index in range(12):
                yield {"signature": f"sig-{index}", "slot": index, "logs": []}

        async def transaction(self, signature):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return {"meta": {}}
            finally:
                self.active -= 1

    async def scenario():
        helius = FakeHelius()
        sniper = LaunchpadSniper(helius)
        sniper._parse_transaction = lambda signature, slot, logs, tx: LaunchCandidate(
            f"mint-{signature}", "raydium", signature, slot, slot, None, tuple(logs)
        )
        received = []
        await sniper.run(lambda candidate: _record_candidate(received, candidate))
        return helius, received

    async def runner():
        return await scenario()

    helius, received = asyncio.run(runner())
    assert len(received) == 12
    assert helius.max_active > 1


def test_transaction_lookup_is_retried_before_signature_is_discarded():
    import asyncio

    from src.n9n8.launchpad_sniper import LaunchCandidate

    class FlakyHelius:
        def __init__(self):
            self.calls = 0

        async def logs(self, programs):
            yield {"signature": "retry-me", "slot": 1, "logs": []}

        async def transaction(self, signature):
            self.calls += 1
            return None if self.calls == 1 else {"meta": {}}

    async def scenario():
        helius = FlakyHelius()
        sniper = LaunchpadSniper(helius)
        sniper._parse_transaction = lambda signature, slot, logs, tx: LaunchCandidate(
            "mint-retry", "raydium", signature, slot, slot, None, tuple(logs)
        )
        sniper.TRANSACTION_RETRY_DELAYS = (0, 0, 0)
        received = []
        await sniper.run(lambda candidate: _record_candidate(received, candidate))
        return helius, sniper, received

    helius, sniper, received = asyncio.run(scenario())
    assert helius.calls == 2
    assert len(received) == 1
    assert "retry-me" in sniper._seen


async def _record_candidate(target, candidate):
    target.append(candidate)
