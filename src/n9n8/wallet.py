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


    async def import_wallet(self, telegram_id: int, secret_b58: str) -> str:
        try:
            keypair = Keypair.from_bytes(b58decode(secret_b58.strip()))
        except Exception as exc:
            raise WalletError("The supplied base58 private key is invalid.") from exc
        await self.db.ensure_user(telegram_id)
        if await self.db.get_wallet_address(telegram_id):
            raise WalletError("A wallet is already associated with this Telegram account.")
        await self.db.save_wallet(telegram_id, str(keypair.pubkey()), secret_b58.strip())
        await self.db.security_event(telegram_id, "wallet_imported", "A user-provided Solana key was imported.")
        return str(keypair.pubkey())

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
        # A completed fee transfer must not be charged again when Telegram retries
        # a callback or the process restarts after persisting the receipt.
        existing = await self.db.fee_receipt(idempotency_key)
        if existing:
            return int(existing["amount_lamports"]), str(existing["tx_signature"])
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

    async def withdraw_amount(
        self, telegram_id: int, destination: str, amount_lamports: int,
    ) -> tuple[int, int, str]:
        """Send a user-approved SOL amount, retaining the exact network-fee reserve.

        This path has no platform fee. The signed transaction is sent once only.
        A confirmation timeout returns the signature and must never trigger an
        automatic retry because the first transaction may still land.
        """
        try:
            recipient = Pubkey.from_string(destination)
        except Exception as exc:
            raise WalletError("Destination is not a valid Solana address.") from exc
        amount = int(amount_lamports)
        if amount <= 0:
            raise WalletError("Withdrawal amount must be greater than zero.")

        keypair = await self.keypair_for(telegram_id)
        source = keypair.pubkey()
        if recipient == source:
            raise WalletError("Destination must be a different wallet from your 9N8 trading wallet.")
        balance = await self.balance(telegram_id)
        blockhash = await self._latest_hash()
        instruction = transfer(TransferParams(
            from_pubkey=source,
            to_pubkey=recipient,
            lamports=amount,
        ))
        message = Message.new_with_blockhash([instruction], source, blockhash)
        fee_quote = await self.helius.rpc(
            "getFeeForMessage",
            [base64.b64encode(bytes(message)).decode("ascii"), {"commitment": "confirmed"}],
        )
        fee_value = (fee_quote or {}).get("value")
        if fee_value is None:
            raise WalletError("Solana could not quote the network fee. No withdrawal was sent.")
        network_fee = int(fee_value)
        reserve = 10_000
        if network_fee <= 0 or balance < amount + network_fee + reserve:
            maximum = max(0, balance - network_fee - reserve)
            raise WalletError(
                "Insufficient available SOL after the current network fee and safety reserve. "
                f"Maximum now: {maximum} lamports. No withdrawal was sent."
            )

        transaction = Transaction.new_signed_with_payer(
            [instruction], source, [keypair], blockhash
        )
        encoded = base64.b64encode(bytes(transaction)).decode("ascii")
        signature = await self.helius.rpc(
            "sendTransaction",
            [encoded, {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            }],
        )
        if not isinstance(signature, str) or not signature:
            raise WalletError(
                "Solana did not return a transaction signature. Check wallet history before retrying."
            )
        try:
            await self.confirm(signature)
        except WalletError as exc:
            failed = "failed on-chain" in str(exc)
            try:
                await self.db.record_trade_receipt(
                    telegram_id,
                    "withdrawal",
                    signature,
                    "failed" if failed else "unknown",
                    amount_lamports=amount,
                    metadata={
                        "destination": str(recipient),
                        "network_fee_lamports": network_fee,
                        "source": "telegram_percent_withdrawal",
                        "confirmation_error": str(exc)[:300],
                    },
                )
            except Exception:
                logger.exception("Could not persist withdrawal outcome for signature %s", signature)
            if failed:
                raise WalletError(
                    f"Withdrawal failed on-chain. Signature: {signature}. No automatic retry was made."
                ) from exc
            raise WalletError(
                "Withdrawal was submitted but confirmation is still unknown. "
                f"Check this signature before doing anything else: {signature}. "
                "9N8 recorded the unknown outcome where possible and will not submit it again automatically."
            ) from exc

        try:
            await self.db.record_trade_receipt(
                telegram_id,
                "withdrawal",
                signature,
                "confirmed",
                amount_lamports=amount,
                metadata={
                    "destination": str(recipient),
                    "network_fee_lamports": network_fee,
                    "source": "telegram_percent_withdrawal",
                },
            )
            await self.db.set_payout_address(telegram_id, str(recipient))
        except Exception as exc:
            raise WalletError(
                "The withdrawal is confirmed on-chain, but the local receipt could not be saved. "
                f"Check this signature: {signature}. Do not send another withdrawal. "
                f"Receipt error: {type(exc).__name__}"
            ) from exc
        return amount, network_fee, signature

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
                b"\x08" + int(dust_raw_amount).to_bytes(8, "little"),
                [
                    AccountMeta(account_pk, False, True),
                    AccountMeta(mint_pk, False, True),
                    AccountMeta(owner, True, False),
                ],
            ))
        instructions.append(Instruction(
            program_pk,
            b"\x09",
            [
                AccountMeta(account_pk, False, True),
                AccountMeta(owner, False, True),
                AccountMeta(owner, True, False),
            ],
        ))
        return await self._send(telegram_id, instructions)

    async def rotate_wallet(self, telegram_id: int, payout_address: str) -> tuple[str, str, int, str]:
        """Pay wipe fee and return the complete old-wallet SOL balance in one tx, then rotate.

        Caller must stop the user engine and ensure open positions are closed.
        """
        if await self.db.active_positions(telegram_id):
            raise WalletError("Close all positions before deleting and rotating this wallet.")
        try:
            recipient = Pubkey.from_string(payout_address)
        except Exception as exc:
            raise WalletError("Payout address is not a valid Solana public key.") from exc
        keypair = await self.keypair_for(telegram_id)
        source = keypair.pubkey()
        balance = await self.helius.balance_lamports(str(source))
        fee_lamports = usd_to_lamports("1.00", await self.oracle.sol_usd(force=True))
        if balance <= fee_lamports + 5000:
            raise WalletError("Old wallet cannot cover the $1 wipe fee and network fee.")
        blockhash = await self._latest_hash()
        fee_ix = transfer(TransferParams(
            from_pubkey=source, to_pubkey=self.treasury, lamports=fee_lamports
        ))
        provisional_payout = balance - fee_lamports - 5000
        payout_ix = transfer(TransferParams(
            from_pubkey=source, to_pubkey=recipient, lamports=provisional_payout
        ))
        provisional_message = Message.new_with_blockhash(
            [fee_ix, payout_ix], source, blockhash
        )
        fee_quote = await self.helius.rpc(
            "getFeeForMessage",
            [base64.b64encode(bytes(provisional_message)).decode("ascii"), {"commitment": "confirmed"}],
        )
        network_fee = int((fee_quote or {}).get("value") or 5000)
        payout_lamports = balance - fee_lamports - network_fee
        if payout_lamports <= 0:
            raise WalletError("No positive payout remains after the wipe and network fees.")
        payout_ix = transfer(TransferParams(
            from_pubkey=source, to_pubkey=recipient, lamports=payout_lamports
        ))
        tx = Transaction.new_signed_with_payer(
            [fee_ix, payout_ix], source, [keypair], blockhash
        )
        signature = await self.helius.rpc("sendTransaction", [
            base64.b64encode(bytes(tx)).decode("ascii"),
            {
                "encoding": "base64", "skipPreflight": False,
                "preflightCommitment": "confirmed", "maxRetries": 3,
            },
        ])
        await self.confirm(str(signature))
        old_address = str(source)
        await self.db.record_fee(
            telegram_id, "wipe", fee_lamports, str(signature),
            f"wipe:{telegram_id}:{old_address}",
        )
        await self.db.delete_wallet_secret(telegram_id)
        new_address, new_secret = await self.create_wallet(telegram_id)
        await self.db.update_settings(telegram_id, {"enabled": False})
        await self.db.security_event(
            telegram_id, "wallet_rotated",
            f"Old wallet {old_address} retired after payout; new wallet created paused.",
        )
        return new_address, new_secret, payout_lamports, str(signature)
