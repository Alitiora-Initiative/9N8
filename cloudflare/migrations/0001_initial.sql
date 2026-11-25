CREATE TABLE IF NOT EXISTS processed_updates (
  update_id INTEGER PRIMARY KEY,
  processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  telegram_id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  username TEXT,
  first_name TEXT,
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  notifications INTEGER NOT NULL DEFAULT 1,
  share_stats INTEGER NOT NULL DEFAULT 0,
  paper_enabled INTEGER NOT NULL DEFAULT 0,
  max_lots REAL NOT NULL DEFAULT 0.10,
  max_open_positions INTEGER NOT NULL DEFAULT 3,
  default_sl_points INTEGER NOT NULL DEFAULT 500,
  default_tp_points INTEGER NOT NULL DEFAULT 1000,
  be_trigger_points INTEGER NOT NULL DEFAULT 150,
  be_offset_points INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_positions (
  ticket INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  lots REAL NOT NULL,
  entry_price REAL NOT NULL,
  stop_loss REAL NOT NULL,
  take_profit REAL NOT NULL,
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
  close_price REAL,
  closed_at TEXT,
  pnl_quote REAL,
  profit_currency TEXT NOT NULL,
  contract_size REAL NOT NULL,
  point REAL NOT NULL,
  owner_id TEXT NOT NULL,
  be_trigger_points INTEGER NOT NULL DEFAULT 0,
  be_offset_points INTEGER NOT NULL DEFAULT 0,
  be_activated INTEGER NOT NULL DEFAULT 0,
  close_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_paper_owner_status
  ON paper_positions(owner_id, status);

CREATE INDEX IF NOT EXISTS idx_paper_closed_at
  ON paper_positions(status, closed_at);

CREATE TABLE IF NOT EXISTS report_chats (
  chat_id TEXT PRIMARY KEY,
  chat_title TEXT NOT NULL,
  chat_type TEXT NOT NULL,
  added_by TEXT NOT NULL,
  added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
  symbol TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  close REAL NOT NULL,
  ema_fast REAL NOT NULL,
  ema_slow REAL NOT NULL,
  rsi REAL NOT NULL,
  candle_time TEXT NOT NULL,
  explanation TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
