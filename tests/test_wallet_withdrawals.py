from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from solders.hash import Hash
from solders.keypair import Keypair

from src.n9n8.wallet import WalletError, WalletManager


class FakeHelius:
    def __init__(self, signature: str):
        self.signature = signature
        self.calls = []

    async def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "getFeeForMessage":
            return {"value": 5_000}
        if method == "sendTransaction":
            return self.signature
        raise AssertionError(f"Unexpected RPC method: {method}")


def withdrawal_manager(balance: int, confirmation_error: Exception | None = None):
    source = Keypair()
    destination = Keypair()
    signature = str(Keypair().pubkey())
    manager = WalletManager.__new__(WalletManager)
    manager.keypair_for = AsyncMock(return_value=source)
    manager.balance = AsyncMock(return_value=balance)
    manager._latest_hash = AsyncMock(return_value=Hash.default())
    manager.helius = FakeHelius(signature)
    manager.confirm = AsyncMock(side_effect=confirmation_error)
    manager.db = SimpleNamespace(
        record_trade_receipt=AsyncMock(),
        set_payout_address=AsyncMock(),
    )
    return manager, source, destination, signature


@pytest.mark.asyncio
async def test_withdrawal_rejects_sending_to_same_wallet_before_rpc():
    manager, source, _destination, _signature = withdrawal_manager(1_000_000)

    with pytest.raises(WalletError, match="different wallet"):
        await manager.withdraw_amount(123, str(source.pubkey()), 100_000)

    assert manager.helius.calls == []
    manager.db.record_trade_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdrawal_refuses_to_sign_when_fee_and_reserve_make_amount_unaffordable():
    manager, _source, destination, _signature = withdrawal_manager(100_000)

    with pytest.raises(WalletError, match="Insufficient available SOL"):
        await manager.withdraw_amount(123, str(destination.pubkey()), 90_000)

    assert [method for method, _ in manager.helius.calls] == ["getFeeForMessage"]
    manager.db.record_trade_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdrawal_unknown_confirmation_returns_signature_without_retry():
    manager, _source, destination, signature = withdrawal_manager(
        1_000_000,
        WalletError("Transaction was submitted but confirmation timed out."),
    )

    with pytest.raises(WalletError, match=signature):
        await manager.withdraw_amount(123, str(destination.pubkey()), 100_000)

    methods = [method for method, _ in manager.helius.calls]
    assert methods == ["getFeeForMessage", "sendTransaction"]
    manager.confirm.assert_awaited_once_with(signature)
    manager.db.record_trade_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_withdrawal_records_receipt_and_network_fee():
    manager, _source, destination, signature = withdrawal_manager(1_000_000)
    manager.confirm = AsyncMock(return_value=None)

    amount, fee, result_signature = await manager.withdraw_amount(
        123, str(destination.pubkey()), 100_000
    )

    assert (amount, fee, result_signature) == (100_000, 5_000, signature)
    manager.db.record_trade_receipt.assert_awaited_once()
    manager.db.set_payout_address.assert_awaited_once_with(123, str(destination.pubkey()))
    assert [method for method, _ in manager.helius.calls] == ["getFeeForMessage", "sendTransaction"]
