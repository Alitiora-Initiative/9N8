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
