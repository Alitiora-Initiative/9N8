-- Defense in depth: internal service-role tables explicitly deny API roles.
-- Edge Functions continue to access these tables through their service-role key.
DO $block$
DECLARE
  r record;
  v_policy_name text;
BEGIN
  FOR r IN
    SELECT *
    FROM (VALUES
      ('nexus','approvals'),
      ('nexus','audit_events'),
      ('nexus','connector_accounts'),
      ('nexus','fills'),
      ('nexus','job_outbox'),
      ('nexus','market_observations'),
      ('nexus','order_intents'),
      ('nexus','orders'),
      ('nexus','risk_profiles'),
      ('nexus','strategy_versions'),
      ('nexus','tenant_memberships'),
      ('nexus','tenants'),
      ('nexus','trade_proposals'),
      ('nexus','worker_leases'),
      ('public','n8_paper_asset_state'),
      ('public','n8_paper_positions'),
      ('public','n8_paper_ticks'),
      ('public','n8_paper_trades')
    ) AS t(schema_name, table_name)
  LOOP
    IF to_regclass(format('%I.%I',r.schema_name,r.table_name)) IS NULL THEN
      RAISE EXCEPTION 'expected internal table %.% is missing',r.schema_name,r.table_name;
    END IF;
    v_policy_name := 'deny_anon_authenticated';
    IF NOT EXISTS (
      SELECT 1
      FROM pg_policies
      WHERE schemaname=r.schema_name
        AND tablename=r.table_name
        AND policyname=v_policy_name
    ) THEN
      EXECUTE format(
        'CREATE POLICY %I ON %I.%I FOR ALL TO anon, authenticated USING (false) WITH CHECK (false)',
        v_policy_name,r.schema_name,r.table_name
      );
    END IF;
  END LOOP;
END;
$block$;
