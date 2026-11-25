create schema if not exists n8_private;
revoke all on schema n8_private from public;
revoke all on schema n8_private from anon, authenticated;
grant usage on schema n8_private to service_role;

create table if not exists public.n8_users (
  id uuid primary key default gen_random_uuid(),
  telegram_user_id bigint not null unique,
  telegram_username text,
  first_name text,
  status text not null default 'active' check (status in ('active','suspended','deleted')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.n8_exchange_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  provider text not null check (provider in ('okx','binance','bybit')),
  environment text not null check (environment in ('demo','testnet')),
  label text,
  account_hint text,
  vault_secret_id uuid,
  permissions jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending','active','error','revoked')),
  last_validated_at timestamptz,
  last_error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists n8_exchange_connections_user_idx
  on public.n8_exchange_connections(user_id, provider, status);

create table if not exists public.n8_wallet_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  provider text not null check (provider in ('trust_wallet','phantom','walletconnect','manual')),
  chain_namespace text not null,
  chain_id text,
  network text not null,
  public_address text not null,
  label text,
  verified_at timestamptz,
  status text not null default 'watch_only' check (status in ('watch_only','verified','revoked')),
  created_at timestamptz not null default now(),
  unique(user_id, chain_namespace, network, public_address)
);
create index if not exists n8_wallet_connections_user_idx
  on public.n8_wallet_connections(user_id, status);

create table if not exists public.n8_group_settings (
  chat_id bigint primary key,
  title text,
  created_by_user_id uuid references public.n8_users(id) on delete set null,
  mode text not null default 'market_only' check (mode in ('market_only','community_helper','moderator')),
  bot_status text not null default 'member',
  bot_rights jsonb not null default '{}'::jsonb,
  settings jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.n8_risk_policies (
  user_id uuid primary key references public.n8_users(id) on delete cascade,
  environment text not null default 'demo' check (environment in ('demo','testnet')),
  max_order_notional numeric(28,10) not null default 10 check (max_order_notional > 0),
  max_open_orders integer not null default 3 check (max_open_orders between 0 and 100),
  daily_loss_limit numeric(28,10) not null default 10 check (daily_loss_limit > 0),
  max_daily_orders integer not null default 10 check (max_daily_orders between 0 and 500),
  live_trading_enabled boolean not null default false,
  require_confirmation boolean not null default true,
  kill_switch boolean not null default false,
  updated_at timestamptz not null default now()
);

create table if not exists public.n8_strategy_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  connection_id uuid references public.n8_exchange_connections(id) on delete set null,
  name text not null,
  provider text not null check (provider in ('okx','binance','bybit')),
  environment text not null check (environment in ('demo','testnet')),
  config jsonb not null default '{}'::jsonb,
  state text not null default 'stopped' check (state in ('stopped','running','paused','error')),
  last_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists n8_strategy_runs_due_idx
  on public.n8_strategy_runs(state, last_run_at) where state = 'running';

create table if not exists public.n8_trade_intents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  connection_id uuid not null references public.n8_exchange_connections(id) on delete restrict,
  strategy_run_id uuid references public.n8_strategy_runs(id) on delete set null,
  provider text not null check (provider in ('okx','binance','bybit')),
  environment text not null check (environment in ('demo','testnet')),
  order_payload jsonb not null,
  risk_snapshot jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending','approved','rejected','expired','submitted','failed')),
  idempotency_key text not null unique,
  expires_at timestamptz not null,
  approved_at timestamptz,
  submitted_at timestamptz,
  provider_order_id text,
  created_at timestamptz not null default now()
);
create index if not exists n8_trade_intents_user_idx
  on public.n8_trade_intents(user_id, status, created_at desc);

create table if not exists public.n8_telegram_updates (
  update_id bigint primary key,
  received_at timestamptz not null default now(),
  processed_at timestamptz,
  outcome text
);

create table if not exists public.n8_audit_events (
  id bigint generated always as identity primary key,
  user_id uuid references public.n8_users(id) on delete set null,
  chat_id bigint,
  event_type text not null,
  resource_type text,
  resource_id text,
  outcome text not null default 'ok',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists n8_audit_events_user_idx
  on public.n8_audit_events(user_id, created_at desc);

do $$
declare t text;
begin
  foreach t in array array[
    'n8_users','n8_exchange_connections','n8_wallet_connections',
    'n8_group_settings','n8_risk_policies','n8_strategy_runs',
    'n8_trade_intents','n8_telegram_updates','n8_audit_events'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from public, anon, authenticated', t);
    execute format('grant all on table public.%I to service_role', t);
  end loop;
end $$;

create or replace function public.n8_store_exchange_secret(
  p_telegram_user_id bigint,
  p_provider text,
  p_environment text,
  p_label text,
  p_credentials jsonb,
  p_permissions jsonb default '{}'::jsonb,
  p_account_hint text default null
) returns uuid
language plpgsql security definer set search_path = ''
as $$
declare
  v_user_id uuid;
  v_connection_id uuid := gen_random_uuid();
  v_secret_id uuid;
  v_name text := 'n8-exchange-' || v_connection_id::text;
begin
  if coalesce(current_setting('request.jwt.claim.role', true), '') <> 'service_role' then
    raise exception 'not_allowed';
  end if;
  if p_provider not in ('okx','binance','bybit') then raise exception 'unsupported_provider'; end if;
  if p_environment not in ('demo','testnet') then raise exception 'unsupported_environment'; end if;
  if p_credentials is null or p_credentials = '{}'::jsonb then raise exception 'empty_credentials'; end if;
  select u.id into v_user_id from public.n8_users u
  where u.telegram_user_id = p_telegram_user_id and u.status = 'active' for update;
  if v_user_id is null then raise exception 'user_not_registered'; end if;
  v_secret_id := vault.create_secret(p_credentials::text, v_name,
    '9N8 per-user exchange credentials; provider=' || p_provider || '; environment=' || p_environment);
  insert into public.n8_exchange_connections(
    id,user_id,provider,environment,label,account_hint,vault_secret_id,permissions,status,last_validated_at
  ) values (
    v_connection_id,v_user_id,p_provider,p_environment,p_label,p_account_hint,v_secret_id,
    coalesce(p_permissions,'{}'::jsonb),'active',now()
  );
  return v_connection_id;
end;
$$;

create or replace function public.n8_get_exchange_credentials(p_telegram_user_id bigint,p_connection_id uuid)
returns jsonb language plpgsql security definer set search_path = ''
as $$
declare v_secret jsonb;
begin
  if coalesce(current_setting('request.jwt.claim.role', true), '') <> 'service_role' then raise exception 'not_allowed'; end if;
  select ds.decrypted_secret::jsonb into v_secret
  from public.n8_exchange_connections c
  join public.n8_users u on u.id = c.user_id
  join vault.decrypted_secrets ds on ds.id = c.vault_secret_id
  where u.telegram_user_id = p_telegram_user_id and c.id = p_connection_id
    and c.status = 'active' and u.status = 'active';
  if v_secret is null then raise exception 'connection_not_found_or_revoked'; end if;
  return v_secret;
end;
$$;

create or replace function public.n8_revoke_exchange_connection(p_telegram_user_id bigint,p_connection_id uuid)
returns boolean language plpgsql security definer set search_path = ''
as $$
declare v_secret_id uuid;
begin
  if coalesce(current_setting('request.jwt.claim.role', true), '') <> 'service_role' then raise exception 'not_allowed'; end if;
  select c.vault_secret_id into v_secret_id
  from public.n8_exchange_connections c
  join public.n8_users u on u.id = c.user_id
  where u.telegram_user_id = p_telegram_user_id and c.id = p_connection_id and c.status <> 'revoked'
  for update of c;
  if v_secret_id is null then return false; end if;
  delete from vault.secrets where id = v_secret_id;
  update public.n8_exchange_connections set status = 'revoked',vault_secret_id = null,updated_at = now()
  where id = p_connection_id;
  return true;
end;
$$;

revoke all on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text) from public, anon, authenticated;
revoke all on function public.n8_get_exchange_credentials(bigint,uuid) from public, anon, authenticated;
revoke all on function public.n8_revoke_exchange_connection(bigint,uuid) from public, anon, authenticated;
grant execute on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text) to service_role;
grant execute on function public.n8_get_exchange_credentials(bigint,uuid) to service_role;
grant execute on function public.n8_revoke_exchange_connection(bigint,uuid) to service_role;
