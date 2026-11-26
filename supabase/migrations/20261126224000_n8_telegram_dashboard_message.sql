ALTER TABLE public.n8_users
  ADD COLUMN IF NOT EXISTS telegram_dashboard_message_id BIGINT;

COMMENT ON COLUMN public.n8_users.telegram_dashboard_message_id IS
  'Telegram private-chat message ID for the persistent 9N8 control panel.';
