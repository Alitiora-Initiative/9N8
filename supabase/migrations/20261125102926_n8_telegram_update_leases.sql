alter table public.n8_telegram_updates
  add column if not exists lease_until timestamptz,
  add column if not exists attempts integer not null default 0;

create or replace function public.n8_claim_telegram_update(
  p_update_id bigint,
  p_lease_seconds integer default 60
) returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_claimed bigint;
  v_role text;
  v_lease integer := greatest(10, least(coalesce(p_lease_seconds, 60), 300));
begin
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  if v_role <> 'service_role' then raise exception 'not_allowed'; end if;

  insert into public.n8_telegram_updates(update_id, lease_until, attempts)
  values(p_update_id, now() + make_interval(secs => v_lease), 1)
  on conflict (update_id) do update
    set lease_until = now() + make_interval(secs => v_lease),
        attempts = public.n8_telegram_updates.attempts + 1
    where public.n8_telegram_updates.processed_at is null
      and (public.n8_telegram_updates.lease_until is null
        or public.n8_telegram_updates.lease_until < now())
  returning update_id into v_claimed;

  return v_claimed is not null;
end;
$$;

revoke all on function public.n8_claim_telegram_update(bigint,integer) from public, anon, authenticated;
grant execute on function public.n8_claim_telegram_update(bigint,integer) to service_role;
