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
    "slippage_pct": "5",
    "priority_fee_lamports": 10000,
    "jito_tip_lamports": 1000000,
    "take_profit_pct": "10",
    "stop_loss_pct": "15",
    "max_hold_seconds": 60,
    "moon_bag_pct": "15",
    "daily_position_limit": 5,
    "max_daily_drawdown_pct": "15",
    "mev_shield": True,
    "min_liquidity_usd": "5000",
    "max_top10_concentration_pct": "25",
    "enabled": False,
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
                    sell_signature TEXT
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
            """)
            await db.commit()

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

    async def get_settings(self, telegram_id: int) -> dict[str, Any]:
        await self.ensure_user(telegram_id)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT config_json FROM settings WHERE telegram_id=?", (telegram_id,))
            row = await cur.fetchone()
        data = dict(DEFAULT_SETTINGS)
        if row:
            data.update(json.loads(row[0]))
        return data

    async def update_settings(self, telegram_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        current = await self.get_settings(telegram_id)
        allowed = set(DEFAULT_SETTINGS)
        if set(changes) - allowed:
            raise ValueError("Unknown trading setting.")
        current.update(changes)
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE settings SET config_json=? WHERE telegram_id=?",
                (json.dumps(current, separators=(",", ":")), telegram_id),
            )
            await db.execute(
                "UPDATE users SET enabled=?, updated_at=? WHERE telegram_id=?",
                (1 if current["enabled"] else 0, utc_now(), telegram_id),
            )
            await db.commit()
        return current

    async def set_activation_paid(self, telegram_id: int, paid: bool = True) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET activation_paid=?, updated_at=? WHERE telegram_id=?",
                (1 if paid else 0, utc_now(), telegram_id),
            )
            await db.commit()

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

    async def close_position(
        self, position_id: int, remaining_raw: int, reason: str,
        realized_lamports: int, sell_signature: str, fully_closed: bool
    ) -> None:
        status = "closed" if fully_closed else "open"
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE positions SET token_amount_raw=?, moon_bag_raw=?, status=?,
                   exit_reason=?, realized_lamports=realized_lamports+?, sell_signature=?,
                   initial_cost_lamports=CASE
                       WHEN ?=1 THEN 0
                       WHEN token_amount_raw>0 THEN CAST(initial_cost_lamports * ? / token_amount_raw AS INTEGER)
                       ELSE initial_cost_lamports END
                   WHERE id=?""",
                (
                    remaining_raw, remaining_raw, status, reason, realized_lamports,
                    sell_signature, 1 if fully_closed else 0, remaining_raw, position_id,
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

    async def delete_wallet_secret(self, telegram_id: int) -> None:
        async with self._lock, aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA secure_delete=ON")
            await db.execute(
                "UPDATE users SET encrypted_secret=NULL, solana_address=NULL, enabled=0, updated_at=? WHERE telegram_id=?",
                (utc_now(), telegram_id),
            )
            await db.commit()
        # SQLite secure_delete plus WAL checkpoint/VACUUM is best-effort database hygiene,
        # not a promise of forensic erasure on SSD, cloud snapshots, or filesystem backups.
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await db.execute("VACUUM")
            await db.commit()
