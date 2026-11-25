"""Persistent spot paper-trading ledger using live quotes, never exchange orders."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class PaperTradingError(RuntimeError):
    """A safe, expected paper-trading rejection."""


def _decimal(value: Any, label: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PaperTradingError(f"{label} must be a valid number.") from exc
    if not result.is_finite():
        raise PaperTradingError(f"{label} must be finite.")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass(frozen=True)
class PaperConfig:
    initial_balance: Decimal = Decimal("1000")
    fee_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")
    risk_per_trade_pct: Decimal = Decimal("1")
    max_position_pct: Decimal = Decimal("20")
    daily_loss_limit_pct: Decimal = Decimal("3")
    hard_stop_loss_pct: Decimal = Decimal("5")
    tp1_target_pct: Decimal = Decimal("10")
    tp1_close_pct: Decimal = Decimal("50")
    trailing_activation_pct: Decimal = Decimal("8")
    trailing_stop_pct: Decimal = Decimal("4")
    max_hold_minutes: int = 240
    stagnation_profit_pct: Decimal = Decimal("1")
    max_open_positions: int = 3
    quote_currency: str = "USDT"

    def __post_init__(self) -> None:
        positive = (
            "initial_balance", "hard_stop_loss_pct", "tp1_target_pct",
            "trailing_activation_pct", "trailing_stop_pct",
        )
        for field in positive:
            if _decimal(getattr(self, field), field) <= 0:
                raise ValueError(f"{field} must be greater than zero.")
        if not Decimal("0") <= self.fee_rate < Decimal("1"):
            raise ValueError("fee_rate must be between zero and one.")
        if not Decimal("0") <= self.slippage_bps <= Decimal("500"):
            raise ValueError("slippage_bps must be between zero and 500.")
        if not Decimal("0") < self.risk_per_trade_pct <= Decimal("5"):
            raise ValueError("risk_per_trade_pct must be greater than zero and at most 5.")
        if not Decimal("0") < self.max_position_pct <= Decimal("100"):
            raise ValueError("max_position_pct must be greater than zero and at most 100.")
        if not Decimal("0") < self.daily_loss_limit_pct <= Decimal("25"):
            raise ValueError("daily_loss_limit_pct must be greater than zero and at most 25.")
        if not Decimal("0") < self.tp1_close_pct <= Decimal("100"):
            raise ValueError("tp1_close_pct must be greater than zero and at most 100.")
        if not 1 <= int(self.max_hold_minutes) <= 10080:
            raise ValueError("max_hold_minutes must be between 1 and 10080.")
        if int(self.max_open_positions) < 1:
            raise ValueError("max_open_positions must be at least one.")


class PaperTradingEngine:
    """SQLite-backed virtual account. Prices are supplied by the live market adapter."""

    VALID_STRATEGIES = {"momentum", "trend", "mean_reversion", "grid"}

    def __init__(self, db_path: str, config: PaperConfig | None = None):
        self.db_path = db_path
        self.config = config or PaperConfig()
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_balance TEXT NOT NULL,
                    cash_balance TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    daily_realized_pnl TEXT NOT NULL,
                    daily_start_equity TEXT NOT NULL,
                    daily_date TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    strategy TEXT NOT NULL DEFAULT 'momentum',
                    paused_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    ticket TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    initial_quantity TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    entry_fee_remaining TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    highest_price TEXT NOT NULL,
                    last_price TEXT NOT NULL,
                    tp1_hit INTEGER NOT NULL DEFAULT 0,
                    entry_reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    closed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_paper_positions_status
                    ON paper_positions(status, symbol);

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    exit_price TEXT NOT NULL,
                    gross_pnl TEXT NOT NULL,
                    entry_fee TEXT NOT NULL,
                    exit_fee TEXT NOT NULL,
                    net_pnl TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                );
                """
            )
            now = _now()
            initial = str(self.config.initial_balance)
            db.execute(
                """
                INSERT OR IGNORE INTO paper_account
                (id, initial_balance, cash_balance, realized_pnl, daily_realized_pnl,
                 daily_start_equity, daily_date, enabled, strategy, paused_reason,
                 created_at, updated_at)
                VALUES (1, ?, ?, '0', '0', ?, ?, 0, 'momentum', NULL, ?, ?)
                """,
                (initial, initial, initial, _utc_day(), now, now),
            )

    def _account(self, db: sqlite3.Connection) -> sqlite3.Row:
        row = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if row is None:
            raise PaperTradingError("Paper account could not be initialized.")
        return row

    def _equity(self, db: sqlite3.Connection) -> Decimal:
        account = self._account(db)
        cash = _decimal(account["cash_balance"], "cash balance")
        rows = db.execute(
            "SELECT quantity, last_price FROM paper_positions WHERE status='OPEN'"
        ).fetchall()
        market_value = sum(
            (_decimal(row["quantity"]) * _decimal(row["last_price"]) for row in rows),
            Decimal("0"),
        )
        return cash + market_value

    def _roll_day(self, db: sqlite3.Connection) -> None:
        account = self._account(db)
        today = _utc_day()
        if account["daily_date"] == today:
            return
        equity = self._equity(db)
        paused_reason = account["paused_reason"]
        if paused_reason and str(paused_reason).startswith("DAILY_DRAWDOWN"):
            paused_reason = None
        db.execute(
            """
            UPDATE paper_account
            SET daily_date=?, daily_realized_pnl='0', daily_start_equity=?,
                paused_reason=?, updated_at=?
            WHERE id=1
            """,
            (today, str(equity), paused_reason, _now()),
        )

    def _check_daily_drawdown(self, db: sqlite3.Connection) -> Decimal:
        self._roll_day(db)
        account = self._account(db)
        start = _decimal(account["daily_start_equity"], "daily start equity")
        equity = self._equity(db)
        if start <= 0:
            return Decimal("0")
        drawdown_pct = max(Decimal("0"), (start - equity) / start * Decimal("100"))
        if drawdown_pct >= self.config.daily_loss_limit_pct:
            reason = (
                f"DAILY_DRAWDOWN_LIMIT: equity drawdown {drawdown_pct:.2f}% "
                f"reached the configured {self.config.daily_loss_limit_pct}% ceiling."
            )
            db.execute(
                "UPDATE paper_account SET enabled=0, paused_reason=?, updated_at=? WHERE id=1",
                (reason, _now()),
            )
        return drawdown_pct

    def is_enabled(self) -> bool:
        with self._db() as db:
            self._roll_day(db)
            account = self._account(db)
            return bool(account["enabled"]) and not str(account["paused_reason"] or "").startswith("DAILY_DRAWDOWN")

    def set_enabled(self, enabled: bool) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._roll_day(db)
            account = self._account(db)
            if enabled and str(account["paused_reason"] or "").startswith("DAILY_DRAWDOWN"):
                raise PaperTradingError(
                    "The daily drawdown circuit breaker is active. It will reset at the next UTC day; new paper entries are blocked."
                )
            db.execute(
                "UPDATE paper_account SET enabled=?, paused_reason=NULL, updated_at=? WHERE id=1",
                (1 if enabled else 0, _now()),
            )

    def strategy(self) -> str:
        with self._db() as db:
            return str(self._account(db)["strategy"])

    def set_strategy(self, name: str) -> None:
        strategy = str(name).strip().lower()
        if strategy not in self.VALID_STRATEGIES:
            raise PaperTradingError(
                "Choose one of: momentum, trend, mean_reversion, grid."
            )
        with self._db() as db:
            db.execute("UPDATE paper_account SET strategy=?, updated_at=? WHERE id=1", (strategy, _now()))

    def suggest_quote_budget(self) -> Decimal:
        """Size by configured loss-at-stop, then cap by allocation and available cash."""
        with self._db() as db:
            self._roll_day(db)
            drawdown = self._check_daily_drawdown(db)
            if drawdown >= self.config.daily_loss_limit_pct:
                return Decimal("0")
            account = self._account(db)
            equity = self._equity(db)
            cash = _decimal(account["cash_balance"])
            risk_budget = equity * self.config.risk_per_trade_pct / Decimal("100")
            stop_fraction = self.config.hard_stop_loss_pct / Decimal("100")
            stop_risk_notional = risk_budget / stop_fraction
            allocation_cap = equity * self.config.max_position_pct / Decimal("100")
            notional = min(stop_risk_notional, allocation_cap)
            budget_with_fee = notional * (Decimal("1") + self.config.fee_rate)
            return max(Decimal("0"), min(budget_with_fee, cash))

    def open_position(
        self,
        symbol: str,
        quote_budget: Decimal | str | float,
        *,
        bid: Decimal | str | float,
        ask: Decimal | str | float,
        reason: str = "MANUAL_PAPER_ENTRY",
    ) -> dict[str, Any]:
        symbol = str(symbol).strip().upper()
        if not symbol or len(symbol) > 30:
            raise PaperTradingError("A valid market symbol is required.")
        budget = _decimal(quote_budget, "Quote amount")
        bid_price = _decimal(bid, "Bid")
        ask_price = _decimal(ask, "Ask")
        if budget <= 0 or bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
            raise PaperTradingError("Quote amount and bid/ask prices must be positive and valid.")

        slip = self.config.slippage_bps / Decimal("10000")
        fill_price = ask_price * (Decimal("1") + slip)
        fee_multiplier = Decimal("1") + self.config.fee_rate
        quantity = budget / (fill_price * fee_multiplier)
        entry_notional = quantity * fill_price
        entry_fee = entry_notional * self.config.fee_rate
        total_cost = entry_notional + entry_fee
        now = _now()
        ticket = uuid.uuid4().hex[:12].upper()

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._roll_day(db)
            self._check_daily_drawdown(db)
            account = self._account(db)
            if str(account["paused_reason"] or "").startswith("DAILY_DRAWDOWN"):
                raise PaperTradingError("Daily drawdown circuit breaker is active; new entries are blocked.")
            cash = _decimal(account["cash_balance"])
            equity = self._equity(db)
            allocation_cap = equity * self.config.max_position_pct / Decimal("100")
            if budget > allocation_cap:
                raise PaperTradingError(
                    f"Quote budget exceeds the per-position cap ({allocation_cap:.4f} {self.config.quote_currency})."
                )
            if total_cost > cash:
                raise PaperTradingError("Insufficient paper cash after estimated fees.")
            existing = db.execute(
                "SELECT 1 FROM paper_positions WHERE status='OPEN' AND symbol=?",
                (symbol,),
            ).fetchone()
            if existing:
                raise PaperTradingError(f"A paper position for {symbol} is already open.")
            open_count = db.execute(
                "SELECT COUNT(*) AS n FROM paper_positions WHERE status='OPEN'"
            ).fetchone()["n"]
            if int(open_count) >= self.config.max_open_positions:
                raise PaperTradingError("Maximum number of simultaneous paper positions reached.")
            db.execute(
                """
                INSERT INTO paper_positions
                (ticket, symbol, quantity, initial_quantity, entry_price, entry_fee_remaining,
                 opened_at, highest_price, last_price, tp1_hit, entry_reason, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'OPEN')
                """,
                (
                    ticket, symbol, str(quantity), str(quantity), str(fill_price),
                    str(entry_fee), now, str(bid_price), str(bid_price), str(reason)[:500],
                ),
            )
            db.execute(
                "UPDATE paper_account SET cash_balance=?, updated_at=? WHERE id=1",
                (str(cash - total_cost), now),
            )
            self._check_daily_drawdown(db)

        return {
            "ticket": ticket,
            "symbol": symbol,
            "quantity": quantity,
            "entry_price": fill_price,
            "entry_fee": entry_fee,
            "quote_spent": total_cost,
            "reason": str(reason),
            "opened_at": now,
        }

    def _close_quantity(
        self,
        db: sqlite3.Connection,
        position: sqlite3.Row,
        quantity_to_close: Decimal,
        bid_price: Decimal,
        reason: str,
    ) -> dict[str, Any]:
        current_quantity = _decimal(position["quantity"])
        if quantity_to_close <= 0 or quantity_to_close > current_quantity:
            raise PaperTradingError("Invalid paper close quantity.")
        slip = self.config.slippage_bps / Decimal("10000")
        exit_price = bid_price * (Decimal("1") - slip)
        exit_notional = quantity_to_close * exit_price
        exit_fee = exit_notional * self.config.fee_rate
        remaining_entry_fee = _decimal(position["entry_fee_remaining"])
        entry_fee_allocated = remaining_entry_fee * quantity_to_close / current_quantity
        entry_price = _decimal(position["entry_price"])
        gross_pnl = (exit_price - entry_price) * quantity_to_close
        net_pnl = gross_pnl - entry_fee_allocated - exit_fee
        now = _now()

        account = self._account(db)
        cash = _decimal(account["cash_balance"]) + exit_notional - exit_fee
        realized = _decimal(account["realized_pnl"]) + net_pnl
        daily_realized = _decimal(account["daily_realized_pnl"]) + net_pnl
        remaining_quantity = current_quantity - quantity_to_close
        remaining_fee = remaining_entry_fee - entry_fee_allocated
        fully_closed = remaining_quantity <= Decimal("0.000000000000000001")

        db.execute(
            """
            UPDATE paper_account
            SET cash_balance=?, realized_pnl=?, daily_realized_pnl=?, updated_at=?
            WHERE id=1
            """,
            (str(cash), str(realized), str(daily_realized), now),
        )
        db.execute(
            """
            INSERT INTO paper_trades
            (ticket, symbol, side, quantity, entry_price, exit_price, gross_pnl,
             entry_fee, exit_fee, net_pnl, reason, opened_at, closed_at)
            VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position["ticket"], position["symbol"], str(quantity_to_close),
                str(entry_price), str(exit_price), str(gross_pnl),
                str(entry_fee_allocated), str(exit_fee), str(net_pnl), reason,
                position["opened_at"], now,
            ),
        )
        if fully_closed:
            db.execute(
                """
                UPDATE paper_positions
                SET quantity='0', entry_fee_remaining='0', last_price=?, status='CLOSED', closed_at=?
                WHERE ticket=?
                """,
                (str(bid_price), now, position["ticket"]),
            )
        else:
            db.execute(
                """
                UPDATE paper_positions
                SET quantity=?, entry_fee_remaining=?, last_price=?
                WHERE ticket=?
                """,
                (str(remaining_quantity), str(remaining_fee), str(bid_price), position["ticket"]),
            )
        return {
            "ticket": position["ticket"],
            "symbol": position["symbol"],
            "reason": reason,
            "quantity": quantity_to_close,
            "exit_price": exit_price,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "remaining_quantity": Decimal("0") if fully_closed else remaining_quantity,
            "partial": not fully_closed,
            "closed_at": now if fully_closed else None,
        }

    def close_position(
        self,
        ticket: str,
        *,
        bid: Decimal | str | float,
        reason: str = "MANUAL_PAPER_CLOSE",
    ) -> dict[str, Any]:
        bid_price = _decimal(bid, "Bid")
        if bid_price <= 0:
            raise PaperTradingError("Bid price must be greater than zero.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            position = db.execute(
                "SELECT * FROM paper_positions WHERE ticket=? AND status='OPEN'",
                (str(ticket).upper(),),
            ).fetchone()
            if position is None:
                raise PaperTradingError("Open paper position not found.")
            result = self._close_quantity(
                db, position, _decimal(position["quantity"]), bid_price, str(reason)[:80]
            )
            self._check_daily_drawdown(db)
            return result

    def update_prices(self, quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Mark positions and evaluate the configured automatic paper exits."""
        events: list[dict[str, Any]] = []
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._roll_day(db)
            positions = db.execute(
                "SELECT * FROM paper_positions WHERE status='OPEN'"
            ).fetchall()
            for position in positions:
                quote = quotes.get(str(position["symbol"]).upper())
                if not quote:
                    continue
                try:
                    bid = _decimal(quote.get("bidPx") or quote.get("bid"), "Bid")
                    ask = _decimal(quote.get("askPx") or quote.get("ask"), "Ask")
                    last = _decimal(quote.get("last") or bid, "Last")
                except PaperTradingError:
                    continue
                if bid <= 0 or ask <= 0 or last <= 0 or ask < bid:
                    continue
                entry = _decimal(position["entry_price"])
                old_high = _decimal(position["highest_price"])
                high = max(old_high, bid, last)
                db.execute(
                    "UPDATE paper_positions SET highest_price=?, last_price=? WHERE ticket=?",
                    (str(high), str(bid), position["ticket"]),
                )
                current_pct = (bid - entry) / entry * Decimal("100")
                highest_profit_pct = (high - entry) / entry * Decimal("100")
                elapsed = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(str(position["opened_at"]))
                ).total_seconds() / 60
                quantity = _decimal(position["quantity"])
                reason: str | None = None
                close_quantity = quantity

                # Exit precedence is deterministic: protection, first target, trail, then time.
                if current_pct <= -self.config.hard_stop_loss_pct:
                    reason = "HARD_STOP_LOSS"
                elif not bool(position["tp1_hit"]) and current_pct >= self.config.tp1_target_pct:
                    close_quantity = min(
                        quantity,
                        _decimal(position["initial_quantity"])
                        * self.config.tp1_close_pct / Decimal("100"),
                    )
                    reason = "TAKE_PROFIT_1"
                    db.execute(
                        "UPDATE paper_positions SET tp1_hit=1 WHERE ticket=?",
                        (position["ticket"],),
                    )
                else:
                    trailing_active = (
                        highest_profit_pct >= self.config.trailing_activation_pct
                        or bool(position["tp1_hit"])
                    )
                    drawdown_from_peak = (
                        (high - bid) / high * Decimal("100") if high > 0 else Decimal("0")
                    )
                    if trailing_active and drawdown_from_peak >= self.config.trailing_stop_pct:
                        reason = "TRAILING_STOP"
                    elif elapsed >= self.config.max_hold_minutes and current_pct < self.config.stagnation_profit_pct:
                        reason = "TIME_KILL_SWITCH"

                if reason:
                    event = self._close_quantity(db, position, close_quantity, bid, reason)
                    event["pnl_pct_at_exit"] = current_pct
                    events.append(event)
            self._check_daily_drawdown(db)
        return events

    def positions(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
            return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._roll_day(db)
            drawdown = self._check_daily_drawdown(db)
            account = self._account(db)
            equity = self._equity(db)
            cash = _decimal(account["cash_balance"])
            realized = _decimal(account["realized_pnl"])
            unrealized = equity - cash - sum(
                (
                    _decimal(row["quantity"]) * _decimal(row["entry_price"])
                    + _decimal(row["entry_fee_remaining"])
                    for row in db.execute(
                        "SELECT quantity, entry_price, entry_fee_remaining FROM paper_positions WHERE status='OPEN'"
                    ).fetchall()
                ),
                Decimal("0"),
            )
            count = db.execute(
                "SELECT COUNT(*) AS n FROM paper_positions WHERE status='OPEN'"
            ).fetchone()["n"]
            return {
                "initial_balance": _decimal(account["initial_balance"]),
                "cash_balance": cash,
                "equity": equity,
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "daily_realized_pnl": _decimal(account["daily_realized_pnl"]),
                "daily_drawdown_pct": drawdown,
                "daily_loss_limit_pct": self.config.daily_loss_limit_pct,
                "enabled": bool(account["enabled"]),
                "strategy": account["strategy"],
                "paused_reason": account["paused_reason"],
                "open_positions": int(count),
                "quote_currency": self.config.quote_currency,
            }

    def reset(self, new_balance: Decimal | str | float) -> None:
        balance = _decimal(new_balance, "New balance")
        if balance <= 0:
            raise PaperTradingError("New paper balance must be greater than zero.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            open_count = db.execute(
                "SELECT COUNT(*) AS n FROM paper_positions WHERE status='OPEN'"
            ).fetchone()["n"]
            if int(open_count):
                raise PaperTradingError("Close all open paper positions before resetting the account.")
            now = _now()
            db.execute("DELETE FROM paper_trades")
            db.execute("DELETE FROM paper_positions")
            db.execute(
                """
                UPDATE paper_account SET initial_balance=?, cash_balance=?, realized_pnl='0',
                    daily_realized_pnl='0', daily_start_equity=?, daily_date=?, enabled=0,
                    paused_reason=NULL, updated_at=? WHERE id=1
                """,
                (str(balance), str(balance), str(balance), _utc_day(), now),
            )
