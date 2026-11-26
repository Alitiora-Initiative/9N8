from src.n9n8.autonomous_engine import AutonomousEngine
from src.n9n8.helius import PUMP_FUN_PROGRAM


def test_wallet_transaction_delta_excludes_rent_but_keeps_wrapped_sol_proceeds():
    wallet = "wallet-public-key"
    wsol_account = "wrapped-sol-account"
    token_account = "other-token-account"
    transaction = {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": wallet, "signer": True, "writable": True},
                    {"pubkey": wsol_account, "signer": False, "writable": True},
                    {"pubkey": token_account, "signer": False, "writable": True},
                ],
                "instructions": [
                    {
                        "parsed": {
                            "type": "closeAccount",
                            "info": {"account": wsol_account, "destination": wallet},
                        }
                    },
                    {
                        "parsed": {
                            "type": "closeAccount",
                            "info": {"account": token_account, "destination": wallet},
                        }
                    },
                ],
            }
        },
        "meta": {
            "preBalances": [1_000, 3_039_280, 2_039_280],
            "postBalances": [5_079_560, 0, 0],
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": "So11111111111111111111111111111111111111112",
                    "uiTokenAmount": {"amount": "1000000", "decimals": 9},
                }
            ],
        },
    }

    # Wallet delta is 5,078,560 lamports. Remove rent refunds (2,039,280
    # from each closed account) while retaining 1,000,000 wrapped SOL received.
    assert AutonomousEngine._wallet_transaction_delta(transaction, wallet) == 1_000_000


def test_wallet_transaction_delta_returns_none_without_fee_payer_key():
    transaction = {
        "transaction": {"message": {"accountKeys": [{"pubkey": "someone-else"}]}},
        "meta": {"preBalances": [10], "postBalances": [20]},
    }
    assert AutonomousEngine._wallet_transaction_delta(transaction, "wallet") is None
