begin;

alter table public.n8_exchange_connections
  drop constraint if exists n8_exchange_connections_provider_check;

alter table public.n8_exchange_connections
  add constraint n8_exchange_connections_provider_check
  check (provider in ('binance', 'okx'));

create or replace function public.n8_store_exchange_secret(
  p_telegram_user_id bigint,
  p_provider text,
  p_environment text,
  p_label text,
  p_credentials jsonb,
  p_permissions jsonb default '{}'::jsonb,
  p_account_hint text default null
)
returns uuid
language plpgsql
security definer
set search_path to ''
as $function$
declare
  v_user_id uuid;
  v_connection_id uuid := gen_random_uuid();
  v_secret_id uuid;
  v_role text;
  v_name text := 'n8-exchange-' || v_connection_id::text;
  v_note text;
begin
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  if v_role <> 'service_role' then raise exception 'not_allowed'; end if;
  if p_provider not in ('binance', 'okx') then raise exception 'unsupported_provider'; end if;
  if p_environment <> 'live' then raise exception 'unsupported_environment'; end if;
  if p_credentials is null or p_credentials = '{}'::jsonb then raise exception 'empty_credentials'; end if;
  if nullif(p_credentials ->> 'apiKey', '') is null or
     nullif(p_credentials ->> 'apiSecret', '') is null then
    raise exception 'api_key_and_secret_required';
  end if;
  if p_provider = 'okx' and nullif(p_credentials ->> 'passphrase', '') is null then
    raise exception 'okx_passphrase_required';
  end if;

  select u.id into v_user_id
  from public.n8_users u
  where u.telegram_user_id = p_telegram_user_id and u.status = 'active'
  for update;
  if v_user_id is null then raise exception 'user_not_registered'; end if;

  v_note := case
    when p_provider = 'binance'
      then '9N8 per-user Binance Spot LIVE credentials stored in Vault; no withdrawals or transfers are used'
    else
      '9N8 per-user OKX Spot LIVE API credentials and passphrase stored in Vault; this release validates read access only'
  end;
  v_secret_id := vault.create_secret(p_credentials::text, v_name, v_note);

  insert into public.n8_exchange_connections(
    id, user_id, provider, environment, label, account_hint,
    vault_secret_id, permissions, status, last_validated_at
  ) values (
    v_connection_id, v_user_id, p_provider, 'live', p_label, p_account_hint,
    v_secret_id, coalesce(p_permissions, '{}'::jsonb), 'active', now()
  );

  return v_connection_id;
end;
$function$;

revoke all on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text)
  from public, anon, authenticated;
grant execute on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text)
  to service_role;

commit;
