-- 9N8 wallet sessions, policy controls, and universal market/trading records.
-- This migration adds storage and security boundaries only. It does not enable live trading.

alter table public.n8_wallet_connections
  drop constraint if exists n8_wallet_connections_provider_check;
alter table public.n8_wallet_connections
  add constraint n8_wallet_connections_provider_check
  check (provider in ('trust_wallet','phantom','walletconnect','metamask','manual'));

alter table public.n8_wallet_connections
  add column if not exists connection_type text not null default 'watch_only'
    check (connection_type in ('watch_only','walletconnect_session','injected_session')),
  add column if not exists execution_mode text not null default 'approval'
    check (execution_mode in ('approval','auto')),
  add column if not exists auto_trade_enabled boolean not null default false,
  add column if not exists max_order_usd numeric(20,8) not null default 5
    check (max_order_usd >= 0),
  add column if not exists max_daily_notional_usd numeric(20,8) not null default 10
    check (max_daily_notional_usd >= 0),
  add column if not exists last_connected_at timestamptz,
  add column if not exists disconnected_at timestamptz;

alter table public.n8_risk_policies
  add column if not exists execution_mode text not null default 'approval'
    check (execution_mode in ('approval','auto')),
  add column if not exists auto_execute_enabled boolean not null default false,
  add column if not exists wallet_auto_execute_enabled boolean not null default false,
  add column if not exists max_order_usd numeric(20,8) not null default 10
    check (max_order_usd >= 0),
  add column if not exists max_daily_notional_usd numeric(20,8) not null default 25
    check (max_daily_notional_usd >= 0),
  add column if not exists max_open_positions integer not null default 3
    check (max_open_positions between 0 and 100),
  add column if not exists max_slippage_bps integer not null default 100
    check (max_slippage_bps between 0 and 10000),
  add column if not exists max_price_deviation_bps integer not null default 50
    check (max_price_deviation_bps between 0 and 10000),
  add column if not exists minimum_liquidity_usd numeric(20,8) not null default 50000
    check (minimum_liquidity_usd >= 0);

create table if not exists public.n8_wallet_challenges (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  provider text not null check (provider in ('walletconnect','phantom','trust_wallet','metamask')),
  chain_namespace text not null check (chain_namespace in ('eip155','solana')),
  chain_id text not null,
  network text not null,
  public_address text not null,
  nonce text not null unique,
  message text not null,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists n8_wallet_challenges_owner_expiry_idx
  on public.n8_wallet_challenges(user_id, expires_at desc);

create table if not exists public.n8_market_instruments (
  id uuid primary key default gen_random_uuid(),
  market_type text not null check (market_type in ('solana','crypto','stocks','forex')),
  symbol text not null,
  canonical_id text not null unique,
  name text,
  base_asset text,
  quote_asset text,
  venue text,
  network text,
  contract_address text,
  metadata jsonb not null default '{}'::jsonb,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.n8_market_snapshots (
  id bigserial primary key,
  instrument_id uuid not null references public.n8_market_instruments(id) on delete cascade,
  source text not null,
  observed_at timestamptz not null,
  price numeric(30,12),
  bid numeric(30,12),
  ask numeric(30,12),
  volume_24h numeric(30,12),
  liquidity_usd numeric(30,8),
  change_24h_pct numeric(18,8),
  metadata jsonb not null default '{}'::jsonb,
  unique(instrument_id, source, observed_at)
);
create index if not exists n8_market_snapshots_instrument_time_idx
  on public.n8_market_snapshots(instrument_id, observed_at desc);

create table if not exists public.n8_market_candles (
  id bigserial primary key,
  instrument_id uuid not null references public.n8_market_instruments(id) on delete cascade,
  source text not null,
  timeframe text not null,
  open_time timestamptz not null,
  close_time timestamptz not null,
  open numeric(30,12) not null,
  high numeric(30,12) not null,
  low numeric(30,12) not null,
  close numeric(30,12) not null,
  volume numeric(30,12) not null default 0,
  quote_volume numeric(30,12),
  trades_count bigint,
  metadata jsonb not null default '{}'::jsonb,
  unique(instrument_id, source, timeframe, open_time),
  check (high >= low and high >= greatest(open, close) and low <= least(open, close))
);
create index if not exists n8_market_candles_lookup_idx
  on public.n8_market_candles(instrument_id, timeframe, open_time desc);

create table if not exists public.n8_news_items (
  id uuid primary key default gen_random_uuid(),
  source_name text not null,
  source_domain text,
  canonical_url text not null unique,
  headline text not null,
  summary text,
  published_at timestamptz,
  collected_at timestamptz not null default now(),
  content_hash text,
  asset_symbols text[] not null default '{}',
  catalyst_tags text[] not null default '{}',
  impact_score integer not null default 0 check (impact_score between -100 and 100),
  directional_bias text not null default 'neutral' check (directional_bias in ('bullish','bearish','neutral','volatility')),
  is_noise boolean not null default false,
  duplicate_of uuid references public.n8_news_items(id) on delete set null,
  metadata jsonb not null default '{}'::jsonb
);
create index if not exists n8_news_items_published_idx on public.n8_news_items(published_at desc);
create index if not exists n8_news_items_score_idx on public.n8_news_items(impact_score desc, published_at desc);

create table if not exists public.n8_research_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  market_type text not null check (market_type in ('solana','crypto','stocks','forex','macro')),
  subject text not null,
  conclusion text not null,
  directional_bias text not null default 'neutral' check (directional_bias in ('bullish','bearish','neutral','volatility')),
  confidence numeric(6,5) not null default 0 check (confidence between 0 and 1),
  evidence jsonb not null default '[]'::jsonb,
  risks jsonb not null default '[]'::jsonb,
  model_version text,
  created_at timestamptz not null default now(),
  expires_at timestamptz
);
create index if not exists n8_research_reports_owner_time_idx on public.n8_research_reports(user_id, created_at desc);

create table if not exists public.n8_strategy_versions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.n8_users(id) on delete cascade,
  name text not null,
  version integer not null check (version > 0),
  market_type text not null check (market_type in ('solana','crypto','stocks','forex','multi')),
  config jsonb not null,
  checksum text not null,
  enabled boolean not null default false,
  created_at timestamptz not null default now(),
  unique(user_id, name, version)
);

create table if not exists public.n8_strategy_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  strategy_version_id uuid references public.n8_strategy_versions(id) on delete set null,
  instrument_id uuid references public.n8_market_instruments(id) on delete set null,
  signal text not null check (signal in ('long','short','watch','reject')),
  score numeric(10,5) not null default 0,
  rationale text not null,
  evidence jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  expires_at timestamptz
);
create index if not exists n8_strategy_signals_owner_time_idx on public.n8_strategy_signals(user_id, created_at desc);

create table if not exists public.n8_trade_proposals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  market_type text not null check (market_type in ('solana','crypto','stocks','forex')),
  instrument_id uuid references public.n8_market_instruments(id) on delete set null,
  symbol text not null,
  venue text not null,
  execution_method text not null check (execution_method in ('paper','exchange_api','wallet')),
  environment text not null check (environment in ('paper','demo','testnet','live')),
  side text not null check (side in ('buy','sell','long','short')),
  order_type text not null default 'market' check (order_type in ('market','limit','stop','stop_limit')),
  quantity numeric(30,12),
  quote_amount_usd numeric(20,8),
  limit_price numeric(30,12),
  stop_price numeric(30,12),
  target_price numeric(30,12),
  max_slippage_bps integer not null default 100 check (max_slippage_bps between 0 and 10000),
  strategy_version_id uuid references public.n8_strategy_versions(id) on delete set null,
  research_report_id uuid references public.n8_research_reports(id) on delete set null,
  payload jsonb not null,
  risk_snapshot jsonb not null,
  payload_hash text not null,
  idempotency_key text not null,
  decision_mode text not null default 'approval' check (decision_mode in ('approval','auto','reject')),
  status text not null default 'pending' check (status in ('pending','approved','rejected','expired','executing','submitted','partially_filled','filled','cancelled','failed')),
  expires_at timestamptz not null,
  approved_at timestamptz,
  rejected_at timestamptz,
  executed_at timestamptz,
  created_at timestamptz not null default now(),
  unique(user_id, idempotency_key)
);
create index if not exists n8_trade_proposals_owner_status_idx on public.n8_trade_proposals(user_id, status, created_at desc);

create table if not exists public.n8_approvals (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid not null references public.n8_trade_proposals(id) on delete cascade,
  user_id uuid not null references public.n8_users(id) on delete cascade,
  decision text not null check (decision in ('approved','rejected','expired')),
  actor_telegram_user_id bigint not null,
  payload_hash text not null,
  requested_at timestamptz not null default now(),
  decided_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb
);
create index if not exists n8_approvals_owner_time_idx on public.n8_approvals(user_id, decided_at desc);

create table if not exists public.n8_orders (
  id uuid primary key default gen_random_uuid(),
  proposal_id uuid not null unique references public.n8_trade_proposals(id) on delete restrict,
  user_id uuid not null references public.n8_users(id) on delete cascade,
  provider text not null,
  environment text not null check (environment in ('paper','demo','testnet','live')),
  external_order_id text,
  status text not null check (status in ('pending','submitted','open','partially_filled','filled','cancelled','rejected','failed','unknown')),
  request_payload jsonb not null,
  response_payload jsonb not null default '{}'::jsonb,
  submitted_at timestamptz,
  reconciled_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists n8_orders_owner_status_idx on public.n8_orders(user_id, status, created_at desc);

create table if not exists public.n8_fills (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references public.n8_orders(id) on delete cascade,
  user_id uuid not null references public.n8_users(id) on delete cascade,
  fill_id text,
  quantity numeric(30,12) not null check (quantity > 0),
  price numeric(30,12) not null check (price >= 0),
  fee numeric(30,12) not null default 0 check (fee >= 0),
  fee_asset text,
  filled_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb,
  unique(order_id, fill_id)
);
create index if not exists n8_fills_owner_time_idx on public.n8_fills(user_id, filled_at desc);

create table if not exists public.n8_positions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  market_type text not null check (market_type in ('solana','crypto','stocks','forex')),
  instrument_id uuid references public.n8_market_instruments(id) on delete set null,
  symbol text not null,
  venue text not null,
  environment text not null check (environment in ('paper','demo','testnet','live')),
  quantity numeric(30,12) not null default 0,
  average_entry_price numeric(30,12) not null default 0,
  mark_price numeric(30,12),
  realized_pnl_usd numeric(20,8) not null default 0,
  unrealized_pnl_usd numeric(20,8) not null default 0,
  status text not null default 'open' check (status in ('open','closed')),
  opened_at timestamptz not null default now(),
  closed_at timestamptz,
  metadata jsonb not null default '{}'::jsonb
);
create index if not exists n8_positions_owner_status_idx on public.n8_positions(user_id, status, opened_at desc);

create table if not exists public.n8_trade_journal (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  proposal_id uuid references public.n8_trade_proposals(id) on delete set null,
  order_id uuid references public.n8_orders(id) on delete set null,
  strategy_version_id uuid references public.n8_strategy_versions(id) on delete set null,
  outcome text not null check (outcome in ('win','loss','breakeven','cancelled','rejected','unresolved')),
  net_pnl_usd numeric(20,8),
  lesson text,
  metrics jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists n8_trade_journal_owner_time_idx on public.n8_trade_journal(user_id, created_at desc);

create table if not exists public.n8_strategy_metrics (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  strategy_version_id uuid references public.n8_strategy_versions(id) on delete cascade,
  period_start timestamptz not null,
  period_end timestamptz not null,
  trades_count integer not null default 0,
  wins integer not null default 0,
  losses integer not null default 0,
  win_rate numeric(10,8),
  expectancy_usd numeric(20,8),
  profit_factor numeric(20,8),
  max_drawdown_pct numeric(10,8),
  avg_slippage_bps numeric(12,5),
  metrics jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (period_end > period_start)
);
create index if not exists n8_strategy_metrics_owner_time_idx on public.n8_strategy_metrics(user_id, period_end desc);

create table if not exists public.n8_brain_memories (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  memory_type text not null check (memory_type in ('research','strategy','trade_outcome','risk_preference','market_regime')),
  subject text not null,
  content text not null,
  tags text[] not null default '{}',
  importance numeric(6,5) not null default 0.5 check (importance between 0 and 1),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  expires_at timestamptz
);
create index if not exists n8_brain_memories_owner_time_idx on public.n8_brain_memories(user_id, created_at desc);

create table if not exists public.n8_report_artifacts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  report_type text not null,
  storage_bucket text not null,
  storage_path text not null,
  mime_type text not null,
  width integer,
  height integer,
  source_data_hash text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique(storage_bucket, storage_path)
);

-- Server-side only. All user-facing reads/writes go through authenticated server code.
do $$
declare
  t text;
begin
  foreach t in array array[
    'n8_wallet_challenges','n8_market_instruments','n8_market_snapshots','n8_market_candles',
    'n8_news_items','n8_research_reports','n8_strategy_versions','n8_strategy_signals',
    'n8_trade_proposals','n8_approvals','n8_orders','n8_fills','n8_positions',
    'n8_trade_journal','n8_strategy_metrics','n8_brain_memories','n8_report_artifacts'
  ]
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from public, anon, authenticated', t);
    execute format('grant all on table public.%I to service_role', t);
  end loop;
end $$;

grant usage, select on all sequences in schema public to service_role;
