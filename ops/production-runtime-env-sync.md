# Production runtime environment synchronization

Run the GitHub Actions workflow **9N8 runtime secret sync** manually after the required provider tokens are available.

Required additional GitHub Actions secrets:
- `SUPABASE_ACCESS_TOKEN` — Supabase project administration for setting Edge Function secrets and deploying `telegram-bot-next`
- `VERCEL_TOKEN` — Vercel team access to update Production variables and create a Production deployment for `9n8-python-engine`

The existing application secrets keep their names, including `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_URL`, `HELIUS_API_KEY`, and `JUPITER_API_KEY`. The workflow validates the webhook URL against the canonical Supabase function endpoint. It generates the Telegram webhook signing secret inside the run; do not create a separate GitHub secret for it.

The optional public Solana treasury address is the GitHub Actions Variable `MASTER_TREASURY_ADDRESS`. Generate its keypair locally on Linux with the Rust tool under `services/solana-core`. The private key must stay offline and must never be committed, uploaded to CI, or placed in Vercel or Supabase.

The workflow preserves `N9N8_TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`, verifies Jupiter using a quote-only request, verifies Telegram before webhook rotation, syncs runtime variables, and creates one production deployment after Vercel environment synchronization.
