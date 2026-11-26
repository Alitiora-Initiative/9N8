from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

from .database import DEFAULT_SETTINGS, utc_now

logger = logging.getLogger(__name__)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def _position(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    # Preserve the full unsigned-sized token / lamport values without JSON float
    # conversion or JavaScript's 53-bit integer precision loss.
    for key in ("token_amount_raw", "initial_cost_lamports", "moon_bag_raw", "realized_lamports",
                "peak_value_lamports", "last_value_lamports"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    if item.get("entry_price_usd") is not None:
        item["entry_price_usd"] = str(item["entry_price_usd"])
    return item


class PostgresDatabase:
    """Neon/PostgreSQL persistence adapter for the existing 9N8 domain API.

    Wallet private keys are Fernet-encrypted before they reach PostgreSQL.
    The encryption key must be stable and backed up independently from Neon.
    """

    def __init__(self, database_url: str, encryption_key: bytes):
        if not database_url.startswith(("postgres://", "postgresql://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string.")
        self.database_url = database_url
        self.fernet = Fernet(encryption_key)
        self.pool: asyncpg.Pool | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._initialize_lock:
            if self.pool is not None:
                return
            self.pool = await asyncpg.create_pool(
                dsn=self.database_url,
                min_size=0,
                max_size=5,
                max_inactive_connection_lifetime=60,
                command_timeout=30,
                # Required for transaction-pooling-compatible Neon endpoints.
                statement_cache_size=0,
            )
            async with self.pool.acquire() as conn:
                ready = await conn.fetchval(
                    "SELECT to_regclass('public.n9n8_users') IS NOT NULL"
                )
                if not ready:
                    await self.close()
                    raise RuntimeError(
                        "The 9N8 PostgreSQL schema is missing. Apply migrations/001_n9n8_postgres.sql first."
                    )

    async def close(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            await pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("PostgreSQL pool is not initialized.")
        return self.pool

    async def user_count(self) -> int:
        return int(await self._pool().fetchval("SELECT count(*) FROM public.n9n8_users"))

    async def ensure_user(self, telegram_id: int) -> None:
        pool = self._pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO public.n9n8_users(telegram_id)
                   VALUES($1) ON CONFLICT(telegram_id) DO NOTHING""",
                int(telegram_id),
            )
            await conn.execute(
                """INSERT INTO public.n9n8_settings(telegram_id, config_json)
                   VALUES($1, $2::jsonb) ON CONFLICT(telegram_id) DO NOTHING""",
                int(telegram_id), json.dumps(DEFAULT_SETTINGS, separators=(",", ":")),
            )

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """SELECT u.telegram_id, u.activation_paid, u.enabled, u.created_at, u.updated_at,
                      w.solana_address, w.encrypted_secret, w.payout_address
               FROM public.n9n8_users u
               LEFT JOIN public.n9n8_wallet_vault w USING (telegram_id)
               WHERE u.telegram_id=$1""",
            int(telegram_id),
        )
        if row is None:
            return None
        user = dict(row)
        if user.get("encrypted_secret") is not None:
            user["encrypted_secret"] = bytes(user["encrypted_secret"])
        return user

    async def save_wallet(self, telegram_id: int, address: str, secret_b58: str) -> None:
        encrypted = self.fernet.encrypt(secret_b58.encode("utf-8"))
        await self.ensure_user(telegram_id)
        await self._pool().execute(
            """INSERT INTO public.n9n8_wallet_vault
                 (telegram_id, solana_address, encrypted_secret)
               VALUES($1, $2, $3)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 solana_address=EXCLUDED.solana_address,
                 encrypted_secret=EXCLUDED.encrypted_secret,
                 payout_address=NULL, updated_at=now()""",
            int(telegram_id), address, encrypted,
        )

    async def load_secret(self, telegram_id: int) -> str:
        encrypted = await self._pool().fetchval(
            "SELECT encrypted_secret FROM public.n9n8_wallet_vault WHERE telegram_id=$1",
            int(telegram_id),
        )
        if not encrypted:
            raise RuntimeError("No active wallet is stored for this Telegram account.")
        try:
            return self.fernet.decrypt(bytes(encrypted)).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("Stored wallet cannot be decrypted with the configured ENCRYPTION_MASTER_KEY.") from exc

    async def get_wallet_address(self, telegram_id: int) -> str | None:
        return await self._pool().fetchval(
            "SELECT solana_address FROM public.n9n8_wallet_vault WHERE telegram_id=$1",
            int(telegram_id),
        )

    async def get_settings(self, telegram_id: int) -> dict[str, Any]:
        await self.ensure_user(telegram_id)
        raw = await self._pool().fetchval(
            "SELECT config_json FROM public.n9n8_settings WHERE telegram_id=$1",
            int(telegram_id),
        )
        values = dict(DEFAULT_SETTINGS)
        values.update(_json_value(raw))
        return values

    async def update_settings(self, telegram_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        current = await self.get_settings(telegram_id)
        if set(changes) - set(DEFAULT_SETTINGS):
            raise ValueError("Unknown trading setting.")
        current.update(changes)
        pool = self._pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE public.n9n8_settings
                   SET config_json=$2::jsonb, updated_at=now() WHERE telegram_id=$1""",
                int(telegram_id), json.dumps(current, separators=(",", ":")),
            )
            await conn.execute(
                """UPDATE public.n9n8_users
                   SET enabled=$2, updated_at=now() WHERE telegram_id=$1""",
                int(telegram_id), bool(current.get("enabled")),
            )
        return current

    async def set_activation_paid(self, telegram_id: int, paid: bool = True) -> None:
        await self._pool().execute(
            "UPDATE public.n9n8_users SET activation_paid=$2, updated_at=now() WHERE telegram_id=$1",
            int(telegram_id), bool(paid),
        )

    async def fee_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """SELECT fee_type, amount_lamports, tx_signature, idempotency_key, created_at
               FROM public.n9n8_fee_ledger WHERE idempotency_key=$1""",
            idempotency_key,
        )
        if row is None:
            return None
        item = dict(row)
        item["amount_lamports"] = int(item["amount_lamports"])
        return item

    async def record_fee(
        self, telegram_id: int, fee_type: str, lamports: int, signature: str, idempotency_key: str
    ) -> None:
        if lamports <= 0 or not signature:
            raise ValueError("A fee ledger entry requires a positive amount and transaction signature.")
        await self._pool().execute(
            """INSERT INTO public.n9n8_fee_ledger
                 (telegram_id, fee_type, amount_lamports, tx_signature, idempotency_key)
               VALUES($1, $2, $3, $4, $5) ON CONFLICT(idempotency_key) DO NOTHING""",
            int(telegram_id), fee_type, Decimal(str(lamports)), signature, idempotency_key,
        )

    async def create_position(
        self, telegram_id: int, mint: str, token_amount_raw: int,
        initial_cost_lamports: int, entry_price_usd: str, buy_signature: str
    ) -> int:
        position_id = await self._pool().fetchval(
            """INSERT INTO public.n9n8_positions
                 (telegram_id, mint, token_amount_raw, initial_cost_lamports,
                  entry_price_usd, status, buy_signature)
               VALUES($1,$2,$3,$4,$5,'open',$6) RETURNING id""",
            int(telegram_id), mint, Decimal(str(token_amount_raw)), Decimal(str(initial_cost_lamports)),
            Decimal(str(entry_price_usd)), buy_signature,
        )
        return int(position_id)

    async def active_positions(self, telegram_id: int | None = None) -> list[dict[str, Any]]:
        if telegram_id is None:
            rows = await self._pool().fetch(
                "SELECT * FROM public.n9n8_positions WHERE status='open' ORDER BY id"
            )
        else:
            rows = await self._pool().fetch(
                "SELECT * FROM public.n9n8_positions WHERE status='open' AND telegram_id=$1 ORDER BY id",
                int(telegram_id),
            )
        return [_position(row) for row in rows if row is not None]

    async def get_position(self, position_id: int, telegram_id: int | None = None) -> dict[str, Any] | None:
        if telegram_id is None:
            row = await self._pool().fetchrow(
                "SELECT * FROM public.n9n8_positions WHERE id=$1", int(position_id)
            )
        else:
            row = await self._pool().fetchrow(
                "SELECT * FROM public.n9n8_positions WHERE id=$1 AND telegram_id=$2",
                int(position_id), int(telegram_id),
            )
        return _position(row)

    async def update_position_metrics(
        self, position_id: int, current_value_lamports: int, peak_value_lamports: int,
        pnl_pct: str,
    ) -> None:
        await self._pool().execute(
            """UPDATE public.n9n8_positions SET
               peak_value_lamports=GREATEST(peak_value_lamports,$2),
               last_value_lamports=$3,last_pnl_pct=$4,updated_at=now()
               WHERE id=$1 AND status='open'""",
            int(position_id),Decimal(str(peak_value_lamports)),
            Decimal(str(current_value_lamports)),str(pnl_pct),
        )

    async def close_position(
        self, position_id: int, remaining_raw: int, reason: str,
        realized_lamports: int, sell_signature: str, fully_closed: bool
    ) -> None:
        tp1_update = True if reason in {"take_profit_1","take_profit_partial"} else None
        tp2_update = True if reason == "take_profit_2" else None
        await self._pool().execute(
            """UPDATE public.n9n8_positions
               SET token_amount_raw=$2,
                   moon_bag_raw=$2,
                   status=CASE WHEN $6 THEN 'closed' ELSE 'open' END,
                   exit_reason=$3,
                   realized_lamports=realized_lamports+$4,
                   sell_signature=NULLIF($5,''),
                   tp1_done=COALESCE($7,tp1_done),
                   tp2_done=COALESCE($8,tp2_done),
                   initial_cost_lamports=CASE
                     WHEN $6 THEN 0
                     WHEN token_amount_raw > 0
                       THEN trunc(initial_cost_lamports * $2 / token_amount_raw)
                     ELSE initial_cost_lamports END
               WHERE id=$1""",
            int(position_id), Decimal(str(remaining_raw)), reason, Decimal(str(realized_lamports)),
            sell_signature, bool(fully_closed), tp1_update, tp2_update,
        )

    async def ledger_totals(self, telegram_id: int) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT fee_type, SUM(amount_lamports) AS total
               FROM public.n9n8_fee_ledger WHERE telegram_id=$1 GROUP BY fee_type""",
            int(telegram_id),
        )
        return [{"fee_type": row["fee_type"], "total": int(row["total"])} for row in rows]

    async def security_event(self, telegram_id: int | None, event_type: str, detail: str) -> None:
        await self._pool().execute(
            """INSERT INTO public.n9n8_security_events(telegram_id,event_type,detail)
               VALUES($1,$2,$3)""",
            int(telegram_id) if telegram_id is not None else None, event_type, detail[:1000],
        )

    async def set_payout_address(self, telegram_id: int, address: str) -> None:
        await self._pool().execute(
            """UPDATE public.n9n8_wallet_vault SET payout_address=$2, updated_at=now()
               WHERE telegram_id=$1""",
            int(telegram_id), address,
        )

    async def start_daily_risk(
        self, telegram_id: int, day_utc: str, starting_equity_lamports: int
    ) -> dict[str, int]:
        if starting_equity_lamports < 0:
            raise ValueError("Starting equity cannot be negative.")
        row = await self._pool().fetchrow(
            """INSERT INTO public.n9n8_daily_risk
                 (telegram_id, day_utc, starting_equity_lamports)
               VALUES($1,$2::date,$3)
               ON CONFLICT(telegram_id,day_utc) DO UPDATE
               SET starting_equity_lamports=CASE
                 WHEN public.n9n8_daily_risk.starting_equity_lamports=0 AND $3>0
                 THEN $3 ELSE public.n9n8_daily_risk.starting_equity_lamports END
               RETURNING starting_equity_lamports,realized_pnl_lamports,trade_count""",
            int(telegram_id), day_utc, Decimal(str(starting_equity_lamports)),
        )
        return {
            "starting_equity_lamports": int(row["starting_equity_lamports"]),
            "realized_pnl_lamports": int(row["realized_pnl_lamports"]),
            "trade_count": int(row["trade_count"]),
        }

    async def record_daily_entry(self, telegram_id: int, day_utc: str) -> None:
        await self._pool().execute(
            """UPDATE public.n9n8_daily_risk SET trade_count=trade_count+1
               WHERE telegram_id=$1 AND day_utc=$2::date""",
            int(telegram_id), day_utc,
        )

    async def record_daily_pnl(self, telegram_id: int, day_utc: str, pnl_lamports: int) -> None:
        await self._pool().execute(
            """UPDATE public.n9n8_daily_risk
               SET realized_pnl_lamports=realized_pnl_lamports+$3
               WHERE telegram_id=$1 AND day_utc=$2::date""",
            int(telegram_id), day_utc, Decimal(str(int(pnl_lamports))),
        )

    async def daily_risk_snapshot(self, telegram_id: int, day_utc: str) -> dict[str, int] | None:
        row = await self._pool().fetchrow(
            """SELECT starting_equity_lamports,realized_pnl_lamports,trade_count
               FROM public.n9n8_daily_risk WHERE telegram_id=$1 AND day_utc=$2::date""",
            int(telegram_id), day_utc,
        )
        if row is None:
            return None
        return {
            "starting_equity_lamports": int(row["starting_equity_lamports"]),
            "realized_pnl_lamports": int(row["realized_pnl_lamports"]),
            "trade_count": int(row["trade_count"]),
        }

    async def enabled_users(self) -> list[int]:
        rows = await self._pool().fetch(
            """SELECT u.telegram_id FROM public.n9n8_users u
               JOIN public.n9n8_wallet_vault w USING(telegram_id)
               WHERE u.enabled=TRUE ORDER BY u.telegram_id"""
        )
        return [int(row["telegram_id"]) for row in rows]

    async def count_positions_since(self, telegram_id: int, since_iso: str) -> int:
        return int(await self._pool().fetchval(
            """SELECT count(*) FROM public.n9n8_positions
               WHERE telegram_id=$1 AND opened_at >= $2::timestamptz""",
            int(telegram_id), since_iso,
        ))

    async def has_open_mint(self, telegram_id: int, mint: str) -> bool:
        return bool(await self._pool().fetchval(
            """SELECT EXISTS(SELECT 1 FROM public.n9n8_positions
               WHERE telegram_id=$1 AND mint=$2 AND status='open')""",
            int(telegram_id), mint,
        ))

    async def all_users_with_wallets(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT u.telegram_id,w.solana_address,u.activation_paid,u.enabled
               FROM public.n9n8_users u JOIN public.n9n8_wallet_vault w USING(telegram_id)
               ORDER BY u.telegram_id"""
        )
        return [dict(row) for row in rows]

    async def delete_wallet_secret(self, telegram_id: int) -> None:
        pool = self._pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM public.n9n8_wallet_vault WHERE telegram_id=$1",
                int(telegram_id),
            )
            await conn.execute(
                """UPDATE public.n9n8_users SET activation_paid=FALSE,enabled=FALSE,updated_at=now()
                   WHERE telegram_id=$1""",
                int(telegram_id),
            )
            row = await conn.fetchrow(
                "SELECT config_json FROM public.n9n8_settings WHERE telegram_id=$1",
                int(telegram_id),
            )
            if row:
                config = dict(DEFAULT_SETTINGS)
                config.update(_json_value(row["config_json"]))
                config["enabled"] = False
                await conn.execute(
                    """UPDATE public.n9n8_settings SET config_json=$2::jsonb,updated_at=now()
                       WHERE telegram_id=$1""",
                    int(telegram_id), json.dumps(config, separators=(",", ":")),
                )

    async def enqueue_job(
        self, telegram_id: int, job_type: str, payload: dict[str, Any],
        idempotency_key: str, max_attempts: int = 1,
    ) -> dict[str, Any]:
        allowed = {"panic_sell", "withdraw_all", "rotate_wallet"}
        if job_type not in allowed:
            raise ValueError("Unsupported worker job type.")
        await self.ensure_user(telegram_id)
        row = await self._pool().fetchrow(
            """INSERT INTO public.n9n8_worker_jobs
                 (telegram_id,job_type,payload,idempotency_key,max_attempts)
               VALUES($1,$2,$3::jsonb,$4,$5)
               ON CONFLICT(idempotency_key) DO UPDATE
                 SET idempotency_key=EXCLUDED.idempotency_key
               RETURNING id,telegram_id,job_type,payload,status,attempts,result_json,last_error,created_at""",
            int(telegram_id), job_type, json.dumps(payload), idempotency_key, max(1, int(max_attempts)),
        )
        item = dict(row)
        item["payload"] = _json_value(item["payload"])
        item["result_json"] = _json_value(item["result_json"])
        return item

    async def mark_stale_jobs_unknown(self) -> int:
        """Never replay an operation whose transaction may have landed before a worker crash."""
        status = await self._pool().execute(
            """UPDATE public.n9n8_worker_jobs
               SET status='unknown',
                   last_error='Worker disappeared while this operation was running; verify wallet and signatures before retrying.',
                   locked_at=NULL,locked_by=NULL,updated_at=now()
               WHERE status='running' AND locked_at < now() - INTERVAL '10 minutes'"""
        )
        try:
            return int(str(status).split()[-1])
        except (ValueError, IndexError):
            return 0

    async def claim_jobs(self, worker_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """WITH claimed AS (
                 SELECT id FROM public.n9n8_worker_jobs
                 WHERE status='queued' AND available_at<=now()
                 ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED
               )
               UPDATE public.n9n8_worker_jobs j
               SET status='running',locked_at=now(),locked_by=$1,
                   attempts=attempts+1,updated_at=now()
               FROM claimed c WHERE j.id=c.id
               RETURNING j.id,j.telegram_id,j.job_type,j.payload,j.status,j.attempts,
                         j.max_attempts,j.idempotency_key""",
            worker_id, max(1, min(int(limit), 50)),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _json_value(item["payload"])
            result.append(item)
        return result

    async def complete_job(self, job_id: int, result: dict[str, Any] | None = None) -> None:
        await self._pool().execute(
            """UPDATE public.n9n8_worker_jobs
               SET status='succeeded',result_json=$2::jsonb,last_error=NULL,
                   locked_at=NULL,locked_by=NULL,updated_at=now()
               WHERE id=$1 AND status='running'""",
            int(job_id), json.dumps(result or {}),
        )

    async def fail_job(self, job_id: int, error: str, unknown: bool = False) -> None:
        await self._pool().execute(
            """UPDATE public.n9n8_worker_jobs
               SET status=$2,last_error=$3,locked_at=NULL,locked_by=NULL,updated_at=now()
               WHERE id=$1 AND status='running'""",
            int(job_id), "unknown" if unknown else "failed", error[:1000],
        )

    async def get_job(self, job_id: int, telegram_id: int) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """SELECT id,job_type,status,attempts,result_json,last_error,created_at,updated_at
               FROM public.n9n8_worker_jobs WHERE id=$1 AND telegram_id=$2""",
            int(job_id), int(telegram_id),
        )
        if row is None:
            return None
        item = dict(row)
        item["result_json"] = _json_value(item["result_json"])
        return item

    async def heartbeat(self, worker_id: str, status: str, trading_mode: str, metadata: dict[str, Any] | None = None) -> None:
        await self._pool().execute(
            """INSERT INTO public.n9n8_worker_heartbeats(worker_id,status,trading_mode,metadata,last_seen_at)
               VALUES($1,$2,$3,$4::jsonb,now())
               ON CONFLICT(worker_id) DO UPDATE SET status=EXCLUDED.status,
                 trading_mode=EXCLUDED.trading_mode,metadata=EXCLUDED.metadata,last_seen_at=now()""",
            worker_id, status, trading_mode, json.dumps(metadata or {}),
        )

    async def publish_event(self, telegram_id: int, event: dict[str, Any]) -> int:
        event_type = str(event.get("type") or "event")[:80]
        event_id = await self._pool().fetchval(
            """INSERT INTO public.n9n8_telemetry_events(telegram_id,event_type,event_json)
               VALUES($1,$2,$3::jsonb) RETURNING id""",
            int(telegram_id), event_type, json.dumps(event, default=str),
        )
        return int(event_id)

    async def events_after(self, telegram_id: int, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT id,event_json,created_at FROM public.n9n8_telemetry_events
               WHERE telegram_id=$1 AND id>$2 ORDER BY id LIMIT $3""",
            int(telegram_id), max(0, int(after_id)), max(1, min(int(limit), 200)),
        )
        result = []
        for row in rows:
            item = _json_value(row["event_json"])
            item["id"] = int(row["id"])
            item["created_at"] = row["created_at"].isoformat()
            result.append(item)
        return result

    async def list_trade_receipts(self, telegram_id: int, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT id,event_type,signature,status,mint,position_id,
                      amount_lamports,token_amount_raw,metadata,created_at,confirmed_at
               FROM public.n9n8_trade_receipts
               WHERE telegram_id=$1 ORDER BY id DESC LIMIT $2""",
            int(telegram_id),max(1,min(int(limit),100)),
        )
        result=[]
        for row in rows:
            item=dict(row)
            item["metadata"]=_json_value(item.get("metadata"))
            for key in ("amount_lamports","token_amount_raw"):
                if item.get(key) is not None: item[key]=str(item[key])
            for key in ("created_at","confirmed_at"):
                if item.get(key) is not None: item[key]=item[key].isoformat()
            result.append(item)
        return result

    async def record_trade_receipt(
        self, telegram_id: int, event_type: str, signature: str,
        status: str, mint: str | None = None, position_id: int | None = None,
        amount_lamports: int | None = None, token_amount_raw: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._pool().execute(
            """INSERT INTO public.n9n8_trade_receipts AS existing
                 (telegram_id,event_type,signature,status,mint,position_id,
                  amount_lamports,token_amount_raw,metadata)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
               ON CONFLICT(signature) DO UPDATE SET status=EXCLUDED.status,
                 metadata=existing.metadata || EXCLUDED.metadata,
                 confirmed_at=CASE WHEN EXCLUDED.status IN ('confirmed','finalized')
                                   THEN now() ELSE existing.confirmed_at END""",
            int(telegram_id), event_type, signature, status, mint, position_id,
            Decimal(str(amount_lamports)) if amount_lamports is not None else None,
            Decimal(str(token_amount_raw)) if token_amount_raw is not None else None,
            json.dumps(metadata or {}),
        )
