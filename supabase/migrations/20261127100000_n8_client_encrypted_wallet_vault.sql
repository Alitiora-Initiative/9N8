-- Client-encrypted non-custodial wallet vault.
-- The server stores only AES-256-GCM ciphertext and public derivation addresses.
-- No plaintext mnemonic/private key/passphrase is accepted by this schema.

create table if not exists public.n8_wallet_vaults (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references public.n8_users(id) on delete cascade,
  format_version integer not null default 1 check (format_version = 1),
  cipher text not null default 'AES-256-GCM' check (cipher = 'AES-256-GCM'),
  kdf text not null default 'PBKDF2-SHA256' check (kdf = 'PBKDF2-SHA256'),
  kdf_iterations integer not null check (kdf_iterations between 600000 and 1000000),
  salt_b64 text not null check (length(salt_b64) between 20 and 64),
  iv_b64 text not null check (length(iv_b64) between 16 and 32),
  ciphertext_b64 text not null check (length(ciphertext_b64) between 40 and 65536),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.n8_wallet_accounts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.n8_users(id) on delete cascade,
  wallet_index integer not null check (wallet_index between 0 and 1000),
  chain_namespace text not null check (chain_namespace in ('solana','eip155')),
  chain_id text not null,
  network text not null,
  public_address text not null,
  derivation_path text not null,
  label text not null default 'Wallet',
  is_selected boolean not null default false,
  created_at timestamptz not null default now(),
  unique (user_id, chain_namespace, chain_id, public_address),
  unique (user_id, wallet_index, chain_namespace, chain_id)
);

create index if not exists n8_wallet_accounts_owner_idx
  on public.n8_wallet_accounts(user_id, chain_namespace, wallet_index);

alter table public.n8_wallet_vaults enable row level security;
alter table public.n8_wallet_accounts enable row level security;

-- These tables are service-role-only. Do not grant Data API access to anon/authenticated.
revoke all on table public.n8_wallet_vaults from public, anon, authenticated;
revoke all on table public.n8_wallet_accounts from public, anon, authenticated;
grant select, insert, update, delete on table public.n8_wallet_vaults to service_role;
grant select, insert, update, delete on table public.n8_wallet_accounts to service_role;
