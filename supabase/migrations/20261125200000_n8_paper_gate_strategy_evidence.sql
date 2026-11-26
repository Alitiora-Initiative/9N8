-- 9N8 paper-evaluation evidence alignment.
-- This migration makes the 30-day gate evaluate the real Binance-market paper engine's
-- closed virtual trades. It does NOT enable live trading or create exchange orders.

CREATE OR REPLACE FUNCTION public.n8_ensure_default_paper_strategy(p_user_id uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  v_role text;
  v_config jsonb := jsonb_build_object(
    'engine', 'ema9-ema21-cross-v1',
    'timeframe', '1m',
    'entry', 'EMA9 crosses above EMA21; RSI14 between 48 and 72; close above EMA21; positive volume',
    'exit', 'EMA9 crosses below EMA21 or 2% stop-loss or 4% take-profit',
    'risk', jsonb_build_object('feeRate', 0.001, 'maxPositionUsd', 50, 'maxOpenPositions', 3),
    'marketData', 'Binance public Spot candles and order-book depth',
    'evaluation', 'Closed virtual paper trades; fees and visible-depth slippage included'
  );
  v_id uuid;
BEGIN
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  IF v_role <> 'service_role' THEN RAISE EXCEPTION 'not_allowed'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.n8_users WHERE id = p_user_id AND status = 'active') THEN
    RAISE EXCEPTION 'paper_strategy_user_not_active';
  END IF;

  INSERT INTO public.n8_strategy_versions(
    user_id, name, version, market_type, config, checksum, enabled
  )
  VALUES (
    p_user_id, 'EMA9-EMA21 + RSI14 (Binance Spot Paper)', 1, 'crypto',
    v_config, md5(v_config::text), true
  )
  ON CONFLICT (user_id, name, version)
  DO UPDATE SET
    config = EXCLUDED.config,
    checksum = EXCLUDED.checksum,
    enabled = true
  RETURNING id INTO v_id;
  RETURN v_id;
END;
$function$;

REVOKE ALL ON FUNCTION public.n8_ensure_default_paper_strategy(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.n8_ensure_default_paper_strategy(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.n8_start_paper_session(
  p_user_id uuid,
  p_market_type text DEFAULT 'crypto'::text,
  p_chat_id bigint DEFAULT NULL::bigint
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public', 'vault', 'net'
AS $function$
DECLARE
  v_session public.n8_paper_sessions%rowtype;
  v_request_id bigint;
  v_strategy_id uuid;
BEGIN
  IF p_market_type <> 'crypto' THEN RAISE EXCEPTION 'paper_market_not_supported_yet'; END IF;
  IF NOT EXISTS (SELECT 1 FROM public.n8_users u WHERE u.id = p_user_id AND u.status = 'active') THEN
    RAISE EXCEPTION 'paper_user_not_found';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  IF EXISTS (SELECT 1 FROM public.n8_risk_policies WHERE user_id=p_user_id AND kill_switch=true) THEN
    RAISE EXCEPTION 'paper_engine_kill_switch_active';
  END IF;

  v_strategy_id := public.n8_ensure_default_paper_strategy(p_user_id);

  SELECT * INTO v_session
  FROM public.n8_paper_sessions
  WHERE user_id = p_user_id AND status = 'running'
  ORDER BY created_at DESC
  LIMIT 1
  FOR UPDATE;

  IF NOT FOUND THEN
    INSERT INTO public.n8_paper_sessions (
      user_id, market_type, status, evaluation_window_days, started_at,
      last_activity_at, initial_balance_usd, cash_balance_usd, config
    )
    VALUES (
      p_user_id, 'crypto', 'running', 30, now(), now(), 1000, 1000,
      jsonb_build_object(
        'engine','ema9-ema21-cross-v1',
        'strategy_version_id',v_strategy_id,
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
    )
    RETURNING * INTO v_session;
  ELSE
    UPDATE public.n8_paper_sessions
    SET config = config || jsonb_build_object(
          'telegram_chat_id', p_chat_id,
          'strategy_version_id', v_strategy_id,
          'execution_model','binance_spot_orderbook_vwap',
          'max_slippage_bps',100,
          'max_spread_bps',75,
          'live_execution',false,
          'exchange_credentials_used',false,
          'wallet_signing_used',false
        ),
        updated_at = now()
    WHERE id = v_session.id
    RETURNING * INTO v_session;
  END IF;

  SELECT net.http_post(
    url := 'https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/n8-paper-worker',
    headers := jsonb_build_object(
      'Content-Type','application/json',
      'x-9n8-worker-key',(SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name='n8_paper_worker_key')
    ),
    body := jsonb_build_object('trigger','telegram_start','session_id',v_session.id),
    timeout_milliseconds := 8000
  ) INTO v_request_id;

  RETURN jsonb_build_object(
    'ok',true,'session_id',v_session.id,'status',v_session.status,
    'market_type',v_session.market_type,'initial_balance_usd',v_session.initial_balance_usd,
    'cash_balance_usd',v_session.cash_balance_usd,'started_at',v_session.started_at,
    'strategy_version_id',v_strategy_id,'worker_request_id',v_request_id
  );
END;
$function$;

REVOKE ALL ON FUNCTION public.n8_start_paper_session(uuid,text,bigint) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.n8_start_paper_session(uuid,text,bigint) TO service_role;

-- Attach a real, user-owned strategy version to already-running paper sessions.
DO $block$
DECLARE
  r record;
  v_strategy_id uuid;
  v_config jsonb := jsonb_build_object(
    'engine', 'ema9-ema21-cross-v1',
    'timeframe', '1m',
    'entry', 'EMA9 crosses above EMA21; RSI14 between 48 and 72; close above EMA21; positive volume',
    'exit', 'EMA9 crosses below EMA21 or 2% stop-loss or 4% take-profit',
    'risk', jsonb_build_object('feeRate', 0.001, 'maxPositionUsd', 50, 'maxOpenPositions', 3),
    'marketData', 'Binance public Spot candles and order-book depth',
    'evaluation', 'Closed virtual paper trades; fees and visible-depth slippage included'
  );
BEGIN
  FOR r IN SELECT DISTINCT user_id FROM public.n8_paper_sessions WHERE status IN ('running','completed')
  LOOP
    INSERT INTO public.n8_strategy_versions(
      user_id, name, version, market_type, config, checksum, enabled
    )
    VALUES (
      r.user_id, 'EMA9-EMA21 + RSI14 (Binance Spot Paper)', 1, 'crypto',
      v_config, md5(v_config::text), true
    )
    ON CONFLICT (user_id, name, version)
    DO UPDATE SET enabled = true
    RETURNING id INTO v_strategy_id;

    UPDATE public.n8_paper_sessions
    SET config = config || jsonb_build_object('strategy_version_id', v_strategy_id),
        updated_at = now()
    WHERE user_id = r.user_id
      AND status IN ('running','completed')
      AND nullif(config->>'strategy_version_id','') IS NULL;
  END LOOP;
END;
$block$;

CREATE OR REPLACE FUNCTION public.n8_live_gate_assess(
  p_user_id uuid,
  p_window_days integer,
  p_account_type text,
  p_connection_id uuid,
  p_market_type text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  v_role text;
  v_user public.n8_users%rowtype;
  v_policy public.n8_risk_policies%rowtype;
  v_session public.n8_paper_sessions%rowtype;
  v_balance public.n8_live_balance_checks%rowtype;
  v_readiness public.n8_live_connector_readiness%rowtype;
  v_delegation public.n8_wallet_delegations%rowtype;
  v_provider text;
  v_days numeric := 0;
  v_total_trades integer := 0;
  v_strategy_count integer := 0;
  v_strategy_pass_count integer := 0;
  v_unresolved_orders integer := 0;
  v_pending_proposals integer := 0;
  v_open_positions integer := 0;
  v_reasons text[] := ARRAY[]::text[];
  v_eligible boolean := false;
  v_min_balance numeric := 10;
BEGIN
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  IF v_role <> 'service_role' THEN RAISE EXCEPTION 'not_allowed'; END IF;
  IF p_window_days NOT IN (7,30) THEN RAISE EXCEPTION 'invalid_window_days'; END IF;

  IF p_window_days = 7 THEN
    RETURN jsonb_build_object(
      'eligible', false, 'status', 'progress_review_only',
      'reasons', jsonb_build_array('The 7-day review is a progress checkpoint only. Live eligibility requires the completed 30-day forward-paper gate.'),
      'safety', jsonb_build_object('withdrawalsDisabled',true,'agentTransfersDisabled',true,'liveTradingEnabled',false)
    );
  END IF;

  IF p_account_type NOT IN ('exchange','wallet','broker') THEN RAISE EXCEPTION 'invalid_account_type'; END IF;
  IF p_market_type NOT IN ('solana','crypto','stocks','forex') THEN RAISE EXCEPTION 'invalid_market_type'; END IF;

  SELECT * INTO v_user FROM public.n8_users WHERE id=p_user_id AND status='active';
  IF NOT FOUND THEN
    RETURN jsonb_build_object('eligible',false,'status','ineligible','reasons',jsonb_build_array('Account is not active.'));
  END IF;

  SELECT * INTO v_policy FROM public.n8_risk_policies WHERE user_id=p_user_id;
  IF NOT FOUND THEN
    v_reasons := array_append(v_reasons,'Risk policy is missing.');
  ELSE
    v_min_balance := greatest(10,v_policy.min_live_balance_usd);
    IF v_policy.kill_switch THEN v_reasons := array_append(v_reasons,'Emergency stop is active.'); END IF;
  END IF;

  SELECT * INTO v_session
  FROM public.n8_paper_sessions
  WHERE user_id=p_user_id
    AND status IN ('running','completed')
    AND evaluation_window_days=p_window_days
    AND (market_type=p_market_type OR market_type='multi')
  ORDER BY started_at DESC NULLS LAST
  LIMIT 1;

  IF NOT FOUND OR v_session.started_at IS NULL THEN
    v_reasons := array_append(v_reasons,'No real paper-trading session has been started for this market.');
  ELSE
    v_days := greatest(0,extract(epoch FROM (now()-v_session.started_at))/86400.0);
    IF v_days < p_window_days THEN
      v_reasons := array_append(v_reasons,format('Paper trading has run for %s of %s required days.',round(v_days,1),p_window_days));
    END IF;
    IF v_session.status <> 'completed' THEN
      v_reasons := array_append(v_reasons,'The 30-day paper session must be finalized after the engine has closed all virtual positions.');
    END IF;
    IF nullif(v_session.config->>'strategy_version_id','') IS NULL THEN
      v_reasons := array_append(v_reasons,'The paper session is missing its versioned strategy record. Restart/resume paper to repair the session metadata.');
    END IF;

    SELECT count(*)::integer INTO v_total_trades
    FROM public.n8_paper_trades t
    WHERE t.session_id=v_session.id
      AND t.side='sell'
      AND t.realized_pnl_usd IS NOT NULL
      AND t.metadata->>'execution_model'='binance_spot_orderbook_vwap'
      AND t.metadata->>'market_data_source'='binance_spot'
      AND t.metadata ? 'fee_rate'
      AND t.metadata ? 'slippage_bps';

    SELECT count(*)::integer INTO v_strategy_count
    FROM public.n8_strategy_versions sv
    WHERE (sv.user_id=p_user_id OR sv.user_id IS NULL)
      AND sv.enabled=true
      AND (sv.market_type=p_market_type OR sv.market_type='multi');

    WITH strategy_trades AS (
      SELECT
        sv.id AS strategy_id,
        t.realized_pnl_usd AS net_pnl,
        t.filled_at,
        row_number() OVER (PARTITION BY sv.id ORDER BY t.filled_at,t.id) AS rn,
        count(*) OVER (PARTITION BY sv.id) AS trade_count
      FROM public.n8_strategy_versions sv
      JOIN public.n8_paper_sessions s
        ON s.user_id=p_user_id
       AND s.status IN ('running','completed')
       AND s.started_at >= v_session.started_at
       AND (s.config->>'strategy_version_id')::uuid=sv.id
      JOIN public.n8_paper_trades t
        ON t.session_id=s.id
       AND t.side='sell'
       AND t.realized_pnl_usd IS NOT NULL
       AND t.metadata->>'execution_model'='binance_spot_orderbook_vwap'
       AND t.metadata->>'market_data_source'='binance_spot'
       AND t.metadata ? 'fee_rate'
       AND t.metadata ? 'slippage_bps'
       AND t.filled_at >= v_session.started_at
      WHERE (sv.user_id=p_user_id OR sv.user_id IS NULL)
        AND sv.enabled=true
        AND (sv.market_type=p_market_type OR sv.market_type='multi')
    ),
    equity_curve AS (
      SELECT *,
        1000 + sum(net_pnl) OVER (PARTITION BY strategy_id ORDER BY filled_at,rn ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS equity
      FROM strategy_trades
    ),
    drawdowns AS (
      SELECT *,
        max(equity) OVER (PARTITION BY strategy_id ORDER BY filled_at,rn ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS peak
      FROM equity_curve
    ),
    scored AS (
      SELECT *,
        ((peak-equity)/greatest(peak,1))*100 AS drawdown_pct,
        floor(((rn-1)::numeric*4)/greatest(trade_count,1))::integer AS fold,
        rn > floor(trade_count*0.7) AS out_of_sample
      FROM drawdowns
    ),
    per_strategy AS (
      SELECT strategy_id,
        count(*)::integer AS trades_count,
        avg(net_pnl) AS avg_net_pnl,
        max(drawdown_pct) AS max_drawdown_pct,
        sum(net_pnl) FILTER (WHERE fold=0) AS fold_0_pnl,
        sum(net_pnl) FILTER (WHERE fold=1) AS fold_1_pnl,
        sum(net_pnl) FILTER (WHERE fold=2) AS fold_2_pnl,
        sum(net_pnl) FILTER (WHERE fold=3) AS fold_3_pnl,
        sum(net_pnl) FILTER (WHERE out_of_sample) AS out_of_sample_pnl
      FROM scored
      GROUP BY strategy_id
    )
    SELECT count(*) FILTER (
      WHERE coalesce(ps.trades_count,0) >= 100
        AND coalesce(ps.avg_net_pnl,0) > 0
        AND coalesce(ps.max_drawdown_pct,101) <= coalesce(v_policy.max_drawdown_pct,10)
        AND (
          (CASE WHEN coalesce(ps.fold_0_pnl,0)>0 THEN 1 ELSE 0 END) +
          (CASE WHEN coalesce(ps.fold_1_pnl,0)>0 THEN 1 ELSE 0 END) +
          (CASE WHEN coalesce(ps.fold_2_pnl,0)>0 THEN 1 ELSE 0 END) +
          (CASE WHEN coalesce(ps.fold_3_pnl,0)>0 THEN 1 ELSE 0 END)
        ) >= 3
        AND coalesce(ps.out_of_sample_pnl,0) > 0
    )::integer INTO v_strategy_pass_count
    FROM public.n8_strategy_versions sv
    LEFT JOIN per_strategy ps ON ps.strategy_id=sv.id
    WHERE (sv.user_id=p_user_id OR sv.user_id IS NULL)
      AND sv.enabled=true
      AND (sv.market_type=p_market_type OR sv.market_type='multi');

    IF v_strategy_count=0 THEN
      v_reasons := array_append(v_reasons,'No enabled, versioned strategy has measurable paper results.');
    ELSIF v_strategy_pass_count <> v_strategy_count THEN
      v_reasons := array_append(v_reasons,'Every enabled strategy needs 100 closed paper trades, positive net expectancy after fees and slippage, drawdown within policy, at least three profitable chronological walk-forward folds, and a profitable final 30% out-of-sample segment.');
    END IF;
    IF v_total_trades=0 THEN
      v_reasons := array_append(v_reasons,'No completed paper exits with recorded net P&L, fees and visible-depth slippage were found.');
    END IF;
  END IF;

  SELECT count(*)::integer INTO v_unresolved_orders
  FROM public.n8_orders o
  WHERE o.user_id=p_user_id AND o.environment='live'
    AND (o.status IN ('pending','submitted','open','partially_filled','unknown')
      OR (o.status IN ('filled','cancelled','rejected','failed') AND o.reconciled_at IS NULL));
  IF v_unresolved_orders>0 THEN
    v_reasons := array_append(v_reasons,'Some live orders are unresolved or not reconciled.');
  END IF;

  SELECT count(*)::integer INTO v_pending_proposals
  FROM public.n8_trade_proposals p
  WHERE p.user_id=p_user_id AND p.environment='live'
    AND p.status IN ('pending','approved','executing','submitted','partially_filled');
  IF v_pending_proposals>0 THEN
    v_reasons := array_append(v_reasons,'Pending live trade proposals must be resolved before migration.');
  END IF;

  SELECT count(*)::integer INTO v_open_positions
  FROM public.n8_paper_positions p
  WHERE p.session_id=v_session.id AND p.user_id=p_user_id AND p.status='open';
  IF v_open_positions>0 THEN
    v_reasons := array_append(v_reasons,'Close and reconcile all open virtual positions before finalizing the paper evaluation.');
  END IF;

  IF p_account_type='exchange' THEN
    SELECT e.provider INTO v_provider
    FROM public.n8_exchange_connections e
    WHERE e.id=p_connection_id AND e.user_id=p_user_id
      AND e.status='active' AND e.environment='live';
    IF v_provider IS NULL THEN
      v_reasons := array_append(v_reasons,'A validated live exchange connection is not available. Demo/testnet keys cannot enable live trading.');
    END IF;
  ELSIF p_account_type='wallet' THEN
    SELECT * INTO v_delegation
    FROM public.n8_wallet_delegations d
    WHERE d.user_id=p_user_id
      AND d.wallet_connection_id=p_connection_id
      AND d.status='active'
      AND d.starts_at<=now()
      AND d.expires_at>now()
      AND d.agent_can_withdraw=false
      AND d.agent_can_transfer=false
      AND d.owner_controls_withdrawals=true
      AND nullif(d.owner_authorization_hash,'') IS NOT NULL
      AND d.last_verified_at>now()-interval '7 days'
      AND cardinality(d.allowed_targets)>0
      AND cardinality(d.allowed_selectors)>0
      AND cardinality(d.allowed_venues)>0
      AND lower(coalesce(d.recipient_address,''))=lower(d.smart_account_address)
      AND d.max_order_usd<=coalesce(v_policy.max_order_usd,0)
      AND d.max_daily_notional_usd<=coalesce(v_policy.max_daily_notional_usd,0)
    ORDER BY d.created_at DESC LIMIT 1;
    IF NOT FOUND THEN
      v_reasons := array_append(v_reasons,'A verified wallet-owner-authorized trade-only smart-account delegation with target, function, venue, recipient, amount and expiry restrictions is not active.');
    ELSE
      v_provider := v_delegation.execution_provider;
      IF v_provider NOT IN ('jupiter','uniswap_v3') THEN
        v_reasons := array_append(v_reasons,'The wallet execution provider is not on the supported live allowlist.');
      ELSIF p_market_type='solana' AND
        (v_delegation.chain_namespace<>'solana' OR v_provider<>'jupiter') THEN
        v_reasons := array_append(v_reasons,'Solana live trading requires the certified Jupiter session-program connector.');
      ELSIF p_market_type='crypto' AND
        (v_delegation.chain_namespace<>'eip155' OR v_provider<>'uniswap_v3') THEN
        v_reasons := array_append(v_reasons,'EVM crypto live trading requires the certified Uniswap v3 Safe-role connector.');
      ELSIF p_market_type NOT IN ('solana','crypto') THEN
        v_reasons := array_append(v_reasons,'Wallet execution is limited to crypto markets; stocks and forex need a broker.');
      END IF;
    END IF;
  ELSE
    v_reasons := array_append(v_reasons,'A supported live broker connection is not yet configured for this market.');
  END IF;

  IF v_provider IS NOT NULL THEN
    SELECT * INTO v_readiness
    FROM public.n8_live_connector_readiness r
    WHERE r.provider=v_provider
      AND r.account_type=p_account_type
      AND r.market_type=p_market_type
      AND r.environment='live'
      AND r.status='passed'
      AND r.live_order_execution_enabled=true
      AND r.withdrawal_endpoint_absent=true
      AND r.transfer_endpoint_absent=true
      AND r.reconciliation_test_passed=true
      AND r.security_test_passed=true
      AND r.checked_at>now()-interval '7 days'
    ORDER BY r.checked_at DESC LIMIT 1;
    IF NOT FOUND THEN
      v_reasons := array_append(v_reasons,'The selected live connector has not passed its current security, no-withdrawal, no-transfer and reconciliation checks.');
    END IF;
  END IF;

  SELECT * INTO v_balance
  FROM public.n8_live_balance_checks b
  WHERE b.user_id=p_user_id
    AND b.account_type=p_account_type
    AND b.connection_id=p_connection_id
    AND b.observed_at>now()-interval '60 seconds'
  ORDER BY b.observed_at DESC LIMIT 1;
  IF NOT FOUND THEN
    v_reasons := array_append(v_reasons,'A live available-balance check less than 60 seconds old is required.');
  ELSIF v_balance.available_quote_usd < v_min_balance THEN
    v_reasons := array_append(v_reasons,format('Available tradable quote balance is below the $%s minimum.',v_min_balance));
  END IF;

  v_eligible := coalesce(array_length(v_reasons,1),0)=0;
  RETURN jsonb_build_object(
    'eligible',v_eligible,
    'status',CASE WHEN v_eligible THEN 'eligible_pending_owner_confirmation' ELSE 'ineligible' END,
    'reasons',to_jsonb(v_reasons),
    'paper',jsonb_build_object(
      'sessionId',v_session.id,'marketType',p_market_type,
      'startedAt',v_session.started_at,'elapsedDays',round(v_days,1),
      'requiredDays',p_window_days,'completedTrades',v_total_trades,
      'enabledStrategies',v_strategy_count,'strategiesPassing',v_strategy_pass_count,
      'unresolvedOrders',v_unresolved_orders,'pendingProposals',v_pending_proposals,
      'openPositions',v_open_positions
    ),
    'account',jsonb_build_object(
      'accountType',p_account_type,'provider',v_provider,
      'availableQuoteUsd',v_balance.available_quote_usd,
      'balanceObservedAt',v_balance.observed_at,'minimumBalanceUsd',v_min_balance
    ),
    'safety',jsonb_build_object(
      'withdrawalsDisabled',true,'agentTransfersDisabled',true,
      'approvalRequired',coalesce(v_policy.require_confirmation,true),
      'killSwitchActive',coalesce(v_policy.kill_switch,true),
      'liveTradingEnabled',false
    )
  );
END;
$function$;

REVOKE ALL ON FUNCTION public.n8_live_gate_assess(uuid,integer,text,uuid,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.n8_live_gate_assess(uuid,integer,text,uuid,text) TO service_role;

-- Attach the strategy version to the current session so its real closed virtual fills
-- can be measured without creating synthetic orders or journal rows.
