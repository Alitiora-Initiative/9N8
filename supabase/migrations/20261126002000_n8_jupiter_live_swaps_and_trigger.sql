begin;

create table if not exists public.n8_jupiter_swaps (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  wallet_connection_id uuid not null references public.n8_wallet_connections(id) on delete restrict,
  request_id text not null unique,
  input_mint text not null,
  output_mint text not null,
  input_amount_raw text not null,
  output_amount_raw text not null,
  minimum_output_raw text not null,
  input_decimals smallint not null check (input_decimals between 0 and 18),
  output_decimals smallint not null check (output_decimals between 0 and 18),
  router text not null,
  price_impact_pct numeric,
  sol_lamports_equivalent numeric(30,0) not null check (sol_lamports_equivalent > 0),
  transaction_message_sha256 text not null check (transaction_message_sha256 ~ '^[0-9a-f]{64}$'),
  status text not null default 'quote_ready'
    check (status in ('quote_ready','executing','success','failed','unknown','expired')),
  expires_at timestamptz not null,
  owner_signed_at timestamptz,
  signature text,
  result_payload jsonb not null default '{}'::jsonb,
  quote_metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists n8_jupiter_swaps_user_created_idx
  on public.n8_jupiter_swaps(user_id, created_at desc);
create index if not exists n8_jupiter_swaps_user_status_idx
  on public.n8_jupiter_swaps(user_id, status, expires_at);

alter table public.n8_jupiter_swaps enable row level security;
drop policy if exists deny_anon_authenticated on public.n8_jupiter_swaps;
create policy deny_anon_authenticated on public.n8_jupiter_swaps
  for all to anon, authenticated using (false) with check (false);
revoke all on public.n8_jupiter_swaps from anon, authenticated;
grant all on public.n8_jupiter_swaps to service_role;

create table if not exists public.n8_jupiter_automation_orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  wallet_connection_id uuid not null references public.n8_wallet_connections(id) on delete restrict,
  jupiter_order_id text not null unique,
  order_family text not null check (order_family in ('dca','price')),
  input_mint text not null,
  output_mint text not null,
  input_amount_raw text not null,
  order_count integer,
  interval_seconds integer,
  vault_address text not null,
  deposit_signature text,
  status text not null default 'active',
  order_metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists n8_jupiter_automation_orders_user_created_idx
  on public.n8_jupiter_automation_orders(user_id, created_at desc);

alter table public.n8_jupiter_automation_orders enable row level security;
drop policy if exists deny_anon_authenticated on public.n8_jupiter_automation_orders;
create policy deny_anon_authenticated on public.n8_jupiter_automation_orders
  for all to anon, authenticated using (false) with check (false);
revoke all on public.n8_jupiter_automation_orders from anon, authenticated;
grant all on public.n8_jupiter_automation_orders to service_role;

create table if not exists public.n8_jupiter_connector_certification (
  id uuid primary key default gen_random_uuid(),
  provider text not null unique check (provider = 'jupiter_swap_v2'),
  environment text not null default 'live' check (environment = 'live'),
  status text not null default 'testing' check (status in ('disabled','testing','passed','failed')),
  live_manual_wallet_execution_enabled boolean not null default false,
  delegated_automation_enabled boolean not null default false,
  owner_signature_test_passed boolean not null default false,
  message_integrity_test_passed boolean not null default false,
  idempotency_test_passed boolean not null default false,
  unknown_execution_reconciliation_passed boolean not null default false,
  cancellation_withdrawal_test_passed boolean not null default false,
  checked_at timestamptz,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.n8_jupiter_connector_certification enable row level security;
drop policy if exists deny_anon_authenticated on public.n8_jupiter_connector_certification;
create policy deny_anon_authenticated on public.n8_jupiter_connector_certification
  for all to anon, authenticated using (false) with check (false);
revoke all on public.n8_jupiter_connector_certification from anon, authenticated;
grant all on public.n8_jupiter_connector_certification to service_role;

insert into public.n8_jupiter_connector_certification(provider, status, live_manual_wallet_execution_enabled, delegated_automation_enabled, evidence)
values (
  'jupiter_swap_v2',
  'testing',
  false,
  false,
  jsonb_build_object(
    'note','Certification is intentionally not passed. Real swaps require a user-signed wallet transaction and release tests.',
    'pendingChecks', jsonb_build_array('owner signature validation','message hash integrity','idempotent execute claim','unknown execution reconciliation','DCA cancel and vault withdrawal review')
  )
)
on conflict (provider) do nothing;

commit;