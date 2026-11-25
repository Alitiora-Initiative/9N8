-- Finalize a mature 9N8 market-data paper session only after the worker is idle
-- and all virtual positions are closed. This does not enable live trading.
CREATE OR REPLACE FUNCTION public.n8_finalize_mature_paper_session(p_user_id uuid, p_market_type text DEFAULT 'crypto')
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  v_role text;
  v_session public.n8_paper_sessions%rowtype;
  v_open_positions integer := 0;
BEGIN
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  IF v_role <> 'service_role' THEN RAISE EXCEPTION 'not_allowed'; END IF;
  IF p_market_type <> 'crypto' THEN RAISE EXCEPTION 'paper_market_not_supported_yet'; END IF;

  SELECT * INTO v_session
  FROM public.n8_paper_sessions
  WHERE user_id = p_user_id
    AND market_type = p_market_type
    AND evaluation_window_days = 30
    AND status IN ('running', 'completed')
  ORDER BY started_at DESC NULLS LAST
  LIMIT 1
  FOR UPDATE;

  IF NOT FOUND THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'paper_session_not_found');
  END IF;
  IF v_session.status = 'completed' THEN
    RETURN jsonb_build_object('ok', true, 'already_completed', true, 'session_id', v_session.id);
  END IF;
  IF v_session.started_at IS NULL OR v_session.started_at > now() - interval '30 days' THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'paper_window_not_complete',
      'started_at', v_session.started_at, 'required_days', 30);
  END IF;
  IF v_session.worker_lock_until IS NOT NULL AND v_session.worker_lock_until > now() THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'paper_worker_tick_in_progress');
  END IF;
  IF v_session.last_tick_at IS NULL
     OR v_session.last_tick_at < now() - interval '5 minutes'
     OR v_session.last_tick_error IS NOT NULL THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'paper_engine_not_healthy_or_recent');
  END IF;
  IF nullif(v_session.config->>'strategy_version_id','') IS NULL THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'paper_strategy_version_missing');
  END IF;

  SELECT count(*)::integer INTO v_open_positions
  FROM public.n8_paper_positions
  WHERE session_id = v_session.id
    AND user_id = p_user_id
    AND status = 'open';

  IF v_open_positions > 0 THEN
    RETURN jsonb_build_object('ok', false, 'reason', 'paper_positions_still_open',
      'open_positions', v_open_positions, 'session_id', v_session.id);
  END IF;

  UPDATE public.n8_paper_sessions
  SET status = 'completed',
      completed_at = now(),
      last_activity_at = now(),
      updated_at = now()
  WHERE id = v_session.id
  RETURNING * INTO v_session;

  RETURN jsonb_build_object(
    'ok', true,
    'session_id', v_session.id,
    'status', v_session.status,
    'completed_at', v_session.completed_at,
    'strategy_version_id', v_session.config->>'strategy_version_id'
  );
END;
$function$;

REVOKE ALL ON FUNCTION public.n8_finalize_mature_paper_session(uuid,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.n8_finalize_mature_paper_session(uuid,text) TO service_role;
