-- 9N8 Binance Spot LIVE credential storage and live-market paper simulator.
-- This migration intentionally does not enable real order execution.
alter table public.n8_exchange_connections
  drop constraint if exists n8_exchange_connections_environment_check;
alter table public.n8_exchange_connections
  drop constraint if exists n8_exchange_connections_provider_check;
alter table public.n8_exchange_connections
  add constraint n8_exchange_connections_environment_check check (environment = 'live');
alter table public.n8_exchange_connections
  add constraint n8_exchange_connections_provider_check check (provider = 'binance');

alter table public.n8_risk_policies
  drop constraint if exists n8_risk_policies_environment_check;
alter table public.n8_risk_policies
  alter column environment set default 'paper';
update public.n8_risk_policies set environment = 'paper' where environment in ('demo','testnet');
alter table public.n8_risk_policies
  add constraint n8_risk_policies_environment_check check (environment in ('paper','live'));

CREATE OR REPLACE FUNCTION public.n8_store_exchange_secret(p_telegram_user_id bigint, p_provider text, p_environment text, p_label text, p_credentials jsonb, p_permissions jsonb DEFAULT '{}'::jsonb, p_account_hint text DEFAULT NULL::text)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
declare
  v_user_id uuid;
  v_connection_id uuid := gen_random_uuid();
  v_secret_id uuid;
  v_role text;
  v_name text := 'n8-exchange-' || v_connection_id::text;
begin
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  if v_role <> 'service_role' then raise exception 'not_allowed'; end if;
  if p_provider <> 'binance' then raise exception 'unsupported_provider'; end if;
  if p_environment <> 'live' then raise exception 'unsupported_environment'; end if;
  if p_credentials is null or p_credentials = '{}'::jsonb then raise exception 'empty_credentials'; end if;

  select u.id into v_user_id
  from public.n8_users u
  where u.telegram_user_id = p_telegram_user_id and u.status = 'active'
  for update;
  if v_user_id is null then raise exception 'user_not_registered'; end if;

  v_secret_id := vault.create_secret(
    p_credentials::text,
    v_name,
    '9N8 per-user Binance Spot LIVE credentials; withdrawals and transfers must be disabled'
  );
  insert into public.n8_exchange_connections(
    id,user_id,provider,environment,label,account_hint,vault_secret_id,permissions,status,last_validated_at
  ) values (
    v_connection_id,v_user_id,'binance','live',p_label,p_account_hint,v_secret_id,
    coalesce(p_permissions,'{}'::jsonb),'active',now()
  );
  return v_connection_id;
end;
$function$


revoke all on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text) from public, anon, authenticated;
grant execute on function public.n8_store_exchange_secret(bigint,text,text,text,jsonb,jsonb,text) to service_role;

CREATE OR REPLACE FUNCTION public.n8_start_paper_session(p_user_id uuid, p_market_type text DEFAULT 'crypto'::text, p_chat_id bigint DEFAULT NULL::bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public', 'vault', 'net'
AS $function$
declare
  v_session public.n8_paper_sessions%rowtype;
  v_request_id bigint;
begin
  if p_market_type <> 'crypto' then
    raise exception 'paper_market_not_supported_yet';
  end if;
  if not exists (select 1 from public.n8_users u where u.id = p_user_id) then
    raise exception 'paper_user_not_found';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  if exists (select 1 from public.n8_risk_policies where user_id=p_user_id and kill_switch=true) then
    raise exception 'paper_engine_kill_switch_active';
  end if;

  select * into v_session
  from public.n8_paper_sessions
  where user_id = p_user_id and status = 'running'
  order by created_at desc
  limit 1
  for update;

  if not found then
    insert into public.n8_paper_sessions (
      user_id, market_type, status, evaluation_window_days, started_at,
      last_activity_at, initial_balance_usd, cash_balance_usd, config
    ) values (
      p_user_id, 'crypto', 'running', 30, now(), now(), 1000, 1000,
      jsonb_build_object(
        'engine','ema9-ema21-cross-v1',
        'timeframe','1m',
        'provider','binance-public-spot',
        'execution_model','binance_spot_orderbook_vwap',
        'initial_balance_usd',1000,
        'max_position_usd',50,
        'max_open_positions',3,
        'fee_rate',0.001,
        'max_slippage_bps',100,
        'max_spread_bps',75,
        'stop_loss_pct',0.02,
        'take_profit_pct',0.04,
        'telegram_chat_id',p_chat_id,
        'live_execution',false,
        'exchange_credentials_used',false,
        'wallet_signing_used',false
      )
    ) returning * into v_session;
  else
    update public.n8_paper_sessions
    set config = config || jsonb_build_object(
          'telegram_chat_id', p_chat_id,
          'execution_model','binance_spot_orderbook_vwap',
          'max_slippage_bps',100,
          'max_spread_bps',75,
          'live_execution',false,
          'exchange_credentials_used',false,
          'wallet_signing_used',false
        ),
        updated_at = now()
    where id = v_session.id
    returning * into v_session;
  end if;

  select net.http_post(
    url := 'https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/n8-paper-worker',
    headers := jsonb_build_object(
      'Content-Type','application/json',
      'x-9n8-worker-key',(select decrypted_secret from vault.decrypted_secrets where name='n8_paper_worker_key')
    ),
    body := jsonb_build_object('trigger','telegram_start','session_id',v_session.id),
    timeout_milliseconds := 8000
  ) into v_request_id;

  return jsonb_build_object(
    'ok',true,'session_id',v_session.id,'status',v_session.status,
    'market_type',v_session.market_type,'initial_balance_usd',v_session.initial_balance_usd,
    'cash_balance_usd',v_session.cash_balance_usd,'started_at',v_session.started_at,
    'worker_request_id',v_request_id
  );
end;
$function$


revoke all on function public.n8_start_paper_session(uuid,text,bigint) from public, anon, authenticated;
grant execute on function public.n8_start_paper_session(uuid,text,bigint) to service_role;

CREATE OR REPLACE FUNCTION public.n8_apply_paper_trade(p_session_id uuid, p_worker_lock_token uuid, p_instrument_id uuid, p_symbol text, p_side text, p_fill_price numeric, p_notional_usd numeric, p_fee_rate numeric, p_stop_loss_pct numeric, p_take_profit_pct numeric, p_idempotency_key text, p_signal_reason text, p_reference_price numeric, p_candle_open_time timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
declare
  v_session public.n8_paper_sessions%rowtype;
  v_position public.n8_paper_positions%rowtype;
  v_existing public.n8_paper_trades%rowtype;
  v_fee_rate numeric := least(greatest(coalesce(p_fee_rate,0.001),0),0.02);
  v_fee numeric;
  v_notional numeric;
  v_quantity numeric;
  v_cash numeric;
  v_realized numeric;
  v_stop numeric := least(greatest(coalesce(p_stop_loss_pct,0.02),0.005),0.20);
  v_target numeric := least(greatest(coalesce(p_take_profit_pct,0.04),0.01),0.50);
  v_max_position numeric;
  v_max_open integer;
  v_open_count integer;
  v_trade_id uuid;
  v_slippage_bps numeric;
  v_execution_metadata jsonb;
begin
  if p_side not in ('buy','sell') or p_fill_price is null or p_fill_price <= 0
     or p_reference_price is null or p_reference_price <= 0
     or length(coalesce(p_idempotency_key,'')) < 8 then
    raise exception 'paper_trade_input_invalid';
  end if;

  select * into v_session from public.n8_paper_sessions
  where id=p_session_id for update;
  if not found or v_session.status <> 'running'
     or v_session.worker_lock_token is distinct from p_worker_lock_token then
    raise exception 'paper_session_lock_invalid';
  end if;

  select * into v_existing from public.n8_paper_trades
  where session_id=p_session_id and idempotency_key=p_idempotency_key;
  if found then
    return jsonb_build_object('ok',true,'duplicate',true,'trade_id',v_existing.id,
      'cash_balance_usd',v_session.cash_balance_usd);
  end if;

  v_max_position := least(greatest(coalesce((v_session.config->>'max_position_usd')::numeric,50),5),250);
  v_max_open := least(greatest(coalesce((v_session.config->>'max_open_positions')::integer,3),1),10);
  v_cash := v_session.cash_balance_usd;
  v_slippage_bps := abs((p_fill_price-p_reference_price)/p_reference_price*10000);
  v_execution_metadata := jsonb_build_object(
    'execution','paper',
    'market_data_source','binance_spot',
    'execution_model','binance_spot_orderbook_vwap',
    'fee_rate',v_fee_rate,
    'slippage_bps',round(v_slippage_bps,4),
    'reference_price',p_reference_price,
    'fill_price',p_fill_price,
    'reference_price_type','midpoint_at_simulated_execution'
  );

  if p_side='buy' then
    if p_notional_usd is null or p_notional_usd <= 0 or p_notional_usd > v_max_position then
      return jsonb_build_object('ok',false,'reason','paper_order_size_rejected');
    end if;
    select count(*) into v_open_count from public.n8_paper_positions
      where session_id=p_session_id and status='open';
    if v_open_count >= v_max_open then
      return jsonb_build_object('ok',false,'reason','max_open_positions_reached');
    end if;
    if exists (select 1 from public.n8_paper_positions
      where session_id=p_session_id and instrument_id=p_instrument_id and status='open') then
      return jsonb_build_object('ok',false,'reason','position_already_open');
    end if;
    v_notional := round(p_notional_usd,8);
    v_fee := round(v_notional*v_fee_rate,8);
    if v_cash < v_notional+v_fee then
      return jsonb_build_object('ok',false,'reason','paper_cash_insufficient');
    end if;
    v_quantity := v_notional/p_fill_price;
    insert into public.n8_paper_positions (
      session_id,user_id,instrument_id,symbol,quantity,average_entry_price,
      mark_price,entry_fees_usd,stop_loss_price,take_profit_price,status
    ) values (
      p_session_id,v_session.user_id,p_instrument_id,p_symbol,v_quantity,p_fill_price,
      p_reference_price,v_fee,p_fill_price*(1-v_stop),p_fill_price*(1+v_target),'open'
    ) returning * into v_position;
    update public.n8_paper_sessions set cash_balance_usd=v_cash-v_notional-v_fee, updated_at=now()
      where id=p_session_id;
    insert into public.n8_paper_trades (
      session_id,user_id,instrument_id,symbol,side,quantity,fill_price,reference_price,
      notional_usd,fee_usd,idempotency_key,signal_reason,candle_open_time,metadata
    ) values (
      p_session_id,v_session.user_id,p_instrument_id,p_symbol,'buy',v_quantity,p_fill_price,p_reference_price,
      v_notional,v_fee,p_idempotency_key,coalesce(p_signal_reason,'EMA entry'),p_candle_open_time,
      v_execution_metadata
    ) returning id into v_trade_id;
    return jsonb_build_object('ok',true,'trade_id',v_trade_id,'quantity',v_quantity,
      'fee_usd',v_fee,'cash_balance_usd',v_cash-v_notional-v_fee,'position',to_jsonb(v_position));
  end if;

  select * into v_position from public.n8_paper_positions
  where session_id=p_session_id and instrument_id=p_instrument_id and status='open'
  for update;
  if not found then return jsonb_build_object('ok',false,'reason','open_position_not_found'); end if;
  v_quantity := v_position.quantity;
  v_notional := round(v_quantity*p_fill_price,8);
  v_fee := round(v_notional*v_fee_rate,8);
  v_realized := round((p_fill_price-v_position.average_entry_price)*v_quantity-v_position.entry_fees_usd-v_fee,8);
  update public.n8_paper_positions set
    quantity=0,mark_price=p_reference_price,realized_pnl_usd=v_realized,
    unrealized_pnl_usd=0,status='closed',closed_at=now(),updated_at=now()
  where id=v_position.id;
  update public.n8_paper_sessions set cash_balance_usd=v_cash+v_notional-v_fee,updated_at=now()
    where id=p_session_id;
  insert into public.n8_paper_trades (
    session_id,user_id,instrument_id,symbol,side,quantity,fill_price,reference_price,
    notional_usd,fee_usd,realized_pnl_usd,idempotency_key,signal_reason,candle_open_time,metadata
  ) values (
    p_session_id,v_session.user_id,p_instrument_id,p_symbol,'sell',v_quantity,p_fill_price,p_reference_price,
    v_notional,v_fee,v_realized,p_idempotency_key,coalesce(p_signal_reason,'EMA exit'),p_candle_open_time,
    v_execution_metadata || jsonb_build_object('entry_fees_usd',v_position.entry_fees_usd)
  ) returning id into v_trade_id;
  return jsonb_build_object('ok',true,'trade_id',v_trade_id,'quantity',v_quantity,
    'fee_usd',v_fee,'realized_pnl_usd',v_realized,'cash_balance_usd',v_cash+v_notional-v_fee);
end;
$function$


revoke all on function public.n8_apply_paper_trade(uuid,uuid,uuid,text,text,numeric,numeric,numeric,numeric,numeric,text,text,numeric,timestamptz) from public, anon, authenticated;
grant execute on function public.n8_apply_paper_trade(uuid,uuid,uuid,text,text,numeric,numeric,numeric,numeric,numeric,text,text,numeric,timestamptz) to service_role;

update public.n8_paper_sessions
set config = config || jsonb_build_object(
  'execution_model','binance_spot_orderbook_vwap',
  'max_slippage_bps',100,
  'max_spread_bps',75,
  'live_execution',false,
  'exchange_credentials_used',false,
  'wallet_signing_used',false
),
updated_at = now()
where status = 'running';
