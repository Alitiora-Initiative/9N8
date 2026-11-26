-- Keep the production exchange registry aligned with telegram-bot-next.
-- This adds Bybit as an API-key provider only; it does not enable auto-trading,
-- withdrawals, internal transfers, or live account execution.
BEGIN;

ALTER TABLE public.n8_exchange_connections
  DROP CONSTRAINT IF EXISTS n8_exchange_connections_provider_check;

ALTER TABLE public.n8_exchange_connections
  ADD CONSTRAINT n8_exchange_connections_provider_check
  CHECK (provider = ANY (ARRAY['binance'::text, 'bybit'::text, 'okx'::text]));

CREATE OR REPLACE FUNCTION public.n8_store_exchange_secret(
  p_telegram_user_id bigint,
  p_provider text,
  p_environment text,
  p_label text,
  p_credentials jsonb,
  p_permissions jsonb DEFAULT '{}'::jsonb,
  p_account_hint text DEFAULT NULL::text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $function$
DECLARE
  v_user_id uuid;
  v_connection_id uuid := gen_random_uuid();
  v_secret_id uuid;
  v_role text;
  v_name text := 'n8-exchange-' || v_connection_id::text;
  v_note text;
BEGIN
  v_role := coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'),
    ''
  );
  IF v_role <> 'service_role' THEN
    RAISE EXCEPTION 'not_allowed';
  END IF;

  IF p_provider NOT IN ('binance', 'bybit', 'okx') THEN
    RAISE EXCEPTION 'unsupported_provider';
  END IF;
  IF p_environment <> 'live' THEN
    RAISE EXCEPTION 'unsupported_environment';
  END IF;
  IF p_credentials IS NULL OR p_credentials = '{}'::jsonb THEN
    RAISE EXCEPTION 'empty_credentials';
  END IF;
  IF nullif(p_credentials ->> 'apiKey', '') IS NULL
     OR nullif(p_credentials ->> 'apiSecret', '') IS NULL THEN
    RAISE EXCEPTION 'api_key_and_secret_required';
  END IF;
  IF p_provider = 'okx'
     AND nullif(p_credentials ->> 'passphrase', '') IS NULL THEN
    RAISE EXCEPTION 'okx_passphrase_required';
  END IF;

  SELECT u.id INTO v_user_id
  FROM public.n8_users u
  WHERE u.telegram_user_id = p_telegram_user_id
    AND u.status = 'active'
  FOR UPDATE;

  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'user_not_registered';
  END IF;

  v_note := CASE p_provider
    WHEN 'binance' THEN
      '9N8 per-user Binance LIVE credentials stored in Vault; no withdrawals or transfers are used'
    WHEN 'bybit' THEN
      '9N8 per-user Bybit LIVE credentials stored in Vault; wallet transfer and withdrawal scopes are rejected'
    ELSE
      '9N8 per-user OKX LIVE API credentials and passphrase stored in Vault; no withdrawal or transfer endpoints are used'
  END;

  v_secret_id := vault.create_secret(p_credentials::text, v_name, v_note);

  INSERT INTO public.n8_exchange_connections(
    id, user_id, provider, environment, label, account_hint,
    vault_secret_id, permissions, status, last_validated_at
  ) VALUES (
    v_connection_id, v_user_id, p_provider, 'live', p_label, p_account_hint,
    v_secret_id, coalesce(p_permissions, '{}'::jsonb), 'active', now()
  );

  RETURN v_connection_id;
END;
$function$;

COMMIT;
