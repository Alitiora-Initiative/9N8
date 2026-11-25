"""Persistent user preferences and verified Telegram report destinations."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator


class UserStore:
    def __init__(self, db_path: str, *, max_lots: float, max_positions: int,
                 default_sl: int, default_tp: int, default_be_trigger: int, default_be_offset: int):
        self.db_path = db_path
        self.defaults = {
            "max_lots": float(max_lots),
            "max_open_positions": int(max_positions),
            "default_sl_points": int(default_sl),
            "default_tp_points": int(default_tp),
            "be_trigger_points": int(default_be_trigger),
            "be_offset_points": int(default_be_offset),
        }
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
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    notifications INTEGER NOT NULL DEFAULT 1,
                    share_stats INTEGER NOT NULL DEFAULT 0,
                    paper_enabled INTEGER NOT NULL DEFAULT 0,
                    max_lots REAL NOT NULL,
                    max_open_positions INTEGER NOT NULL,
                    default_sl_points INTEGER NOT NULL,
                    default_tp_points INTEGER NOT NULL,
                    be_trigger_points INTEGER NOT NULL,
                    be_offset_points INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS report_chats (
                    chat_id TEXT PRIMARY KEY,
                    chat_title TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    added_by INTEGER NOT NULL,
                    added_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def register(self, telegram_id: int, chat_id: int | str, username: str | None, first_name: str | None) -> dict:
        now = self._now()
        with self._connect() as conn:
            row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (int(telegram_id),)).fetchone()
            if row is None:
                conn.execute("""
                    INSERT INTO users (telegram_id,chat_id,username,first_name,created_at,last_seen,
                        notifications,share_stats,paper_enabled,max_lots,max_open_positions,
                        default_sl_points,default_tp_points,be_trigger_points,be_offset_points)
                    VALUES (?,?,?,?,?,?,1,0,0,?,?,?,?,?,?)
                """, (int(telegram_id), str(chat_id), username, first_name, now, now,
                      self.defaults["max_lots"], self.defaults["max_open_positions"],
                      self.defaults["default_sl_points"], self.defaults["default_tp_points"],
                      self.defaults["be_trigger_points"], self.defaults["be_offset_points"]))
            else:
                conn.execute("UPDATE users SET chat_id=?,username=?,first_name=?,last_seen=? WHERE telegram_id=?",
                             (str(chat_id), username, first_name, now, int(telegram_id)))
        return self.get_user(telegram_id)

    def get_user(self, telegram_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (int(telegram_id),)).fetchone()
        return dict(row) if row else None

    def update(self, telegram_id: int, key: str, value) -> dict:
        allowed = {
            "notifications", "share_stats", "paper_enabled", "max_lots", "max_open_positions",
            "default_sl_points", "default_tp_points", "be_trigger_points", "be_offset_points",
        }
        if key not in allowed:
            raise ValueError("Unsupported user setting.")
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE users SET {key}=? WHERE telegram_id=?", (value, int(telegram_id)))
            if cur.rowcount != 1:
                raise ValueError("User is not registered. Send /start first.")
        return self.get_user(telegram_id)

    def opted_in_user_ids(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_id FROM users WHERE share_stats=1 ORDER BY telegram_id").fetchall()
        return [int(row[0]) for row in rows]

    def notification_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT telegram_id,chat_id FROM users WHERE notifications=1").fetchall()
        return [dict(row) for row in rows]

    def get_chat_id(self, telegram_id: int) -> str | None:
        user = self.get_user(telegram_id)
        return str(user["chat_id"]) if user else None

    def add_report_chat(self, chat_id: int | str, title: str, chat_type: str, added_by: int) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO report_chats(chat_id,chat_title,chat_type,added_by,added_at)
                VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                chat_title=excluded.chat_title,chat_type=excluded.chat_type,added_by=excluded.added_by,added_at=excluded.added_at
            """, (str(chat_id), title, chat_type, int(added_by), self._now()))

    def remove_report_chat(self, chat_id: int | str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM report_chats WHERE chat_id=?", (str(chat_id),))
            return cur.rowcount > 0

    def report_chats(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM report_chats ORDER BY chat_title").fetchall()
        return [dict(row) for row in rows]

    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))

    def registered_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
