-- Defense in depth: the emergency stop must block the scheduler and any in-flight paper fill.
create or replace function public.n8_claim_paper_sessions(p_limit integer default 20)
returns table (
  id uuid, user_id uuid, market_type text, config jsonb,
  initial_balance_usd numeric, cash_balance_usd numeric,
  tick_count bigint, worker_lock_token uuid
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  return query
  with candidates as (
    select s.id
    from public.n8_paper_sessions s
    where s.status = 'running'
      and (s.worker_lock_until is null or s.worker_lock_until < now())
      and not exists (
        select 1 from public.n8_risk_policies rp
        where rp.user_id = s.user_id and rp.kill_switch = true
      )
    order by s.started_at asc
    limit least(greatest(coalesce(p_limit, 20), 1), 50)
    for update skip locked
  ),
  claimed as (
    update public.n8_paper_sessions s
    set worker_lock_token = gen_random_uuid(),
        worker_lock_until = now() + interval '90 seconds',
        updated_at = now()
    from candidates c
    where s.id = c.id
    returning s.id, s.user_id, s.market_type, s.config,
      s.initial_balance_usd, s.cash_balance_usd, s.tick_count, s.worker_lock_token
  )
  select claimed.id, claimed.user_id, claimed.market_type, claimed.config,
    claimed.initial_balance_usd, claimed.cash_balance_usd, claimed.tick_count, claimed.worker_lock_token
  from claimed;
end;
$$;

create or replace function public.n8_guard_paper_trade_kill_switch()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if exists (
    select 1 from public.n8_risk_policies rp
    where rp.user_id = new.user_id and rp.kill_switch = true
  ) then
    raise exception using errcode = '55000', message = 'paper_kill_switch_active';
  end if;
  return new;
end;
$$;

revoke all on function public.n8_guard_paper_trade_kill_switch() from public, anon, authenticated;
grant execute on function public.n8_guard_paper_trade_kill_switch() to service_role;

drop trigger if exists n8_paper_trade_kill_switch_guard on public.n8_paper_trades;
create trigger n8_paper_trade_kill_switch_guard
before insert on public.n8_paper_trades
for each row execute function public.n8_guard_paper_trade_kill_switch();
