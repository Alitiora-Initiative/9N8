from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aiosqlite

from .database import PAPER_DEFAULT_SETTINGS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normal_position(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("token_amount_raw", "initial_cost_lamports", "realized_lamports",
                "peak_value_lamports", "last_value_lamports"):
        if item.get(key) is not None:
            item[key] = str(item[key])
    item["entry_price_usd"] = str(item.get("entry_price_usd") or "0")
    item["last_pnl_pct"] = str(item.get("last_pnl_pct") or "0")
    return item


class PaperStore:
    """Isolated virtual-account storage for SQLite and Neon.

    All records live in paper_* tables. This store has no transaction signer,
    wallet manager, or on-chain submission method.
    """

    def __init__(self, database: Any):
        self.db = database
        self.is_postgres = hasattr(database, "_pool")

    async def initialize(self) -> None:
        if self.is_postgres:
            required = [
                "n9n8_paper_settings", "n9n8_paper_accounts", "n9n8_paper_days",
                "n9n8_paper_positions", "n9n8_paper_trades",
            ]
            async with self.db._pool().acquire() as conn:
                found = await conn.fetchval(
                    """SELECT count(*) FROM unnest($1::text[]) AS t(name)
                       WHERE to_regclass('public.' || name) IS NOT NULL""",
                    required,
                )
            if int(found or 0) != len(required):
                raise RuntimeError(
                    "Paper trading schema is missing. Apply migrations/002_n9n8_paper_live_separation.sql."
                )
            return
        async with aiosqlite.connect(self.db.path) as conn:
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS paper_settings(
                    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                    config_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_accounts(
                    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                    starting_balance_lamports INTEGER NOT NULL,
                    cash_balance_lamports INTEGER NOT NULL,
                    realized_pnl_lamports INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_days(
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    day_utc TEXT NOT NULL, started_at TEXT NOT NULL,
                    PRIMARY KEY(telegram_id,day_utc)
                );
                CREATE TABLE IF NOT EXISTS paper_positions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    mint TEXT NOT NULL, launchpad TEXT NOT NULL,
                    token_amount_raw INTEGER NOT NULL, initial_cost_lamports INTEGER NOT NULL,
                    entry_price_usd TEXT NOT NULL DEFAULT '0',
                    status TEXT NOT NULL CHECK(status IN ('open','closed')),
                    opened_at TEXT NOT NULL, closed_at TEXT, exit_reason TEXT,
                    realized_lamports INTEGER NOT NULL DEFAULT 0,
                    peak_value_lamports INTEGER NOT NULL DEFAULT 0,
                    last_value_lamports INTEGER NOT NULL DEFAULT 0,
                    last_pnl_pct TEXT NOT NULL DEFAULT '0',
                    tp1_done INTEGER NOT NULL DEFAULT 0, tp2_done INTEGER NOT NULL DEFAULT 0,
                    buy_reference TEXT, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS paper_positions_user_status_idx
                    ON paper_positions(telegram_id,status,id);
                CREATE UNIQUE INDEX IF NOT EXISTS paper_positions_one_open_mint_idx
                    ON paper_positions(telegram_id,mint) WHERE status='open';
                CREATE TABLE IF NOT EXISTS paper_trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    position_id INTEGER REFERENCES paper_positions(id) ON DELETE SET NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy','sell','rejected')),
                    mint TEXT NOT NULL, launchpad TEXT,
                    token_amount_raw INTEGER NOT NULL DEFAULT 0,
                    sol_amount_lamports INTEGER NOT NULL DEFAULT 0,
                    fee_lamports INTEGER NOT NULL DEFAULT 0,
                    price_usd TEXT NOT NULL DEFAULT '0', reason TEXT,
                    reference_signature TEXT, quote_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS paper_trades_user_time_idx
                    ON paper_trades(telegram_id,created_at DESC);
            """)
            await conn.commit()

    async def users(self) -> list[dict[str, Any]]:
        if self.is_postgres:
            rows = await self.db._pool().fetch(
                """SELECT u.telegram_id,u.activation_paid,u.enabled,w.solana_address
                   FROM public.n9n8_users u
                   LEFT JOIN public.n9n8_wallet_vault w USING(telegram_id)
                   ORDER BY u.telegram_id"""
            )
            return [dict(row) for row in rows]
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT telegram_id,activation_paid,enabled,solana_address FROM users ORDER BY telegram_id"
            )
            return [dict(row) for row in await cur.fetchall()]

    async def enabled_users(self) -> list[int]:
        if self.is_postgres:
            rows = await self.db._pool().fetch(
                """SELECT telegram_id FROM public.n9n8_settings
                   WHERE COALESCE((config_json->>'paper_enabled')::boolean,FALSE)
                      OR COALESCE((config_json->>'live_enabled')::boolean,
                                  COALESCE((config_json->>'enabled')::boolean,FALSE))
                   ORDER BY telegram_id"""
            )
            return [int(row["telegram_id"]) for row in rows]
        async with aiosqlite.connect(self.db.path) as conn:
            cur = await conn.execute(
                """SELECT telegram_id FROM settings
                   WHERE json_extract(config_json,'$.paper_enabled')=1
                      OR json_extract(config_json,'$.live_enabled')=1
                      OR (json_extract(config_json,'$.live_enabled') IS NULL
                          AND json_extract(config_json,'$.enabled')=1)
                   ORDER BY telegram_id"""
            )
            return [int(row[0]) for row in await cur.fetchall()]

    async def mode_enabled(self, telegram_id: int, mode: str) -> bool:
        if mode not in {"paper","live"}:
            raise ValueError("mode must be paper or live")
        s = await self.db.get_settings(telegram_id)
        return bool(s.get("paper_enabled",False)) if mode=="paper" else bool(s.get("live_enabled",s.get("enabled",False)))

    async def set_mode_enabled(self, telegram_id: int, mode: str, enabled: bool) -> None:
        if mode not in {"paper","live"}:
            raise ValueError("mode must be paper or live")
        await self.db.ensure_user(telegram_id)
        s = await self.db.get_settings(telegram_id)
        s["paper_enabled"] = bool(s.get("paper_enabled",False))
        s["live_enabled"] = bool(s.get("live_enabled",s.get("enabled",False)))
        s["paper_enabled" if mode=="paper" else "live_enabled"] = bool(enabled)
        s["enabled"] = s["live_enabled"]
        await self.db.update_settings(telegram_id,{
            "paper_enabled":s["paper_enabled"],
            "live_enabled":s["live_enabled"],
            "enabled":s["enabled"],
        })

    async def settings(self, telegram_id: int, mode: str = "paper") -> dict[str, Any]:
        if mode=="live":
            return await self.db.get_settings(telegram_id)
        if mode!="paper":
            raise ValueError("mode must be live or paper")
        await self.db.ensure_user(telegram_id)
        if self.is_postgres:
            await self.db._pool().execute(
                """INSERT INTO public.n9n8_paper_settings(telegram_id,config_json)
                   VALUES($1,$2::jsonb) ON CONFLICT(telegram_id) DO NOTHING""",
                int(telegram_id),json.dumps(PAPER_DEFAULT_SETTINGS,separators=(",",":")),
            )
            raw=await self.db._pool().fetchval(
                "SELECT config_json FROM public.n9n8_paper_settings WHERE telegram_id=$1",int(telegram_id)
            )
            try: values=json.loads(raw) if isinstance(raw,str) else dict(raw or {})
            except (TypeError,json.JSONDecodeError): values={}
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_settings(telegram_id,config_json,updated_at) VALUES(?,?,?)",
                    (int(telegram_id),json.dumps(PAPER_DEFAULT_SETTINGS,separators=(",",":")),_now()),
                )
                await conn.commit()
                cur=await conn.execute("SELECT config_json FROM paper_settings WHERE telegram_id=?",(int(telegram_id),))
                row=await cur.fetchone()
            try: values=json.loads(row[0]) if row else {}
            except (TypeError,json.JSONDecodeError): values={}
        result=dict(PAPER_DEFAULT_SETTINGS); result.update(values)
        return result

    async def update_settings(self, telegram_id: int, changes: dict[str,Any], mode: str="paper") -> dict[str,Any]:
        if mode=="live":
            return await self.db.update_settings(telegram_id,changes)
        if mode!="paper":
            raise ValueError("mode must be paper or live")
        if set(changes)-set(PAPER_DEFAULT_SETTINGS):
            raise ValueError("Unknown paper setting.")
        result=await self.settings(telegram_id,"paper"); result.update(changes)
        if self.is_postgres:
            await self.db._pool().execute(
                """UPDATE public.n9n8_paper_settings SET config_json=$2::jsonb,updated_at=now()
                   WHERE telegram_id=$1""",
                int(telegram_id),json.dumps(result,separators=(",",":")),
            )
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                await conn.execute(
                    "UPDATE paper_settings SET config_json=?,updated_at=? WHERE telegram_id=?",
                    (json.dumps(result,separators=(",",":")),_now(),int(telegram_id)),
                )
                await conn.commit()
        return result

    async def create_account(self, telegram_id: int, starting_lamports: int) -> dict[str,int]:
        if starting_lamports<=0: raise ValueError("Paper starting balance must be positive.")
        await self.db.ensure_user(telegram_id)
        if self.is_postgres:
            await self.db._pool().execute(
                """INSERT INTO public.n9n8_paper_accounts
                   (telegram_id,starting_balance_lamports,cash_balance_lamports,realized_pnl_lamports)
                   VALUES($1,$2,$2,0) ON CONFLICT(telegram_id) DO NOTHING""",
                int(telegram_id),Decimal(str(starting_lamports)),
            )
        else:
            now=_now()
            async with aiosqlite.connect(self.db.path) as conn:
                await conn.execute(
                    """INSERT OR IGNORE INTO paper_accounts
                       (telegram_id,starting_balance_lamports,cash_balance_lamports,realized_pnl_lamports,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (int(telegram_id),starting_lamports,starting_lamports,0,now,now),
                )
                await conn.commit()
        account=await self.account(telegram_id)
        if account is None: raise RuntimeError("Paper account could not be created.")
        return account

    async def account(self, telegram_id: int) -> dict[str,int] | None:
        if self.is_postgres:
            row=await self.db._pool().fetchrow(
                """SELECT starting_balance_lamports,cash_balance_lamports,realized_pnl_lamports
                   FROM public.n9n8_paper_accounts WHERE telegram_id=$1""",int(telegram_id)
            )
            return {k:int(row[k]) for k in ("starting_balance_lamports","cash_balance_lamports","realized_pnl_lamports")} if row else None
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory=aiosqlite.Row
            cur=await conn.execute(
                "SELECT starting_balance_lamports,cash_balance_lamports,realized_pnl_lamports FROM paper_accounts WHERE telegram_id=?",
                (int(telegram_id),)
            )
            row=await cur.fetchone()
        return {k:int(row[k]) for k in ("starting_balance_lamports","cash_balance_lamports","realized_pnl_lamports")} if row else None

    async def touch_day(self, telegram_id: int, day: str) -> None:
        if self.is_postgres:
            await self.db._pool().execute(
                """INSERT INTO public.n9n8_paper_days(telegram_id,day_utc)
                   VALUES($1,$2::date) ON CONFLICT DO NOTHING""",int(telegram_id),day
            )
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO paper_days(telegram_id,day_utc,started_at) VALUES(?,?,?)",
                    (int(telegram_id),day,_now())
                )
                await conn.commit()

    async def active_days(self, telegram_id: int) -> int:
        if self.is_postgres:
            val=await self.db._pool().fetchval("SELECT count(*) FROM public.n9n8_paper_days WHERE telegram_id=$1",int(telegram_id))
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                cur=await conn.execute("SELECT count(*) FROM paper_days WHERE telegram_id=?",(int(telegram_id),))
                row=await cur.fetchone(); val=row[0] if row else 0
        return int(val or 0)

    async def has_open_mint(self, telegram_id: int, mint: str) -> bool:
        if self.is_postgres:
            val=await self.db._pool().fetchval(
                """SELECT EXISTS(SELECT 1 FROM public.n9n8_paper_positions
                   WHERE telegram_id=$1 AND mint=$2 AND status='open')""",int(telegram_id),mint
            )
            return bool(val)
        async with aiosqlite.connect(self.db.path) as conn:
            cur=await conn.execute(
                "SELECT 1 FROM paper_positions WHERE telegram_id=? AND mint=? AND status='open' LIMIT 1",
                (int(telegram_id),mint)
            )
            row=await cur.fetchone()
        return row is not None

    async def buy(self, telegram_id: int, mint: str, launchpad: str, tokens: int,
                  spend: int, fee: int, entry_price: str, quote: dict[str,Any],
                  reference: str|None=None) -> int:
        if tokens<=0 or spend<=0 or fee<0: raise ValueError("Paper buy amounts must be positive.")
        total=spend+fee; now=_now()
        if self.is_postgres:
            async with self.db._pool().acquire() as conn,conn.transaction():
                acct=await conn.fetchrow("SELECT cash_balance_lamports FROM public.n9n8_paper_accounts WHERE telegram_id=$1 FOR UPDATE",int(telegram_id))
                if not acct or int(acct["cash_balance_lamports"])<total:
                    raise ValueError("Not enough virtual SOL in your paper account.")
                await conn.execute("UPDATE public.n9n8_paper_accounts SET cash_balance_lamports=cash_balance_lamports-$2,updated_at=now() WHERE telegram_id=$1",int(telegram_id),Decimal(str(total)))
                pid=await conn.fetchval(
                    """INSERT INTO public.n9n8_paper_positions
                       (telegram_id,mint,launchpad,token_amount_raw,initial_cost_lamports,entry_price_usd,status,buy_reference)
                       VALUES($1,$2,$3,$4,$5,$6,'open',$7) RETURNING id""",
                    int(telegram_id),mint,launchpad,Decimal(str(tokens)),Decimal(str(total)),Decimal(entry_price),reference
                )
                await conn.execute(
                    """INSERT INTO public.n9n8_paper_trades
                       (telegram_id,position_id,side,mint,launchpad,token_amount_raw,sol_amount_lamports,fee_lamports,price_usd,reason,reference_signature,quote_json)
                       VALUES($1,$2,'buy',$3,$4,$5,$6,$7,$8,'paper_entry',$9,$10::jsonb)""",
                    int(telegram_id),int(pid),mint,launchpad,Decimal(str(tokens)),Decimal(str(spend)),Decimal(str(fee)),Decimal(entry_price),reference,json.dumps(quote,default=str)
                )
                return int(pid)
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory=aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            cur=await conn.execute("SELECT cash_balance_lamports FROM paper_accounts WHERE telegram_id=?",(int(telegram_id),))
            acct=await cur.fetchone()
            if not acct or int(acct[0])<total:
                await conn.rollback(); raise ValueError("Not enough virtual SOL in your paper account.")
            await conn.execute("UPDATE paper_accounts SET cash_balance_lamports=cash_balance_lamports-?,updated_at=? WHERE telegram_id=?",(total,now,int(telegram_id)))
            cur=await conn.execute(
                """INSERT INTO paper_positions(telegram_id,mint,launchpad,token_amount_raw,initial_cost_lamports,entry_price_usd,status,opened_at,buy_reference,updated_at)
                   VALUES(?,?,?,?,?,?,'open',?,?,?)""",
                (int(telegram_id),mint,launchpad,tokens,total,entry_price,now,reference,now)
            )
            pid=int(cur.lastrowid)
            await conn.execute(
                """INSERT INTO paper_trades(telegram_id,position_id,side,mint,launchpad,token_amount_raw,sol_amount_lamports,fee_lamports,price_usd,reason,reference_signature,quote_json,created_at)
                   VALUES(?,?,'buy',?,?,?,?,?,?, 'paper_entry',?,?,?)""",
                (int(telegram_id),pid,mint,launchpad,tokens,spend,fee,entry_price,reference,json.dumps(quote,separators=(",",":")),now)
            )
            await conn.commit()
            return pid

    async def positions(self, telegram_id: int|None=None) -> list[dict[str,Any]]:
        if self.is_postgres:
            if telegram_id is None:
                rows=await self.db._pool().fetch("SELECT * FROM public.n9n8_paper_positions WHERE status='open' ORDER BY id")
            else:
                rows=await self.db._pool().fetch("SELECT * FROM public.n9n8_paper_positions WHERE status='open' AND telegram_id=$1 ORDER BY id",int(telegram_id))
            return [_normal_position(dict(r)) for r in rows]
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory=aiosqlite.Row
            if telegram_id is None: cur=await conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY id")
            else: cur=await conn.execute("SELECT * FROM paper_positions WHERE status='open' AND telegram_id=? ORDER BY id",(int(telegram_id),))
            rows=await cur.fetchall()
        return [_normal_position(dict(r)) for r in rows]

    async def position(self, position_id: int, telegram_id: int|None=None) -> dict[str,Any]|None:
        if self.is_postgres:
            if telegram_id is None: row=await self.db._pool().fetchrow("SELECT * FROM public.n9n8_paper_positions WHERE id=$1",int(position_id))
            else: row=await self.db._pool().fetchrow("SELECT * FROM public.n9n8_paper_positions WHERE id=$1 AND telegram_id=$2",int(position_id),int(telegram_id))
            return _normal_position(dict(row)) if row else None
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory=aiosqlite.Row
            if telegram_id is None: cur=await conn.execute("SELECT * FROM paper_positions WHERE id=?",(int(position_id),))
            else: cur=await conn.execute("SELECT * FROM paper_positions WHERE id=? AND telegram_id=?",(int(position_id),int(telegram_id)))
            row=await cur.fetchone()
        return _normal_position(dict(row)) if row else None

    async def mark(self, position_id: int, value: int, pnl: str) -> None:
        if self.is_postgres:
            await self.db._pool().execute(
                """UPDATE public.n9n8_paper_positions SET peak_value_lamports=GREATEST(peak_value_lamports,$2),
                   last_value_lamports=$2,last_pnl_pct=$3,updated_at=now() WHERE id=$1 AND status='open'""",
                int(position_id),Decimal(str(value)),str(pnl)
            )
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                await conn.execute(
                    """UPDATE paper_positions SET peak_value_lamports=MAX(peak_value_lamports,?),
                       last_value_lamports=?,last_pnl_pct=?,updated_at=? WHERE id=? AND status='open'""",
                    (int(value),int(value),str(pnl),_now(),int(position_id))
                )
                await conn.commit()

    async def sell(self, position_id: int, telegram_id: int, tokens: int, gross: int, fee: int,
                   reason: str, quote: dict[str,Any], reference: str|None=None) -> dict[str,int|bool]:
        if tokens<=0 or gross<=0: raise ValueError("Paper sell needs a positive token amount and quote.")
        now=_now()
        if self.is_postgres:
            async with self.db._pool().acquire() as conn,conn.transaction():
                pos=await conn.fetchrow("SELECT * FROM public.n9n8_paper_positions WHERE id=$1 AND telegram_id=$2 AND status='open' FOR UPDATE",int(position_id),int(telegram_id))
                if not pos: raise ValueError("Paper position is not open.")
                old=int(pos["token_amount_raw"])
                if tokens>old: raise ValueError("Paper sell exceeds the open token balance.")
                basis=int(pos["initial_cost_lamports"]); basis_sold=min(basis,(basis*tokens)//max(old,1))
                net=max(0,gross-max(0,fee)); realized=net-basis_sold
                remaining=old-tokens; remaining_basis=0 if remaining==0 else basis-basis_sold
                await conn.execute("UPDATE public.n9n8_paper_accounts SET cash_balance_lamports=cash_balance_lamports+$2,realized_pnl_lamports=realized_pnl_lamports+$3,updated_at=now() WHERE telegram_id=$1",int(telegram_id),Decimal(str(net)),Decimal(str(realized)))
                await conn.execute(
                    """UPDATE public.n9n8_paper_positions SET token_amount_raw=$2,initial_cost_lamports=$3,
                       status=CASE WHEN $2=0 THEN 'closed' ELSE 'open' END,
                       closed_at=CASE WHEN $2=0 THEN now() ELSE NULL END,exit_reason=$4,
                       realized_lamports=realized_lamports+$5,updated_at=now() WHERE id=$1""",
                    int(position_id),Decimal(str(remaining)),Decimal(str(remaining_basis)),reason,Decimal(str(realized))
                )
                await conn.execute(
                    """INSERT INTO public.n9n8_paper_trades
                       (telegram_id,position_id,side,mint,launchpad,token_amount_raw,sol_amount_lamports,fee_lamports,price_usd,reason,reference_signature,quote_json)
                       VALUES($1,$2,'sell',$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)""",
                    int(telegram_id),int(position_id),str(pos["mint"]),str(pos["launchpad"]),Decimal(str(tokens)),Decimal(str(gross)),Decimal(str(fee)),Decimal(str(pos["entry_price_usd"])),reason,reference,json.dumps(quote,default=str)
                )
            return {"remaining_raw":remaining,"basis_sold_lamports":basis_sold,"realized_lamports":realized,"net_proceeds_lamports":net,"fully_closed":remaining==0}
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory=aiosqlite.Row
            await conn.execute("BEGIN IMMEDIATE")
            cur=await conn.execute("SELECT * FROM paper_positions WHERE id=? AND telegram_id=? AND status='open'",(int(position_id),int(telegram_id)))
            pos=await cur.fetchone()
            if not pos: await conn.rollback(); raise ValueError("Paper position is not open.")
            old=int(pos["token_amount_raw"])
            if tokens>old: await conn.rollback(); raise ValueError("Paper sell exceeds the open token balance.")
            basis=int(pos["initial_cost_lamports"]); basis_sold=min(basis,(basis*tokens)//max(old,1))
            net=max(0,gross-max(0,fee)); realized=net-basis_sold
            remaining=old-tokens; remaining_basis=0 if remaining==0 else basis-basis_sold
            await conn.execute("UPDATE paper_accounts SET cash_balance_lamports=cash_balance_lamports+?,realized_pnl_lamports=realized_pnl_lamports+?,updated_at=? WHERE telegram_id=?",(net,realized,now,int(telegram_id)))
            await conn.execute(
                """UPDATE paper_positions SET token_amount_raw=?,initial_cost_lamports=?,status=?,closed_at=?,exit_reason=?,realized_lamports=realized_lamports+?,updated_at=? WHERE id=?""",
                (remaining,remaining_basis,"closed" if remaining==0 else "open",now if remaining==0 else None,reason,realized,now,int(position_id))
            )
            await conn.execute(
                """INSERT INTO paper_trades(telegram_id,position_id,side,mint,launchpad,token_amount_raw,sol_amount_lamports,fee_lamports,price_usd,reason,reference_signature,quote_json,created_at)
                   VALUES(?,?,'sell',?,?,?,?,?,?,?,?,?,?)""",
                (int(telegram_id),int(position_id),pos["mint"],pos["launchpad"],tokens,gross,fee,pos["entry_price_usd"],reason,reference,json.dumps(quote,separators=(",",":")),now)
            )
            await conn.commit()
        return {"remaining_raw":remaining,"basis_sold_lamports":basis_sold,"realized_lamports":realized,"net_proceeds_lamports":net,"fully_closed":remaining==0}

    async def set_targets(self, position_id: int, tp1_done: bool | None = None, tp2_done: bool | None = None) -> None:
        if self.is_postgres:
            await self.db._pool().execute(
                """UPDATE public.n9n8_paper_positions SET
                   tp1_done=COALESCE($2,tp1_done),tp2_done=COALESCE($3,tp2_done),updated_at=now()
                   WHERE id=$1 AND status='open'""",
                int(position_id), tp1_done, tp2_done,
            )
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                await conn.execute(
                    """UPDATE paper_positions SET
                       tp1_done=COALESCE(?,tp1_done),tp2_done=COALESCE(?,tp2_done),updated_at=?
                       WHERE id=? AND status='open'""",
                    (None if tp1_done is None else int(tp1_done),
                     None if tp2_done is None else int(tp2_done),_now(),int(position_id)),
                )
                await conn.commit()

    async def trade_count_today(self, telegram_id: int, day_utc: str) -> int:
        if self.is_postgres:
            value = await self.db._pool().fetchval(
                """SELECT count(*) FROM public.n9n8_paper_trades
                   WHERE telegram_id=$1 AND side='buy' AND created_at::date=$2::date""",
                int(telegram_id),day_utc,
            )
        else:
            async with aiosqlite.connect(self.db.path) as conn:
                cur=await conn.execute(
                    """SELECT count(*) FROM paper_trades
                       WHERE telegram_id=? AND side='buy' AND substr(created_at,1,10)=?""",
                    (int(telegram_id),day_utc),
                )
                row=await cur.fetchone(); value=row[0] if row else 0
        return int(value or 0)

    async def history(self, telegram_id: int, limit: int=30) -> list[dict[str,Any]]:
        limit=max(1,min(int(limit),100))
        if self.is_postgres:
            rows=await self.db._pool().fetch("SELECT * FROM public.n9n8_paper_trades WHERE telegram_id=$1 ORDER BY id DESC LIMIT $2",int(telegram_id),limit)
            out=[]
            for row in rows:
                d=dict(row)
                for k in ("token_amount_raw","sol_amount_lamports","fee_lamports"): d[k]=str(d[k])
                try: d["quote_json"]=json.loads(d["quote_json"]) if isinstance(d["quote_json"],str) else dict(d["quote_json"] or {})
                except (TypeError,json.JSONDecodeError): d["quote_json"]={}
                out.append(d)
            return out
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory=aiosqlite.Row
            cur=await conn.execute("SELECT * FROM paper_trades WHERE telegram_id=? ORDER BY id DESC LIMIT ?",(int(telegram_id),limit))
            rows=await cur.fetchall()
        out=[dict(row) for row in rows]
        for d in out:
            try: d["quote_json"]=json.loads(d["quote_json"])
            except (TypeError,json.JSONDecodeError): d["quote_json"]={}
        return out
