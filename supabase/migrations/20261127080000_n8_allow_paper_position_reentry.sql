-- Keep closed paper positions as immutable trade history while allowing a
-- later re-entry for the same instrument in the same paper session.
-- The old table-wide UNIQUE(session_id, instrument_id) incorrectly blocked
-- every re-entry after the first position had been closed.

ALTER TABLE public.n8_paper_positions
  DROP CONSTRAINT IF EXISTS n8_paper_positions_session_id_instrument_id_key;

-- A session may have many historical closed positions, but only one active
-- position for a given instrument at any time.
CREATE UNIQUE INDEX IF NOT EXISTS n8_paper_positions_one_open_per_instrument_idx
  ON public.n8_paper_positions (session_id, instrument_id)
  WHERE status = 'open';
