"""Multi-user SQLite paper ledger; market data is read-only and never routed to MT5."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence

from .mt5_provider import MT5Provider, MT5SymbolInfo


class PaperTradingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperPosition:
    ticket: int
    symbol: str
    side: str
    lots: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: str
    profit_currency: str
    owner_id: int = 0
    be_trigger_points: int = 0
    be_offset_points: int = 0
    be_activated: bool = False


@dataclass(frozen=True)
class PaperClose:
    ticket: int
    symbol: str
    side: str
    lots: float
    entry_price: float
    close_price: float
    pnl_quote: float
    currency: str
    closed_at: str
    owner_id: int = 0
    reason: str = "MANUAL"


class PaperBroker:
    """Simulated broker backed by SQLite and a read-only MT5 quote provider."""

    def __init__(
        self,
        provider: MT5Provider,
        db_path: str,
        initial_balance: float = 10000.0,
        max_lots: float = 0.10,
        max_open_positions: int = 3,
        slippage_points: int = 2,
        max_tick_age_seconds: int = 900,
    ):
        self.provider = provider
        self.db_path = db_path
        self.initial_balance = float(initial_balance)
        self.max_lots = float(max_lots)
        self.max_open_positions = int(max_open_positions)
        self.slippage_points = int(slippage_points)
        self.max_tick_age_seconds = int(max_tick_age_seconds)
        if not isfinite(self.initial_balance) or self.initial_balance <= 0:
            raise ValueError("Paper reference capital must be a positive finite number.")
        if not isfinite(self.max_lots) or self.max_lots <= 0:
            raise ValueError("Paper maximum lot size must be a positive finite number.")
        if self.max_open_positions < 1 or self.max_tick_age_seconds < 1 or self.slippage_points < 0:
            raise ValueError("Invalid paper risk configuration.")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_positions (
                    ticket INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    lots REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
                    close_price REAL,
                    closed_at TEXT,
                    pnl_quote REAL,
                    profit_currency TEXT NOT NULL,
                    contract_size REAL NOT NULL,
                    point REAL NOT NULL,
                    owner_id INTEGER NOT NULL DEFAULT 0,
                    be_trigger_points INTEGER NOT NULL DEFAULT 0,
                    be_offset_points INTEGER NOT NULL DEFAULT 0,
                    be_activated INTEGER NOT NULL DEFAULT 0,
                    close_reason TEXT
                )
            """)
            # Safe, additive migration for databases created by earlier 9N8 builds.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(paper_positions)").fetchall()}
            migrations = {
                "owner_id": "ALTER TABLE paper_positions ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
                "be_trigger_points": "ALTER TABLE paper_positions ADD COLUMN be_trigger_points INTEGER NOT NULL DEFAULT 0",
                "be_offset_points": "ALTER TABLE paper_positions ADD COLUMN be_offset_points INTEGER NOT NULL DEFAULT 0",
                "be_activated": "ALTER TABLE paper_positions ADD COLUMN be_activated INTEGER NOT NULL DEFAULT 0",
                "close_reason": "ALTER TABLE paper_positions ADD COLUMN close_reason TEXT",
            }
            for name, sql in migrations.items():
                if name not in columns:
                    conn.execute(sql)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_owner_status ON paper_positions(owner_id,status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_closed_at ON paper_positions(status,closed_at)")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _fresh_tick(self, symbol: str):
        tick = self.provider.tick(symbol)
        if tick.time_utc == "unknown":
            raise PaperTradingError(f"MT5 tick time is unavailable for {symbol}; refusing a paper fill.")
        try:
            tick_time = datetime.fromisoformat(tick.time_utc)
        except ValueError as exc:
            raise PaperTradingError(f"MT5 returned an invalid tick timestamp for {symbol}.") from exc
        age = (datetime.now(timezone.utc) - tick_time.astimezone(timezone.utc)).total_seconds()
        if age < -60 or age > self.max_tick_age_seconds:
            raise PaperTradingError(f"MT5 quote for {symbol} is stale ({age:.0f}s old). Wait for a fresh market tick.")
        return tick

    @staticmethod
    def _validate_lots(lots: float, info: MT5SymbolInfo, max_lots: float) -> float:
        if not isfinite(lots) or lots <= 0:
            raise PaperTradingError("Lot size must be a positive finite number.")
        if lots > max_lots:
            raise PaperTradingError(f"Lot size exceeds your 9N8 paper limit of {max_lots:g} lots.")
        if lots < info.volume_min - 1e-9 or lots > info.volume_max + 1e-9:
            raise PaperTradingError(f"Lot size must be within broker symbol bounds {info.volume_min:g}–{info.volume_max:g}.")
        units = lots / info.volume_step
        if abs(units - round(units)) > 1e-6:
            raise PaperTradingError(f"Lot size must follow broker volume step {info.volume_step:g}.")
        return lots

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> PaperPosition:
        return PaperPosition(
            ticket=int(row["ticket"]), symbol=str(row["symbol"]), side=str(row["side"]),
            lots=float(row["lots"]), entry_price=float(row["entry_price"]),
            stop_loss=float(row["stop_loss"]), take_profit=float(row["take_profit"]),
            opened_at=str(row["opened_at"]), profit_currency=str(row["profit_currency"]),
            owner_id=int(row["owner_id"] or 0),
            be_trigger_points=int(row["be_trigger_points"] or 0),
            be_offset_points=int(row["be_offset_points"] or 0),
            be_activated=bool(row["be_activated"]),
        )

    def open_position(
        self,
        symbol: str,
        side: str,
        lots: float,
        sl_points: int,
        tp_points: int,
        owner_id: int = 0,
        max_lots: float | None = None,
        max_open_positions: int | None = None,
        be_trigger_points: int = 0,
        be_offset_points: int = 0,
    ) -> PaperPosition:
        symbol = symbol.strip().upper()
        side = side.upper()
        if not symbol:
            raise PaperTradingError("A symbol is required.")
        if side not in {"BUY", "SELL"}:
            raise PaperTradingError("Side must be BUY or SELL.")
        if sl_points <= 0 or tp_points <= 0:
            raise PaperTradingError("Both stop-loss and take-profit distances must be positive points.")
        if int(owner_id) < 0:
            raise PaperTradingError("Invalid account owner.")
        if not 0 <= int(be_trigger_points) <= 1_000_000 or not 0 <= int(be_offset_points) <= 100_000:
            raise PaperTradingError("Break-even settings are outside supported limits.")
        if int(be_trigger_points) == 0 and int(be_offset_points) != 0:
            raise PaperTradingError("A break-even offset requires a positive trigger distance.")
        if int(be_trigger_points) > 0 and int(be_offset_points) >= int(be_trigger_points):
            raise PaperTradingError("Break-even offset must be smaller than its trigger distance.")
        user_max_lots = min(self.max_lots, float(max_lots if max_lots is not None else self.max_lots))
        user_max_positions = min(self.max_open_positions, int(max_open_positions if max_open_positions is not None else self.max_open_positions))
        if user_max_lots <= 0 or user_max_positions < 1:
            raise PaperTradingError("Your personal risk limits are invalid.")

        # Positive DEMO verification occurs inside connect(). Only read methods follow.
        self.provider.connect()
        info = self.provider.symbol_info(symbol)
        self._validate_lots(lots, info, user_max_lots)
        min_stop = max(1, info.stops_level)
        if sl_points < min_stop or tp_points < min_stop:
            raise PaperTradingError(f"SL and TP must each be at least {min_stop} points for {symbol}.")
        tick = self._fresh_tick(symbol)
        slippage = self.slippage_points * info.point
        entry = tick.ask + slippage if side == "BUY" else tick.bid - slippage
        if side == "BUY":
            stop_loss = entry - sl_points * info.point
            take_profit = entry + tp_points * info.point
        else:
            stop_loss = entry + sl_points * info.point
            take_profit = entry - tp_points * info.point
        opened_at = self._now()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE owner_id=? AND status='OPEN'", (int(owner_id),)
            ).fetchone()[0]
            if count >= user_max_positions:
                raise PaperTradingError(f"Your paper limit reached: max {user_max_positions} open positions.")
            cursor = conn.execute("""
                INSERT INTO paper_positions
                (symbol,side,lots,entry_price,stop_loss,take_profit,opened_at,status,
                 profit_currency,contract_size,point,owner_id,be_trigger_points,be_offset_points)
                VALUES (?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?)
            """, (symbol, side, lots, entry, stop_loss, take_profit, opened_at,
                  info.currency_profit or "UNKNOWN", info.contract_size, info.point, int(owner_id),
                  int(be_trigger_points), int(be_offset_points)))
            ticket = int(cursor.lastrowid)
        return PaperPosition(ticket, symbol, side, lots, entry, stop_loss, take_profit,
                             opened_at, info.currency_profit or "UNKNOWN", int(owner_id),
                             int(be_trigger_points), int(be_offset_points), False)

    def open_positions(self, owner_id: int | None = None) -> list[PaperPosition]:
        with self._connect() as conn:
            if owner_id is None:
                rows = conn.execute("SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY ticket").fetchall()
            else:
                rows = conn.execute("SELECT * FROM paper_positions WHERE status='OPEN' AND owner_id=? ORDER BY ticket", (int(owner_id),)).fetchall()
        return [self._row_to_position(row) for row in rows]

    def close_position(self, ticket: int, reason: str = "MANUAL", owner_id: int | None = None) -> PaperClose:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if owner_id is None:
                row = conn.execute("SELECT * FROM paper_positions WHERE ticket=? AND status='OPEN'", (ticket,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM paper_positions WHERE ticket=? AND status='OPEN' AND owner_id=?", (ticket, int(owner_id))).fetchone()
            if row is None:
                raise PaperTradingError(f"Open paper ticket {ticket} was not found in your account.")
            tick = self._fresh_tick(str(row["symbol"]))
            slippage = self.slippage_points * float(row["point"])
            close_price = tick.bid - slippage if row["side"] == "BUY" else tick.ask + slippage
            direction = 1.0 if row["side"] == "BUY" else -1.0
            pnl = (close_price - float(row["entry_price"])) * direction * float(row["lots"]) * float(row["contract_size"])
            closed_at = self._now()
            conn.execute("""
                UPDATE paper_positions SET status='CLOSED',close_price=?,closed_at=?,pnl_quote=?,close_reason=?
                WHERE ticket=? AND status='OPEN'
            """, (close_price, closed_at, pnl, reason, ticket))
        return PaperClose(ticket, str(row["symbol"]), str(row["side"]), float(row["lots"]),
                          float(row["entry_price"]), close_price, pnl, str(row["profit_currency"]),
                          closed_at, int(row["owner_id"] or 0), reason)

    def _activate_break_even(self, position: PaperPosition, bid: float, ask: float) -> PaperPosition:
        if position.be_activated or position.be_trigger_points <= 0:
            return position
        direction_points = ((bid - position.entry_price) if position.side == "BUY" else (position.entry_price - ask))
        favorable_points = direction_points / max(1e-12, self._position_point(position.ticket))
        if favorable_points < position.be_trigger_points:
            return position
        point = self._position_point(position.ticket)
        new_sl = (position.entry_price + position.be_offset_points * point) if position.side == "BUY" else (position.entry_price - position.be_offset_points * point)
        # A break-even adjustment must only tighten risk, never loosen the existing stop.
        improves = new_sl > position.stop_loss if position.side == "BUY" else new_sl < position.stop_loss
        if not improves:
            return position
        with self._connect() as conn:
            conn.execute("UPDATE paper_positions SET stop_loss=?,be_activated=1 WHERE ticket=? AND status='OPEN'", (new_sl, position.ticket))
        return PaperPosition(position.ticket, position.symbol, position.side, position.lots, position.entry_price,
                             new_sl, position.take_profit, position.opened_at, position.profit_currency,
                             position.owner_id, position.be_trigger_points, position.be_offset_points, True)

    def _position_point(self, ticket: int) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT point FROM paper_positions WHERE ticket=?", (ticket,)).fetchone()
        if row is None:
            raise PaperTradingError("Paper position disappeared while monitoring break-even.")
        point = float(row["point"])
        if point <= 0:
            raise PaperTradingError("Invalid point size stored for paper position.")
        return point

    def check_stops(self, owner_id: int | None = None) -> list[PaperClose]:
        """Update break-even stops and simulate SL/TP exits; local SQLite writes only."""
        closed: list[PaperClose] = []
        for position in self.open_positions(owner_id):
            tick = self._fresh_tick(position.symbol)
            position = self._activate_break_even(position, tick.bid, tick.ask)
            if position.side == "BUY":
                hit_sl = tick.bid <= position.stop_loss
                hit_tp = tick.bid >= position.take_profit
            else:
                hit_sl = tick.ask >= position.stop_loss
                hit_tp = tick.ask <= position.take_profit
            if hit_sl or hit_tp:
                if hit_tp:
                    reason = "TAKE_PROFIT"
                elif position.be_activated and (
                    (position.side == "BUY" and position.stop_loss >= position.entry_price)
                    or (position.side == "SELL" and position.stop_loss <= position.entry_price)
                ):
                    reason = "BREAK_EVEN" if position.be_offset_points == 0 else "TRAILING_STOP"
                else:
                    reason = "STOP_LOSS"
                closed.append(self.close_position(position.ticket, reason, owner_id=position.owner_id))
        return closed

    def history(self, limit: int = 10, owner_id: int | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            if owner_id is None:
                rows = conn.execute("SELECT * FROM paper_positions ORDER BY ticket DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM paper_positions WHERE owner_id=? ORDER BY ticket DESC LIMIT ?", (int(owner_id), limit)).fetchall()
        return [dict(row) for row in rows]

    def summary(self, owner_id: int | None = None) -> dict:
        where = "" if owner_id is None else " AND owner_id=?"
        params: tuple = () if owner_id is None else (int(owner_id),)
        with self._connect() as conn:
            open_count = int(conn.execute(f"SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'{where}", params).fetchone()[0])
            closed = conn.execute(f"""
                SELECT profit_currency, ROUND(SUM(pnl_quote), 8) AS pnl, COUNT(*) AS trades
                FROM paper_positions WHERE status='CLOSED'{where} GROUP BY profit_currency
            """, params).fetchall()
            total_trades = int(conn.execute(f"SELECT COUNT(*) FROM paper_positions WHERE 1=1{where}", params).fetchone()[0])
        return {
            "reference_capital": self.initial_balance,
            "open_positions": open_count,
            "total_trades": total_trades,
            "realized_by_currency": {str(r["profit_currency"]): {"pnl": float(r["pnl"] or 0), "trades": int(r["trades"])} for r in closed},
        }

    def period_stats(self, start_iso: str, end_iso: str, owner_ids: Sequence[int] | None = None) -> dict:
        """Aggregate closed simulated trades. owner_ids=None means all; [] means none."""
        clauses = ["status='CLOSED'", "closed_at>=?", "closed_at<?"]
        params: list = [start_iso, end_iso]
        if owner_ids is not None:
            ids = [int(value) for value in owner_ids]
            if not ids:
                return {"trades": 0, "wins": 0, "losses": 0, "breakevens": 0, "win_rate": 0.0,
                        "by_currency": {}, "be_moves": 0, "contributors": 0}
            clauses.append(f"owner_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT pnl_quote,profit_currency,close_reason,owner_id,be_activated FROM paper_positions WHERE {where}", params).fetchall()
        wins = sum(1 for row in rows if float(row["pnl_quote"] or 0) > 1e-8)
        losses = sum(1 for row in rows if float(row["pnl_quote"] or 0) < -1e-8)
        breakevens = len(rows) - wins - losses
        by_currency: dict[str, dict[str, float | int]] = {}
        for row in rows:
            currency = str(row["profit_currency"])
            bucket = by_currency.setdefault(currency, {"pnl": 0.0, "trades": 0})
            bucket["pnl"] = float(bucket["pnl"]) + float(row["pnl_quote"] or 0)
            bucket["trades"] = int(bucket["trades"]) + 1
        return {
            "trades": len(rows), "wins": wins, "losses": losses, "breakevens": breakevens,
            "win_rate": (wins / len(rows) * 100.0) if rows else 0.0,
            "by_currency": by_currency,
            "be_moves": sum(1 for row in rows if bool(row["be_activated"])),
            "contributors": len({int(row["owner_id"]) for row in rows}),
        }
