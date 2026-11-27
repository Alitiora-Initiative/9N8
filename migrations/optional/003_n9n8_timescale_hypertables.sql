-- Optional TimescaleDB enhancement only.
-- Do not run against standard Neon/PostgreSQL unless TimescaleDB is installed.
-- The core 9N8 application works with ordinary PostgreSQL tables without it.

BEGIN;
CREATE EXTENSION IF NOT EXISTS timescaledb;
DO $$
BEGIN
    IF to_regclass('public.n9n8_market_candles') IS NULL THEN
        RAISE EXCEPTION 'public.n9n8_market_candles is missing; apply 001_n9n8_postgres.sql first';
    END IF;
END $$;
SELECT create_hypertable(
    'public.n9n8_market_candles',
    'open_time',
    if_not_exists => TRUE,
    migrate_data => TRUE
);
CREATE INDEX IF NOT EXISTS n9n8_market_candles_time_symbol_idx
    ON public.n9n8_market_candles(open_time DESC, instrument_id);
COMMIT;
