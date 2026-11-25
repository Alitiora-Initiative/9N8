create policy n8_backend_only on public.n8_users
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_exchange_connections
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_wallet_connections
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_group_settings
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_risk_policies
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_strategy_runs
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_trade_intents
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_telegram_updates
  for all to anon, authenticated using (false) with check (false);
create policy n8_backend_only on public.n8_audit_events
  for all to anon, authenticated using (false) with check (false);

create index if not exists n8_group_settings_created_by_idx on public.n8_group_settings(created_by_user_id);
create index if not exists n8_strategy_runs_connection_idx on public.n8_strategy_runs(connection_id);
create index if not exists n8_strategy_runs_user_idx on public.n8_strategy_runs(user_id);
create index if not exists n8_trade_intents_connection_idx on public.n8_trade_intents(connection_id);
create index if not exists n8_trade_intents_strategy_idx on public.n8_trade_intents(strategy_run_id);

create or replace function public.n8_store_exchange_secret(
  p_telegram_user_id bigint,p_provider text,p_environment text,p_label text,
  p_credentials jsonb,p_permissions jsonb default '{}'::jsonb,p_account_hint text default null
) returns uuid language plpgsql security definer set search_path = ''
as $$
declare v_user_id uuid; v_connection_id uuid := gen_random_uuid(); v_secret_id uuid;
  v_role text; v_name text := 'n8-exchange-' || v_connection_id::text;
begin
  v_role := coalesce(current_setting('request.jwt.claim.role',true),
    (nullif(current_setting('request.jwt.claims',true),'')::jsonb ->> 'role'),'');
  if v_role <> 'service_role' then raise exception 'not_allowed'; end if;
  if p_provider not in ('okx','binance','bybit') then raise exception 'unsupported_provider'; end if;
  if p_environment not in ('demo','testnet') then raise exception 'unsupported_environment'; end if;
  if p_credentials is null or p_credentials = '{}'::jsonb then raise exception 'empty_credentials'; end if;
  select u.id into v_user_id from public.n8_users u
  where u.telegram_user_id=p_telegram_user_id and u.status='active' for update;
  if v_user_id is null then raise exception 'user_not_registered'; end if;
  v_secret_id := vault.create_secret(p_credentials::text,v_name,
    '9N8 per-user exchange credentials; provider=' || p_provider || '; environment=' || p_environment);
  insert into public.n8_exchange_connections(
    id,user_id,provider,environment,label,account_hint,vault_secret_id,permissions,status,last_validated_at
  ) values (v_connection_id,v_user_id,p_provider,p_environment,p_label,p_account_hint,v_secret_id,
    coalesce(p_permissions,'{}'::jsonb),'active',now());
  return v_connection_id;
end;
$$;

create or replace function public.n8_get_exchange_credentials(p_telegram_user_id bigint,p_connection_id uuid)
returns jsonb language plpgsql security definer set search_path = ''
as $$
declare v_secret jsonb; v_role text;
begin
  v_role := coalesce(current_setting('request.jwt.claim.role',true),
    (nullif(current_setting('request.jwt.claims',true),'')::jsonb ->> 'role'),'');
  if v_role <> 'service_role' then raise exception 'not_allowed'; end if;
  select ds.decrypted_secret::jsonb into v_secret
  from public.n8_exchange_connections c join public.n8_users u on u.id=c.user_id
  join vault.decrypted_secrets ds on ds.id=c.vault_secret_id
  where u.telegram_user_id=p_telegram_user_id and c.id=p_connection_id
    and c.status='active' and u.status='active';
  if v_secret is null then raise exception 'connection_not_found_or_revoked'; end if;
  return v_secret;
end;
$$;

create or replace function public.n8_revoke_exchange_connection(p_telegram_user_id bigint,p_connection_id uuid)
returns boolean language plpgsql security definer set search_path = ''
as $$
declare v_secret_id uuid; v_role text;
begin
  v_role := coalesce(current_setting('request.jwt.claim.role',true),
    (nullif(current_setting('request.jwt.claims',true),'')::jsonb ->> 'role'),'');
  if v_role <> 'service_role' then raise exception 'not_allowed'; end if;
  select c.vault_secret_id into v_secret_id from public.n8_exchange_connections c
  join public.n8_users u on u.id=c.user_id
  where u.telegram_user_id=p_telegram_user_id and c.id=p_connection_id and c.status <> 'revoked'
  for update of c;
  if v_secret_id is null then return false; end if;
  delete from vault.secrets where id=v_secret_id;
  update public.n8_exchange_connections set status='revoked',vault_secret_id=null,updated_at=now()
  where id=p_connection_id;
  return true;
end;
$$;

revoke all on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text) from public,anon,authenticated;
revoke all on function public.n8_get_exchange_credentials(bigint,uuid) from public,anon,authenticated;
revoke all on function public.n8_revoke_exchange_connection(bigint,uuid) from public,anon,authenticated;
grant execute on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text) to service_role;
grant execute on function public.n8_get_exchange_credentials(bigint,uuid) to service_role;
grant execute on function public.n8_revoke_exchange_connection(bigint,uuid) to service_role;
