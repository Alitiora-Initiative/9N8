from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL, level TEXT NOT NULL, event_type TEXT NOT NULL,
  venue TEXT, symbol TEXT, message TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL UNIQUE,
  venue TEXT NOT NULL, symbol TEXT NOT NULL, market_type TEXT NOT NULL,
  side TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
  quantity REAL NOT NULL, average_price REAL NOT NULL,
  entry_timestamp TEXT NOT NULL, exit_timestamp TEXT,
  stop_loss REAL, take_profit REAL,
  slippage_pct REAL NOT NULL DEFAULT 0,
  gas_fee_usd REAL NOT NULL DEFAULT 0, commission_usd REAL NOT NULL DEFAULT 0,
  realized_pnl_usd REAL NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(entry_timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE TABLE IF NOT EXISTS positions (
  position_key TEXT PRIMARY KEY, venue TEXT NOT NULL, symbol TEXT NOT NULL,
  market_type TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
  entry_price REAL NOT NULL, mark_price REAL NOT NULL,
  stop_loss REAL NOT NULL, take_profit REAL NOT NULL,
  realized_pnl_usd REAL NOT NULL DEFAULT 0, unrealized_pnl_usd REAL NOT NULL DEFAULT 0,
  partial_closed INTEGER NOT NULL DEFAULT 0, breakeven_moved INTEGER NOT NULL DEFAULT 0,
  high_watermark REAL, low_watermark REAL, metadata_json TEXT NOT NULL,
  opened_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_equity (
  utc_date TEXT PRIMARY KEY, starting_equity_usd REAL NOT NULL,
  latest_equity_usd REAL NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS controls (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_approvals (
  approval_id TEXT PRIMARY KEY,
  signal_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  result TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_status
  ON pending_approvals(status, created_at);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY, state TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


class TradeStore:
    def __init__(self, path: Path):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    def _db(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("TradeStore is not open.")
        return self.db

    async def event(self, level: str, event_type: str, message: str,
                    venue: str | None = None, symbol: str | None = None,
                    metadata: dict[str, Any] | None = None) -> None:
        await self._db().execute(
            "INSERT INTO events(timestamp,level,event_type,venue,symbol,message,metadata_json) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), level, event_type, venue, symbol,
             message[:1000], json.dumps(metadata or {}, separators=(",", ":"))),
        )
        await self._db().commit()

    async def record_trade(self, trade: dict[str, Any]) -> None:
        cols = (
            "client_order_id,venue,symbol,market_type,side,status,mode,quantity,average_price,"
            "entry_timestamp,exit_timestamp,stop_loss,take_profit,slippage_pct,gas_fee_usd,"
            "commission_usd,realized_pnl_usd,metadata_json"
        )
        values = (
            trade["client_order_id"], trade["venue"], trade["symbol"], trade["market_type"],
            trade["side"], trade["status"], trade["mode"], trade["quantity"],
            trade["average_price"], trade["entry_timestamp"], trade.get("exit_timestamp"),
            trade.get("stop_loss"), trade.get("take_profit"), trade.get("slippage_pct", 0),
            trade.get("gas_fee_usd", 0), trade.get("commission_usd", 0),
            trade.get("realized_pnl_usd", 0), json.dumps(trade.get("metadata", {})),
        )
        await self._db().execute(
            f"INSERT OR IGNORE INTO trades({cols}) VALUES({','.join('?' for _ in values)})", values
        )
        await self._db().commit()

    async def upsert_position(self, p: dict[str, Any]) -> None:
        await self._db().execute(
            """INSERT INTO positions(
              position_key,venue,symbol,market_type,side,quantity,entry_price,mark_price,
              stop_loss,take_profit,realized_pnl_usd,unrealized_pnl_usd,partial_closed,
              breakeven_moved,high_watermark,low_watermark,metadata_json,opened_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(position_key) DO UPDATE SET
              quantity=excluded.quantity,mark_price=excluded.mark_price,
              stop_loss=excluded.stop_loss,take_profit=excluded.take_profit,
              realized_pnl_usd=excluded.realized_pnl_usd,
              unrealized_pnl_usd=excluded.unrealized_pnl_usd,
              partial_closed=excluded.partial_closed,breakeven_moved=excluded.breakeven_moved,
              high_watermark=excluded.high_watermark,low_watermark=excluded.low_watermark,
              metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (p["position_key"],p["venue"],p["symbol"],p["market_type"],p["side"],p["quantity"],
             p["entry_price"],p["mark_price"],p["stop_loss"],p["take_profit"],
             p.get("realized_pnl_usd",0),p.get("unrealized_pnl_usd",0),int(p.get("partial_closed",False)),
             int(p.get("breakeven_moved",False)),p.get("high_watermark"),p.get("low_watermark"),
             json.dumps(p.get("metadata",{})),p["opened_at"],p["updated_at"]),
        )
        await self._db().commit()

    async def remove_position(self, key: str) -> None:
        await self._db().execute("DELETE FROM positions WHERE position_key=?", (key,))
        await self._db().commit()

    async def positions(self) -> list[dict[str, Any]]:
        cur = await self._db().execute("SELECT * FROM positions ORDER BY opened_at")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def trades_since(self, iso_timestamp: str) -> list[dict[str, Any]]:
        cur = await self._db().execute("SELECT * FROM trades WHERE entry_timestamp>=? ORDER BY id", (iso_timestamp,))
        return [dict(r) for r in await cur.fetchall()]

    async def query_trades(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 500)
        if symbol:
            cur = await self._db().execute(
                """SELECT * FROM trades WHERE symbol=?
                   ORDER BY COALESCE(exit_timestamp, entry_timestamp) DESC LIMIT ?""",
                (symbol, limit),
            )
        else:
            cur = await self._db().execute(
                """SELECT * FROM trades
                   ORDER BY COALESCE(exit_timestamp, entry_timestamp) DESC LIMIT ?""",
                (limit,),
            )
        return [dict(row) for row in await cur.fetchall()]

    async def query_events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 500)
        cur = await self._db().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cur.fetchall()]

    async def closed_trades_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        cur = await self._db().execute(
            """SELECT * FROM trades
               WHERE status IN ('closed','partial_closed')
                 AND exit_timestamp IS NOT NULL
                 AND exit_timestamp >= ? AND exit_timestamp < ?
               ORDER BY exit_timestamp""",
            (start_iso, end_iso),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def set_control(self, key: str, value: str) -> None:
        await self._db().execute(
            "INSERT INTO controls(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        await self._db().commit()

    async def get_control(self, key: str, default: str = "") -> str:
        cur = await self._db().execute("SELECT value FROM controls WHERE key=?", (key,))
        row = await cur.fetchone()
        return str(row["value"]) if row else default

    async def set_daily_equity(self, date_key: str, equity: float) -> float:
        cur = await self._db().execute("SELECT starting_equity_usd FROM daily_equity WHERE utc_date=?", (date_key,))
        row = await cur.fetchone()
        if row is None:
            await self._db().execute(
                "INSERT INTO daily_equity(utc_date,starting_equity_usd,latest_equity_usd,updated_at) VALUES(?,?,?,?)",
                (date_key,equity,equity,datetime.now(timezone.utc).isoformat()),
            )
            await self._db().commit()
            return equity
        await self._db().execute(
            "UPDATE daily_equity SET latest_equity_usd=?,updated_at=? WHERE utc_date=?",
            (equity,datetime.now(timezone.utc).isoformat(),date_key),
        )
        await self._db().commit()
        return float(row["starting_equity_usd"])

    async def claim_idempotency(self, key: str, payload: dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._db().execute(
            "INSERT OR IGNORE INTO idempotency(key,state,payload_json,created_at,updated_at) VALUES(?,?,?,?,?)",
            (key,"pending",json.dumps(payload),now,now),
        )
        await self._db().commit()
        return cur.rowcount == 1


    async def create_pending_approval(
        self, approval_id: str, signal_key: str, expires_at: str,
        payload: dict[str, Any],
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._db().execute(
            """INSERT OR IGNORE INTO pending_approvals
               (approval_id,signal_key,status,created_at,expires_at,payload_json)
               VALUES(?,?,'PENDING',?,?,?)""",
            (approval_id, signal_key, now, expires_at,
             json.dumps(payload, separators=(",", ":"), allow_nan=False)),
        )
        await self._db().commit()
        return cur.rowcount == 1

    async def get_pending_approval(self, approval_id: str) -> dict[str, Any] | None:
        await self.expire_stale_approvals()
        cur = await self._db().execute(
            "SELECT * FROM pending_approvals WHERE approval_id=?",
            (approval_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    async def pending_approvals(self, limit: int = 20) -> list[dict[str, Any]]:
        await self.expire_stale_approvals()
        bounded = min(max(int(limit), 1), 100)
        cur = await self._db().execute(
            """SELECT * FROM pending_approvals WHERE status='PENDING'
               ORDER BY created_at LIMIT ?""",
            (bounded,),
        )
        rows = await cur.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result


    async def expire_stale_approvals(self) -> int:
        """Mark old proposals expired so Telegram never displays stale actionable buttons."""
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._db().execute(
            """UPDATE pending_approvals
               SET status='EXPIRED', decided_at=?, decided_by='system',
                   result='Approval window elapsed; no order was submitted.'
               WHERE status='PENDING' AND expires_at<=?""",
            (now, now),
        )
        await self._db().commit()
        return max(0, int(cur.rowcount))

    async def recover_interrupted_approvals(self) -> int:
        """Fail closed after a process restart during an approval/execution operation.

        A PROCESSING approval may have crossed the provider submission boundary
        before the previous process died. It must never be retried automatically.
        """
        now = datetime.now(timezone.utc).isoformat()
        db = self._db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                """UPDATE pending_approvals
                   SET status='RECONCILIATION_REQUIRED', decided_at=?,
                       decided_by='startup-recovery',
                       result='9N8 restarted while this proposal was processing. '
                              'Inspect provider orders, fills and balances before resuming.'
                   WHERE status='PROCESSING'""",
                (now,),
            )
            count = max(0, int(cur.rowcount))
            if count:
                for key, value in (
                    ("execution_reconciliation_required", "true"),
                    ("kill_switch", "true"),
                    ("paused", "true"),
                ):
                    await db.execute(
                        """INSERT INTO controls(key,value,updated_at) VALUES(?,?,?)
                           ON CONFLICT(key) DO UPDATE SET
                             value=excluded.value,updated_at=excluded.updated_at""",
                        (key, value, now),
                    )
                await db.execute(
                    """INSERT INTO events(
                         timestamp,level,event_type,venue,symbol,message,metadata_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        now, "CRITICAL", "interrupted_approval_recovery", None, None,
                        f"Recovered {count} interrupted trade approval(s). New entries are locked until manual reconciliation.",
                        json.dumps({"count": count}, separators=(",", ":")),
                    ),
                )
            await db.commit()
            return count
        except Exception:
            await db.rollback()
            raise

    async def claim_pending_approval(self, approval_id: str) -> bool:
        cur = await self._db().execute(
            """UPDATE pending_approvals SET status='PROCESSING'
               WHERE approval_id=? AND status='PENDING'""",
            (approval_id,),
        )
        await self._db().commit()
        return cur.rowcount == 1

    async def decide_pending_approval(
        self, approval_id: str, status: str, decided_by: str, result: str,
    ) -> None:
        allowed = {"APPROVED", "DENIED", "EXPIRED", "FAILED", "RECONCILIATION_REQUIRED"}
        if status not in allowed:
            raise ValueError("Invalid approval state.")
        await self._db().execute(
            """UPDATE pending_approvals
               SET status=?,decided_at=?,decided_by=?,result=?
               WHERE approval_id=?""",
            (status, datetime.now(timezone.utc).isoformat(),
             str(decided_by)[:100], str(result)[:1000], approval_id),
        )
        await self._db().commit()

    async def update_idempotency(self, key: str, state: str) -> None:
        await self._db().execute(
            "UPDATE idempotency SET state=?,updated_at=? WHERE key=?",
            (state,datetime.now(timezone.utc).isoformat(),key),
        )
        await self._db().commit()
