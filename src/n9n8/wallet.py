from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

from base58 import b58decode, b58encode
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import Message
from solders.transaction import Transaction

from .database import Database
from .helius import HeliusClient
from .math_engine import to_lamports, usd_to_lamports
from .oracle import PriceOracle

logger = logging.getLogger(__name__)
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")


class WalletError(RuntimeError):
    pass


class WalletManager:
    def __init__(self, helius: HeliusClient, db: Database, oracle: PriceOracle, treasury: str):
        self.helius, self.db, self.oracle = helius, db, oracle
        self.treasury = Pubkey.from_string(treasury)

    @staticmethod
    def generate_keypair() -> tuple[Keypair, str]:
        keypair = Keypair()
        secret_b58 = b58encode(bytes(keypair)).decode("ascii")
        return keypair, secret_b58

    async def create_wallet(self, telegram_id: int) -> tuple[str, str]:
        await self.db.ensure_user(telegram_id)
        existing = await self.db.get_wallet_address(telegram_id)
        if existing:
            raise WalletError("A wallet already exists. Export or use the existing wallet instead.")
        keypair, secret = self.generate_keypair()
        await self.db.save_wallet(telegram_id, str(keypair.pubkey()), secret)
        await self.db.security_event(telegram_id, "wallet_created", "New Solana wallet generated.")
        return str(keypair.pubkey()), secret

    async def keypair_for(self, telegram_id: int) -> Keypair:
        encoded = await self.db.load_secret(telegram_id)
        try:
            key = Keypair.from_bytes(b58decode(encoded))
        except Exception as exc:
            raise WalletError("Stored wallet key failed validation; execution disabled.") from exc
        return key

    async def balance(self, telegram_id: int) -> int:
        address = await self.db.get_wallet_address(telegram_id)
        if not address:
            return 0
        return await self.helius.balance_lamports(address)

    async def _latest_hash(self) -> Hash:
        try:
            return Hash.from_string(await self.helius.recent_blockhash())
        except Exception as exc:
            raise WalletError("Could not obtain a recent Solana blockhash.") from exc

    async def _send(self, telegram_id: int, instructions: list[Instruction]) -> str:
        keypair = await self.keypair_for(telegram_id)
        blockhash = await self._latest_hash()
        try:
            tx = Transaction.new_signed_with_payer(
                instructions, keypair.pubkey(), [keypair], blockhash
            )
            encoded = base64.b64encode(bytes(tx)).decode("ascii")
            signature = await self.helius.rpc(
                "sendTransaction",
                [encoded, {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 3,
                }],
            )
            if not isinstance(signature, str):
                raise WalletError("Solana RPC did not return a transaction signature.")
            await self.confirm(signature)
            return signature
        except WalletError:
            raise
        except Exception as exc:
            raise WalletError(f"Transaction submission failed: {type(exc).__name__}") from exc

    async def confirm(self, signature: str, timeout_seconds: int = 45) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = await self.helius.signature_status(signature)
            if status:
                if status.get("err"):
                    raise WalletError("Transaction failed on-chain; no fee receipt was recorded.")
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return
            await asyncio.sleep(1)
        raise WalletError("Transaction was submitted but confirmation timed out. Check the signature before retrying.")

    async def transfer_usd_fee(self, telegram_id: int, usd: str, fee_type: str, idempotency_key: str) -> tuple[int, str]:
        price = await self.oracle.sol_usd(force=True)
        lamports = usd_to_lamports(usd, price)
        if lamports <= 0:
            raise WalletError("The converted fee is below one lamport.")
        balance = await self.balance(telegram_id)
        if balance < lamports + 10_000:
            raise WalletError("Wallet has insufficient SOL for the fee and network cost.")
        signature = await self._send(
            telegram_id,
            [transfer(TransferParams(
                from_pubkey=(await self.keypair_for(telegram_id)).pubkey(),
                to_pubkey=self.treasury,
                lamports=lamports,
            ))],
        )
        await self.db.record_fee(telegram_id, fee_type, lamports, signature, idempotency_key)
        return lamports, signature

    async def withdraw_all(self, telegram_id: int, destination: str) -> tuple[int, str]:
        try:
            recipient = Pubkey.from_string(destination)
        except Exception as exc:
            raise WalletError("Destination is not a valid Solana address.") from exc
        keypair = await self.keypair_for(telegram_id)
        source = keypair.pubkey()
        balance = await self.balance(telegram_id)
        fee_lamports = usd_to_lamports("1.00", await self.oracle.sol_usd(force=True))
        if balance <= fee_lamports + 10_000:
            raise WalletError("Balance is too low to pay the $1 fee and Solana network fee.")
        # The fee is estimated from an exact serialized message, then checked again
        # against the RPC's live fee quote before signing.
        blockhash = await self._latest_hash()
        payout = balance - fee_lamports - 10_000
        instructions = [
            transfer(TransferParams(from_pubkey=source, to_pubkey=self.treasury, lamports=fee_lamports)),
            transfer(TransferParams(from_pubkey=source, to_pubkey=recipient, lamports=payout)),
        ]
        message = Message.new_with_blockhash(instructions, source, blockhash)
        quote = await self.helius.rpc(
            "getFeeForMessage",
            [base64.b64encode(bytes(message)).decode("ascii"), {"commitment": "confirmed"}],
        )
        network_fee = int((quote or {}).get("value") or 5000)
        payout = balance - fee_lamports - network_fee
        if payout <= 0:
            raise WalletError("No positive amount remains after fee and network cost.")
        instructions[1] = transfer(TransferParams(
            from_pubkey=source, to_pubkey=recipient, lamports=payout
        ))
        tx = Transaction.new_signed_with_payer(instructions, source, [keypair], blockhash)
        encoded = base64.b64encode(bytes(tx)).decode("ascii")
        signature = await self.helius.rpc(
            "sendTransaction",
            [encoded, {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            }],
        )
        await self.confirm(signature)
        await self.db.record_fee(
            telegram_id, "withdrawal", fee_lamports, signature, f"withdraw:{signature}"
        )
        return payout, signature

    async def close_token_account(
        self,
        telegram_id: int,
        token_account: str,
        mint: str,
        dust_raw_amount: int,
        token_program: str,
    ) -> str:
        """Burn explicitly approved dust and close an empty ATA in one transaction.

        The current rent-exemption amount is fetched dynamically elsewhere; never
        assume an ATA always holds the same rent amount. Token-2022 accounts with
        withheld transfer fees may fail to close and are intentionally not forced.
        """
        owner = (await self.keypair_for(telegram_id)).pubkey()
        try:
            account_pk = Pubkey.from_string(token_account)
            mint_pk = Pubkey.from_string(mint)
            program_pk = Pubkey.from_string(token_program)
        except Exception as exc:
            raise WalletError("Invalid token account, mint, or token program address.") from exc
        if dust_raw_amount < 0:
            raise WalletError("Dust amount cannot be negative.")
        instructions: list[Instruction] = []
        if dust_raw_amount:
            instructions.append(Instruction(
                program_pk,
                b"\\x08" + int(dust_raw_amount).to_bytes(8, "little"),
                [
                    AccountMeta(account_pk, False, True),
                    AccountMeta(mint_pk, False, True),
                    AccountMeta(owner, True, False),
                ],
            ))
        instructions.append(Instruction(
            program_pk,
            b"\\x09",
            [
                AccountMeta(account_pk, False, True),
                AccountMeta(owner, False, True),
                AccountMeta(owner, True, False),
            ],
        ))
        return await self._send(telegram_id, instructions)

    async def rotate_wallet(self, telegram_id: int) -> tuple[str, str]:
        """Retire the old encrypted key and create a fresh inactive wallet.

        This does not assert forensic erasure from storage media or backups.
        The caller must stop the user's engine and settle the old wallet first.
        """
        old_address = await self.db.get_wallet_address(telegram_id)
        if old_address:
            old_balance = await self.balance(telegram_id)
            fee = usd_to_lamports("1.00", await self.oracle.sol_usd(force=True))
            if old_balance > 0:
                if old_balance < fee + 10_000:
                    raise WalletError("Old wallet does not have enough SOL to cover the $1 wipe fee.")
                # Pay only the termination fee; user must withdraw remaining funds beforehand.
                await self.transfer_usd_fee(
                    telegram_id, "1.00", "wipe", f"wipe:{telegram_id}:{old_address}"
                )
        await self.db.delete_wallet_secret(telegram_id)
        new_address, new_secret = await self.create_wallet(telegram_id)
        await self.db.update_settings(telegram_id, {"enabled": False})
        await self.db.security_event(telegram_id, "wallet_rotated", "Old wallet retired; new wallet created paused.")
        return new_address, new_secret
