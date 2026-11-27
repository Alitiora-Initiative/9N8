from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet


DEFAULT_SETTINGS: dict[str, Any] = {
    "launchpad": "both",
    "buy_amount_sol": "0.05",
    "slippage_pct": "15",
    "buy_slippage_pct": "15",
    "sell_slippage_pct": "20",
    "priority_fee_lamports": 10000,
    "jito_tip_lamports": 2000000,
    "take_profit_pct": "100",
    "take_profit_2_pct": "300",
    "take_profit_2_sell_pct": "25",
    "trailing_stop_pct": "20",
    "stop_loss_pct": "30",
    "max_hold_seconds": 14400,
    "moon_bag_pct": "50",
    "min_liquidity_sol": "5",
    "min_lp_locked_pct": "80",
    "max_dev_wallet_pct": "5",
    "require_mint_authority_revoked": True,
    "require_freeze_authority_revoked": True,
    "daily_position_limit": 5,
    "max_daily_drawdown_pct": "15",
    "mev_shield": True,
    "min_liquidity_usd": "0",
    "max_price_impact_pct": "2",
    "max_slippage_bps": 5000,
    "max_build_quote_deviation_bps": 300,
    "pump_mode": "migration",
    "pump_migration_progress_pct": "80",
    "max_top10_concentration_pct": "25",
    "enabled": False,
    "paper_enabled": False,
    "live_enabled": False,
}


PAPER_DEFAULT_SETTINGS: dict[str, Any] = {
    "launchpad": "both",
    "buy_amount_sol": "0.05",
    "slippage_pct": "15",
    "buy_slippage_pct": "15",
    "sell_slippage_pct": "20",
    "priority_fee_lamports": 10000,
    "jito_tip_lamports": 2000000,
    "take_profit_pct": "100",
    "take_profit_2_pct": "300",
    "take_profit_2_sell_pct": "25",
    "trailing_stop_pct": "20",
    "stop_loss_pct": "30",
    "max_hold_seconds": 14400,
    "moon_bag_pct": "50",
    "daily_position_limit": 5,
    "max_daily_drawdown_pct": "15",
    "mev_shield": True,
    "min_liquidity_sol": "5",
    "min_lp_locked_pct": "80",
    "min_liquidity_usd": "0",
    "max_price_impact_pct": "2",
    "max_slippage_bps": 5000,
    "pump_mode": "migration",
    "pump_migration_progress_pct": "80",
    "max_top10_concentration_pct": "25",
    "max_dev_wallet_pct": "5",
    "require_mint_authority_revoked": True,
    "require_freeze_authority_revoked": True,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str, encryption_key: bytes):
        self.path = path
        self.fernet = Fernet(encryption_key)
        self._lock = __import__("asyncio").Lock()

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA secure_delete=ON")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    solana_address TEXT,
                    encrypted_secret BLOB,
                    payout_address TEXT,
                    activation_paid INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    mint TEXT NOT NULL,
                    token_amount_raw INTEGER NOT NULL,
                    initial_cost_lamports INTEGER NOT NULL,
                    entry_price_usd TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    moon_bag_raw INTEGER NOT NULL DEFAULT 0,
                    exit_reason TEXT,
                    realized_lamports INTEGER NOT NULL DEFAULT 0,
                    buy_signature TEXT,
                    sell_signature TEXT,
                    peak_value_lamports INTEGER NOT NULL DEFAULT 0,
                    last_value_lamports INTEGER NOT NULL DEFAULT 0,
                    last_pnl_pct TEXT NOT NULL DEFAULT '0',
                    tp1_done INTEGER NOT NULL DEFAULT 0,
                    tp2_done INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_positions_user_status ON positions(telegram_id, status);
                CREATE TABLE IF NOT EXISTS fee_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    fee_type TEXT NOT NULL,
                    amount_lamports INTEGER NOT NULL,
                    tx_signature TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_risk (
                    telegram_id INTEGER NOT NULL,
                    day_utc TEXT NOT NULL,
                    starting_equity_lamports INTEGER NOT NULL,
                    realized_pnl_lamports INTEGER NOT NULL DEFAULT 0,
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(telegram_id, day_utc)
                );
                CREATE TABLE IF NOT EXISTS security_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_settings (
                    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                    starting_balance_lamports INTEGER NOT NULL,
                    cash_balance_lamports INTEGER NOT NULL,
                    realized_pnl_lamports INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_days (
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    day_utc TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    PRIMARY KEY(telegram_id, day_utc)
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    mint TEXT NOT NULL,
                    launchpad TEXT NOT NULL,
                    token_amount_raw INTEGER NOT NULL,
                    initial_cost_lamports INTEGER NOT NULL,
                    entry_price_usd TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','closed')),
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    exit_reason TEXT,
                    realized_lamports INTEGER NOT NULL DEFAULT 0,
                    peak_value_lamports INTEGER NOT NULL DEFAULT 0,
                    last_value_lamports INTEGER NOT NULL DEFAULT 0,
                    last_pnl_pct TEXT NOT NULL DEFAULT '0',
                    tp1_done INTEGER NOT NULL DEFAULT 0,
                    tp2_done INTEGER NOT NULL DEFAULT 0,
                    buy_reference TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_positions_user_status
                    ON paper_positions(telegram_id, status, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_positions_one_open_mint
                    ON paper_positions(telegram_id, mint) WHERE status='open';
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    position_id INTEGER REFERENCES paper_positions(id) ON DELETE SET NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell','rejected')),
                    mint TEXT NOT NULL,
                    launchpad TEXT,
                    token_amount_raw INTEGER NOT NULL DEFAULT 0,
                    sol_amount_lamports INTEGER NOT NULL DEFAULT 0,
                    fee_lamports INTEGER NOT NULL DEFAULT 0,
                    price_usd TEXT NOT NULL DEFAULT '0',
                    reason TEXT,
                    reference_signature TEXT,
                    quote_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_trades_user_time
                    ON paper_trades(telegram_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS trade_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    signature TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    mint TEXT,
                    position_id INTEGER,
                    amount_lamports INTEGER,
                    token_amount_raw INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trade_receipts_user_time
                    ON trade_receipts(telegram_id,created_at DESC);
            """)
            # Upgrade existing local databases without changing live position records.
            position_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(positions)")).fetchall()
            }
            for column, definition in (
                ("peak_value_lamports", "INTEGER NOT NULL DEFAULT 0"),
                ("last_value_lamports", "INTEGER NOT NULL DEFAULT 0"),
                ("last_pnl_pct", "TEXT NOT NULL DEFAULT '0'"),
                ("tp1_done", "INTEGER NOT NULL DEFAULT 0"),
                ("tp2_done", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in position_columns:
                    await db.execute(f"ALTER TABLE positions ADD COLUMN {column} {definition}")
            await db.commit()

    async def user_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            row = await cur.fetchone()
            return int(row[0] if row else 0)

    async def ensure_user(self, telegram_id: int) -> None:
        now = utc_now()
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users(telegram_id, created_at, updated_at) VALUES(?,?,?)",
                (telegram_id, now, now),
            )
            await db.execute(
                "INSERT OR IGNORE INTO settings(telegram_id, config_json) VALUES(?,?)",
                (telegram_id, json.dumps(DEFAULT_SETTINGS)),
            )
            await db.execute(
                "INSERT OR IGNORE INTO paper_settings(telegram_id, config_json, updated_at) VALUES(?,?,?)",
                (telegram_id, json.dumps(PAPER_DEFAULT_SETTINGS), now),
            )
            await db.commit()

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def save_wallet(self, telegram_id: int, address: str, secret_b58: str) -> None:
        encrypted = self.fernet.encrypt(secret_b58.encode("utf-8"))
        now = utc_now()
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET solana_address=?, encrypted_secret=?, updated_at=? WHERE telegram_id=?",
                (address, encrypted, now, telegram_id),
            )
            await db.commit()

    async def load_secret(self, telegram_id: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT encrypted_secret FROM users WHERE telegram_id=?", (telegram_id,))
            row = await cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError("No active wallet is stored for this Telegram account.")
        return self.fernet.decrypt(bytes(row[0])).decode("utf-8")

    async def get_wallet_address(self, telegram_id: int) -> str | None:
        user = await self.get_user(telegram_id)
        return user["solana_address"] if user else None

    async def get_settings(self, telegram_id: int, mode: str = "live") -> dict[str, Any]:
        await self.ensure_user(telegram_id)
        if mode not in {"live", "paper"}:
            raise ValueError("mode must be live or paper")
        table, defaults = ("settings", DEFAULT_SETTINGS) if mode == "live" else ("paper_settings", PAPER_DEFAULT_SETTINGS)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(f"SELECT config_json FROM {table} WHERE telegram_id=?", (telegram_id,))
            row = await cur.fetchone()
        data = dict(defaults)
        if row:
            data.update(json.loads(row[0]))
        return data

    async def update_settings(self, telegram_id: int, changes: dict[str, Any], mode: str = "live") -> dict[str, Any]:
        if mode not in {"live", "paper"}:
            raise ValueError("mode must be live or paper")
        current = await self.get_settings(telegram_id, mode=mode)
        defaults = DEFAULT_SETTINGS if mode == "live" else PAPER_DEFAULT_SETTINGS
        if set(changes) - set(defaults):
            raise ValueError("Unknown trading setting.")
        current.update(changes)
        now = utc_now()
        async with self._lock, aiosqlite.connect(self.path) as db:
            if mode == "live":
                await db.execute(
                    "UPDATE settings SET config_json=? WHERE telegram_id=?",
                    (json.dumps(current, separators=(",", ":")), telegram_id),
                )
                live_on = bool(current.get("live_enabled", current.get("enabled", False)))
                await db.execute(
                    "UPDATE users SET enabled=?, updated_at=? WHERE telegram_id=?",
                    (1 if live_on else 0, now, telegram_id),
                )
            else:
                await db.execute(
                    "UPDATE paper_settings SET config_json=?, updated_at=? WHERE telegram_id=?",
                    (json.dumps(current, separators=(",", ":")), now, telegram_id),
                )
            await db.commit()
        return current

    async def set_mode_enabled(self, telegram_id: int, mode: str, enabled: bool) -> None:
        if mode not in {"paper", "live"}:
            raise ValueError("mode must be paper or live")
        await self.ensure_user(telegram_id)
        current = await self.get_settings(telegram_id, "live")
        key = "paper_enabled" if mode == "paper" else "live_enabled"
        current[key] = bool(enabled)
        current["enabled"] = bool(current.get("live_enabled"))
        await self.update_settings(telegram_id, {key: bool(enabled), "enabled": current["enabled"], "live_enabled": current["live_enabled"], "paper_enabled": current["paper_enabled"]}, mode="live")

    async def mode_enabled(self, telegram_id: int, mode: str) -> bool:
        current = await self.get_settings(telegram_id, "live")
        if mode == "paper":
            return bool(current.get("paper_enabled", False))
        if mode == "live":
            return bool(current.get("live_enabled", current.get("enabled", False)))
        raise ValueError("mode must be paper or live")

    async def all_users(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT telegram_id, solana_address, activation_paid, enabled FROM users ORDER BY telegram_id"
            )
            return [dict(row) for row in await cur.fetchall()]

    async def enabled_users(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT u.telegram_id FROM users u
                   LEFT JOIN settings s USING(telegram_id)
                   WHERE json_extract(s.config_json, '$.paper_enabled')=1
                      OR json_extract(s.config_json, '$.live_enabled')=1
                      OR (json_extract(s.config_json, '$.live_enabled') IS NULL AND u.enabled=1)"""
            )
            return [int(row[0]) for row in await cur.fetchall()]

    async def set_activation_paid(self, telegram_id: int, paid: bool = True) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET activation_paid=?, updated_at=? WHERE telegram_id=?",
                (1 if paid else 0, utc_now(), telegram_id),
            )
            await db.commit()


    async def record_trade_receipt(
        self, telegram_id: int, event_type: str, signature: str, status: str,
        mint: str | None = None, position_id: int | None = None,
        amount_lamports: int | None = None, token_amount_raw: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        confirmed = now if status in {"confirmed","finalized"} else None
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO trade_receipts
                   (telegram_id,event_type,signature,status,mint,position_id,amount_lamports,
                    token_amount_raw,metadata_json,created_at,confirmed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(signature) DO UPDATE SET status=excluded.status,
                     metadata_json=excluded.metadata_json,
                     confirmed_at=COALESCE(excluded.confirmed_at,trade_receipts.confirmed_at)""",
                (int(telegram_id),event_type,signature,status,mint,position_id,amount_lamports,
                 token_amount_raw,json.dumps(metadata or {},separators=(",",":")),now,confirmed),
            )
            await db.commit()

    async def list_trade_receipts(self, telegram_id: int, limit: int = 30) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM trade_receipts WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
                (int(telegram_id),max(1,min(int(limit),100))),
            )
            rows = await cur.fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            try: item["metadata"]=json.loads(item.pop("metadata_json"))
            except (TypeError,json.JSONDecodeError): item["metadata"]={}
            result.append(item)
        return result

    async def fee_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT fee_type,amount_lamports,tx_signature,idempotency_key,created_at FROM fee_ledger WHERE idempotency_key=?",
                (idempotency_key,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def record_fee(
        self, telegram_id: int, fee_type: str, lamports: int, signature: str, idempotency_key: str
    ) -> None:
        if lamports <= 0 or not signature:
            raise ValueError("A fee ledger entry requires a positive amount and transaction signature.")
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO fee_ledger
                   (telegram_id, fee_type, amount_lamports, tx_signature, idempotency_key, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (telegram_id, fee_type, lamports, signature, idempotency_key, utc_now()),
            )
            await db.commit()

    async def create_position(
        self, telegram_id: int, mint: str, token_amount_raw: int,
        initial_cost_lamports: int, entry_price_usd: str, buy_signature: str
    ) -> int:
        async with self._lock, aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO positions
                   (telegram_id,mint,token_amount_raw,initial_cost_lamports,entry_price_usd,
                    opened_at,status,buy_signature)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (telegram_id, mint, token_amount_raw, initial_cost_lamports, entry_price_usd,
                 utc_now(), "open", buy_signature),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def active_positions(self, telegram_id: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if telegram_id is None:
                cur = await db.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")
            else:
                cur = await db.execute(
                    "SELECT * FROM positions WHERE status='open' AND telegram_id=? ORDER BY id",
                    (telegram_id,),
                )
            rows = await cur.fetchall()
            return [dict(x) for x in rows]

    async def update_position_metrics(
        self, position_id: int, current_value_lamports: int, peak_value_lamports: int,
        pnl_pct: str,
    ) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE positions SET peak_value_lamports=MAX(peak_value_lamports,?),
                   last_value_lamports=?,last_pnl_pct=?,updated_at=?
                   WHERE id=? AND status='open'""",
                (int(peak_value_lamports),int(current_value_lamports),str(pnl_pct),utc_now(),int(position_id)),
            )
            await db.commit()

    async def close_position(
        self, position_id: int, remaining_raw: int, reason: str,
        realized_lamports: int, sell_signature: str, fully_closed: bool
    ) -> None:
        status = "closed" if fully_closed else "open"
        tp1_update = 1 if reason in {"take_profit_1","take_profit_partial"} else None
        tp2_update = 1 if reason == "take_profit_2" else None
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE positions SET token_amount_raw=?, moon_bag_raw=?, status=?,
                   exit_reason=?, realized_lamports=realized_lamports+?, sell_signature=?,
                   tp1_done=COALESCE(?,tp1_done),tp2_done=COALESCE(?,tp2_done),
                   initial_cost_lamports=CASE
                       WHEN ?=1 THEN 0
                       WHEN token_amount_raw>0 THEN CAST(initial_cost_lamports * ? / token_amount_raw AS INTEGER)
                       ELSE initial_cost_lamports END
                   WHERE id=?""",
                (
                    remaining_raw, remaining_raw, status, reason, realized_lamports,
                    sell_signature,tp1_update,tp2_update,
                    1 if fully_closed else 0, remaining_raw, position_id,
                ),
            )
            await db.commit()

    async def ledger_totals(self, telegram_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT fee_type, SUM(amount_lamports) AS total FROM fee_ledger WHERE telegram_id=? GROUP BY fee_type",
                (telegram_id,),
            )
            return [dict(x) for x in await cur.fetchall()]

    async def security_event(self, telegram_id: int | None, event_type: str, detail: str) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO security_events(telegram_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (telegram_id, event_type, detail[:1000], utc_now()),
            )
            await db.commit()

    async def list_security_events(self, telegram_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT event_type,detail,created_at FROM security_events "
                "WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
                (int(telegram_id), max(1, min(int(limit), 100))),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def wallet_key_was_revealed(self, telegram_id: int, address: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """SELECT 1 FROM security_events
                   WHERE telegram_id=? AND event_type='wallet_key_revealed' AND detail=?
                   LIMIT 1""",
                (int(telegram_id), str(address)),
            )
            return await cur.fetchone() is not None

    async def set_payout_address(self, telegram_id: int, address: str) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET payout_address=?, updated_at=? WHERE telegram_id=?",
                (address, utc_now(), telegram_id),
            )
            await db.commit()



    async def start_daily_risk(self, telegram_id: int, day_utc: str, starting_equity_lamports: int) -> dict[str, int]:
        if starting_equity_lamports < 0:
            raise ValueError("Starting equity cannot be negative.")
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO daily_risk
                   (telegram_id,day_utc,starting_equity_lamports,realized_pnl_lamports,trade_count)
                   VALUES(?,?,?,0,0)""",
                (telegram_id, day_utc, starting_equity_lamports),
            )
            # If a user started the bot before their first deposit, initialize
            # the day's baseline on the first positive funded balance.
            await db.execute(
                """UPDATE daily_risk SET starting_equity_lamports=?
                   WHERE telegram_id=? AND day_utc=?
                     AND starting_equity_lamports=0 AND ? > 0""",
                (starting_equity_lamports, telegram_id, day_utc, starting_equity_lamports),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT starting_equity_lamports,realized_pnl_lamports,trade_count FROM daily_risk WHERE telegram_id=? AND day_utc=?",
                (telegram_id, day_utc),
            )
            row = await cur.fetchone()
        return {
            "starting_equity_lamports": int(row[0]),
            "realized_pnl_lamports": int(row[1]),
            "trade_count": int(row[2]),
        }

    async def record_daily_entry(self, telegram_id: int, day_utc: str) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE daily_risk SET trade_count=trade_count+1 WHERE telegram_id=? AND day_utc=?",
                (telegram_id, day_utc),
            )
            await db.commit()

    async def record_daily_pnl(self, telegram_id: int, day_utc: str, pnl_lamports: int) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE daily_risk SET realized_pnl_lamports=realized_pnl_lamports+? WHERE telegram_id=? AND day_utc=?",
                (int(pnl_lamports), telegram_id, day_utc),
            )
            await db.commit()

    async def daily_risk_snapshot(self, telegram_id: int, day_utc: str) -> dict[str, int] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT starting_equity_lamports,realized_pnl_lamports,trade_count FROM daily_risk WHERE telegram_id=? AND day_utc=?",
                (telegram_id, day_utc),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "starting_equity_lamports": int(row[0]),
            "realized_pnl_lamports": int(row[1]),
            "trade_count": int(row[2]),
        }

    async def enabled_users(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT telegram_id FROM users WHERE enabled=1 AND solana_address IS NOT NULL")
            return [int(row[0]) for row in await cur.fetchall()]

    async def count_positions_since(self, telegram_id: int, since_iso: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM positions WHERE telegram_id=? AND opened_at>=?",
                (telegram_id, since_iso),
            )
            row = await cur.fetchone()
            return int(row[0] if row else 0)

    async def has_open_mint(self, telegram_id: int, mint: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM positions WHERE telegram_id=? AND mint=? AND status='open' LIMIT 1",
                (telegram_id, mint),
            )
            return await cur.fetchone() is not None

    async def get_position(self, position_id: int, telegram_id: int | None = None) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if telegram_id is None:
                cur = await db.execute("SELECT * FROM positions WHERE id=?", (position_id,))
            else:
                cur = await db.execute(
                    "SELECT * FROM positions WHERE id=? AND telegram_id=?",
                    (position_id, telegram_id),
                )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def all_users_with_wallets(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT telegram_id, solana_address, activation_paid, enabled FROM users WHERE solana_address IS NOT NULL"
            )
            return [dict(row) for row in await cur.fetchall()]


    async def create_paper_account(self, telegram_id: int, starting_balance_lamports: int) -> dict[str, int]:
        if starting_balance_lamports <= 0:
            raise ValueError("Paper starting balance must be positive.")
        await self.ensure_user(telegram_id)
        now = utc_now()
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO paper_accounts
                   (telegram_id,starting_balance_lamports,cash_balance_lamports,realized_pnl_lamports,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (telegram_id, starting_balance_lamports, starting_balance_lamports, 0, now, now),
            )
            await db.commit()
        return await self.get_paper_account(telegram_id)

    async def get_paper_account(self, telegram_id: int) -> dict[str, int] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT starting_balance_lamports,cash_balance_lamports,realized_pnl_lamports FROM paper_accounts WHERE telegram_id=?",
                (telegram_id,),
            )
            row = await cur.fetchone()
        return {k: int(row[k]) for k in ("starting_balance_lamports","cash_balance_lamports","realized_pnl_lamports")} if row else None

    async def touch_paper_day(self, telegram_id: int, day_utc: str) -> None:
        await self.ensure_user(telegram_id)
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO paper_days(telegram_id,day_utc,started_at) VALUES(?,?,?)",
                (telegram_id, day_utc, utc_now()),
            )
            await db.commit()

    async def paper_active_days(self, telegram_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM paper_days WHERE telegram_id=?", (telegram_id,))
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def paper_has_open_mint(self, telegram_id: int, mint: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM paper_positions WHERE telegram_id=? AND mint=? AND status='open' LIMIT 1",
                (telegram_id, mint),
            )
            row = await cur.fetchone()
        return row is not None

    async def create_paper_position(
        self, telegram_id: int, mint: str, launchpad: str, token_amount_raw: int,
        cost_lamports: int, entry_price_usd: str, fee_lamports: int,
        quote: dict[str, Any], reference_signature: str | None = None,
    ) -> int:
        if token_amount_raw <= 0 or cost_lamports <= 0:
            raise ValueError("Paper buy must have positive token quantity and cost.")
        now = utc_now()
        pool_cost = int(cost_lamports) + max(0, int(fee_lamports))
        async with self._lock, aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            acct = await db.execute("SELECT cash_balance_lamports FROM paper_accounts WHERE telegram_id=?", (telegram_id,))
            row = await acct.fetchone()
            if not row or int(row[0]) < pool_cost:
                await db.rollback()
                raise ValueError("Not enough virtual SOL in your paper account.")
            await db.execute(
                "UPDATE paper_accounts SET cash_balance_lamports=cash_balance_lamports-?,updated_at=? WHERE telegram_id=?",
                (pool_cost, now, telegram_id),
            )
            cur = await db.execute(
                """INSERT INTO paper_positions
                   (telegram_id,mint,launchpad,token_amount_raw,initial_cost_lamports,entry_price_usd,status,opened_at,peak_value_lamports,buy_reference,updated_at)
                   VALUES(?,?,?,?,?,?,'open',?,?,?,?)""",
                (telegram_id,mint,launchpad,token_amount_raw,pool_cost,entry_price_usd,now,0,reference_signature,now),
            )
            position_id = int(cur.lastrowid)
            await db.execute(
                """INSERT INTO paper_trades
                   (telegram_id,position_id,side,mint,launchpad,token_amount_raw,sol_amount_lamports,fee_lamports,price_usd,reason,reference_signature,quote_json,created_at)
                   VALUES(?,?, 'buy', ?, ?, ?, ?, ?, ?, 'paper_entry', ?, ?, ?)""",
                (telegram_id,position_id,mint,launchpad,token_amount_raw,cost_lamports,fee_lamports,entry_price_usd,reference_signature,json.dumps(quote,separators=(",",":")),now),
            )
            await db.commit()
            return position_id

    async def active_paper_positions(self, telegram_id: int | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if telegram_id is None:
                cur = await db.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY id")
            else:
                cur = await db.execute("SELECT * FROM paper_positions WHERE status='open' AND telegram_id=? ORDER BY id",(telegram_id,))
            rows=await cur.fetchall()
        return [dict(row) for row in rows]

    async def get_paper_position(self, position_id: int, telegram_id: int | None = None) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            if telegram_id is None:
                cur=await db.execute("SELECT * FROM paper_positions WHERE id=?",(position_id,))
            else:
                cur=await db.execute("SELECT * FROM paper_positions WHERE id=? AND telegram_id=?",(position_id,telegram_id))
            row=await cur.fetchone()
        return dict(row) if row else None

    async def mark_paper_position(self, position_id: int, value_lamports: int, pnl_pct: str) -> None:
        now=utc_now()
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE paper_positions SET
                   peak_value_lamports=MAX(peak_value_lamports,?),
                   last_value_lamports=?,last_pnl_pct=?,updated_at=? WHERE id=? AND status='open'""",
                (int(value_lamports),int(value_lamports),pnl_pct,now,position_id),
            )
            await db.commit()

    async def paper_sell(
        self, position_id: int, telegram_id: int, token_amount_raw: int,
        gross_proceeds_lamports: int, fee_lamports: int, reason: str,
        quote: dict[str, Any], reference_signature: str | None = None,
    ) -> dict[str, int | bool]:
        if token_amount_raw <= 0 or gross_proceeds_lamports <= 0:
            raise ValueError("Paper sell needs a positive quantity and quote.")
        now=utc_now()
        async with self._lock, aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cur=await db.execute(
                "SELECT * FROM paper_positions WHERE id=? AND telegram_id=? AND status='open'",
                (position_id,telegram_id),
            )
            pos=await cur.fetchone()
            if not pos:
                await db.rollback()
                raise ValueError("Paper position is not open.")
            old_amount=int(pos["token_amount_raw"])
            if token_amount_raw>old_amount:
                await db.rollback()
                raise ValueError("Paper sell quantity exceeds the open token amount.")
            basis=int(pos["initial_cost_lamports"])
            basis_sold=min(basis,(basis*token_amount_raw)//max(old_amount,1))
            net_proceeds=max(0,int(gross_proceeds_lamports)-max(0,int(fee_lamports)))
            realized=net_proceeds-basis_sold
            remaining=old_amount-token_amount_raw
            remain_basis=0 if remaining==0 else basis-basis_sold
            await db.execute(
                """UPDATE paper_accounts SET cash_balance_lamports=cash_balance_lamports+?,
                   realized_pnl_lamports=realized_pnl_lamports+?,updated_at=? WHERE telegram_id=?""",
                (net_proceeds,realized,now,telegram_id),
            )
            await db.execute(
                """UPDATE paper_positions SET token_amount_raw=?,initial_cost_lamports=?,
                   status=?,closed_at=?,exit_reason=?,realized_lamports=realized_lamports+?,updated_at=?
                   WHERE id=?""",
                (remaining,remain_basis,"closed" if remaining==0 else "open",now if remaining==0 else None,reason,realized,now,position_id),
            )
            await db.execute(
                """INSERT INTO paper_trades
                   (telegram_id,position_id,side,mint,launchpad,token_amount_raw,sol_amount_lamports,fee_lamports,price_usd,reason,reference_signature,quote_json,created_at)
                   VALUES(?,?,'sell',?,?,?,?,?,?,?,?,?,?)""",
                (telegram_id,position_id,pos["mint"],pos["launchpad"],token_amount_raw,gross_proceeds_lamports,fee_lamports,pos["entry_price_usd"],reason,reference_signature,json.dumps(quote,separators=(",",":")),now),
            )
            await db.commit()
        return {"remaining_raw":remaining,"basis_sold_lamports":basis_sold,"realized_lamports":realized,"net_proceeds_lamports":net_proceeds,"fully_closed":remaining==0}

    async def paper_history(self, telegram_id: int, limit: int = 30) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            cur=await db.execute(
                "SELECT * FROM paper_trades WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
                (telegram_id,max(1,min(int(limit),100))),
            )
            rows=await cur.fetchall()
        return [dict(row) for row in rows]

    async def delete_wallet_secret(self, telegram_id: int) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute(
                "UPDATE users SET encrypted_secret=NULL, solana_address=NULL, activation_paid=0, enabled=0, updated_at=? WHERE telegram_id=?",
                (utc_now(), telegram_id),
            )
            await db.commit()
        # SQLite secure_delete plus WAL checkpoint/VACUUM is best-effort database hygiene,
        # not a promise of forensic erasure on SSD, cloud snapshots, or filesystem backups.
        async with aiosqlite.connect(self.path) as db:
            checkpoint = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await checkpoint.fetchall()
            await checkpoint.close()
            await db.execute("VACUUM")
