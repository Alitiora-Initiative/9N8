# 9N8 runtime credential routing and treasury setup

This document defines the production credential contract for the 9N8 deployment.

## Production services

| Service | Responsibility | Receives backend secrets? |
| --- | --- | --- |
| Supabase `telegram-bot-next` | Owns Telegram updates, commands, callbacks, and Jupiter Swap V2 calls | Yes |
| Vercel `9n8-python-engine` | FastAPI API and Python worker/daemon runtime | Yes |
| Vercel `9n8-secure-connect` | Static Mini App frontend | No |
| Vercel `9n8-trading-agent` | Dashboard project | Only its own dashboard/session configuration |

Do not put Telegram, Jupiter, Helius, Supabase service, database, or wallet private-key values in the frontend project.

## Exact credential mapping

| Existing GitHub Actions secret / variable | Supabase `telegram-bot-next` | Vercel `9n8-python-engine` | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` (Secret) | Secret | Secret | Exact name consumed by both runtimes |
| `JUPITER_API_KEY` (Secret) | Secret | Secret | Exact name consumed by both runtimes |
| `HELIUS_API_KEY` (Secret) | Not read by this Edge Function | Secret | Python Helius RPC/WebSocket credentials |
| `TELEGRAM_CHAT_ID` (Secret) | Not required by the multi-user webhook | Secret | Python config accepts this exact name as its positive numeric authorized-user fallback |
| `MASTER_TREASURY_ADDRESS` (Actions Variable) | Not used | Config | Public address only; populated after local key generation |
| `TELEGRAM_WEBHOOK_URL` (existing Secret) | Not read | Not read | The sync workflow uses the canonical `telegram-bot-next` URL below |
| `TELEGRAM_WEBHOOK_SECRET` | Generated during sync and stored in Supabase | Not used | A fresh random secret is generated, then sent to Telegram's `setWebhook` in the same run |

The deployed Edge Function reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `JUPITER_API_KEY` using `Deno.env.get(...)`. GitHub Secrets are not automatically inherited by Supabase or Vercel.

The Python config reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HELIUS_API_KEY`, `JUPITER_API_KEY`, `MASTER_TREASURY_ADDRESS`, and the separately provisioned database/encryption/worker settings. The current Vercel Python project already has names for `DATABASE_URL`, `ENCRYPTION_MASTER_KEY`, and `N9N8_WORKER_BRIDGE_SECRET`; the sync workflow intentionally does not replace those values.

## One-time Actions setup

Add these **two additional repository Actions secrets**:

- `SUPABASE_ACCESS_TOKEN`: a Supabase personal access token with permission to set Edge Function secrets for project `qlfcazdfxexcnzdgpqqm`.
- `VERCEL_TOKEN`: a Vercel access token with permission to manage environment variables for project `9n8-python-engine`.

These are provider access tokens for the sync workflow. They are not substitutes for the application credentials already listed in the repository.

After creating the Linux treasury wallet, add the printed public address as an **Actions Variable** named `MASTER_TREASURY_ADDRESS`. Never add the private key file as an Actions secret.

## Run the sync

Open GitHub → Actions → **9N8 runtime secret sync** → Run workflow.

The workflow:

1. Checks that the required existing Actions secrets and positive Telegram chat/user ID are present.
2. Generates a fresh Telegram webhook secret in the ephemeral runner and masks it from logs.
3. Writes only the exact Supabase Edge Function secrets to a temporary mode-0600 file and runs `supabase secrets set`.
4. Registers the webhook at `https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next` using the generated secret, then verifies the URL with Telegram's `getWebhookInfo`.
5. Upserts `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HELIUS_API_KEY`, and `JUPITER_API_KEY` into the Production environment of the actual Python backend project, not the frontend.
6. Syncs the optional public treasury address when the Actions Variable exists.
7. Verifies the Vercel variable names and Production targets without decrypting or printing their values.

The workflow will stop if Vercel Production has drifted from the safety defaults `N9N8_TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`. It never enables Live mode or submits a trade.

## Deployment activation

Supabase Edge Function secrets are available to deployed Edge Functions as runtime environment variables without a function redeployment.

Vercel environment variable changes do **not** change an already-built deployment. They become available after the next successful deployment of `9n8-python-engine`. The sync workflow deliberately does not create a deployment, so a daily Vercel deployment quota limit cannot cause an additional failed deployment attempt. After the environment sync succeeds, deploy the backend when the Vercel quota permits.

## Treasury wallet security

The Rust utility at `services/solana-core/src/bin/treasury-keygen.rs` generates a standard Solana Ed25519 keypair on a local Linux computer. It writes the Solana CLI JSON keypair format to a new file with mode `0600`, refuses to overwrite an existing file, and prints only the public address.

The treasury private key is not needed by the bot to receive fee transfers. Store it offline with a tested backup. The runtime receives only the public `MASTER_TREASURY_ADDRESS`.

Do not use the treasury keypair as a user trading wallet. Do not generate it in CI, on Vercel, in Supabase, or in a hosted worker. Do not share the private file, 64-byte array, or recovery phrase in chat or Telegram.
