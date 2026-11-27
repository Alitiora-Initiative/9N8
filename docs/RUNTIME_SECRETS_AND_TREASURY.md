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
| `TELEGRAM_WEBHOOK_URL` (existing Secret) | Not read at runtime | Not read | The sync workflow validates this against the canonical `telegram-bot-next` URL and registers that URL with Telegram |
| `TELEGRAM_WEBHOOK_SECRET` | Runtime secret | Not used | Generated and masked inside each sync run, then stored in Supabase and sent to Telegram's `setWebhook`; it is not a GitHub Actions secret |

The deployed Edge Function reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `JUPITER_API_KEY` using `Deno.env.get(...)`. GitHub Secrets are not automatically inherited by Supabase or Vercel.

The Python config reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HELIUS_API_KEY`, `JUPITER_API_KEY`, `MASTER_TREASURY_ADDRESS`, and the separately provisioned database/encryption/worker settings. The current Vercel Python project already has names for `DATABASE_URL`, `ENCRYPTION_MASTER_KEY`, and `N9N8_WORKER_BRIDGE_SECRET`; the sync workflow intentionally does not replace those values.

## One-time Actions setup

Add these **two provider-access credentials** as repository Actions secrets (or organization secrets explicitly granted to this repository):

- `SUPABASE_ACCESS_TOKEN`: a Supabase personal access token with permission to set Edge Function secrets and deploy `telegram-bot-next` in project `qlfcazdfxexcnzdgpqqm`.
- `VERCEL_TOKEN`: a Vercel access token with permission to manage Production environment variables and create a Production deployment for `9n8-python-engine`.

The existing application secrets keep their exact names: `TELEGRAM_BOT_TOKEN`, `JUPITER_API_KEY`, `HELIUS_API_KEY`, `TELEGRAM_CHAT_ID`, and `TELEGRAM_WEBHOOK_URL`. The workflow checks that the webhook URL equals the canonical `telegram-bot-next` endpoint. `TWELVEDATA_API_KEY` is not currently consumed by these two production runtimes.

**Do not add `TELEGRAM_WEBHOOK_SECRET` manually.** The workflow creates a fresh 256-bit secret only after both provider preflight checks pass, masks it in the Actions log, writes it to a temporary runner environment, syncs it to Supabase, and registers it with Telegram in the same run. It is not printed, committed, or retained as a repository variable.

After the Linux treasury wallet is generated, add the printed public address as an **Actions Variable** named `MASTER_TREASURY_ADDRESS`. Never add the private key file as an Actions secret.

Provider access tokens are account-level control credentials. Create them in their respective provider dashboards, grant only the required project/team access, and add them directly to GitHub Actions. Do not paste the values into this chat.

## Run the sync

Open GitHub → Actions → **9N8 runtime secret sync** → Run workflow. It is intentionally manual; do not run it repeatedly to test credentials, because each successful run rotates the Telegram webhook secret and creates one Vercel Production deployment.

The workflow performs these checks and actions in order:

1. Checks the existing application secrets, the two provider access tokens, the positive numeric Telegram chat/user ID, and the canonical webhook URL. It validates the optional treasury address as a 32-byte Solana public key.
2. Performs a Jupiter Swap V2 quote-only request and a Telegram Bot API `getMe` preflight before changing any production secrets or webhook configuration.
3. Generates a fresh webhook secret, stores it only in the runner environment, masks it, and writes the exact Supabase runtime values to a temporary mode-0600 file.
4. Sets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `JUPITER_API_KEY` as Supabase Edge Function secrets, then deploys only `telegram-bot-next` with `--no-verify-jwt`, preserving its own Telegram-header and signed Mini App authentication.
5. Calls the function's safe GET health endpoint to confirm it sees the Telegram token and webhook secret without returning secret values.
6. Registers the canonical `telegram-bot-next` URL with Telegram's `setWebhook` and confirms it using `getWebhookInfo`.
7. Upserts `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `HELIUS_API_KEY`, and `JUPITER_API_KEY` into the Production environment of the actual Python backend project, not the frontend. It syncs the optional public treasury address when the Actions Variable exists.
8. Verifies Vercel variable names and Production targets without decrypting or printing values. It refuses to continue if `N9N8_TRADING_MODE` is not `paper` or `LIVE_TRADING_ENABLED` is not `false`.
9. Creates one fresh Vercel Production deployment of `9n8-python-engine` from the current `main` commit so the new environment is activated.

The workflow never enables Live mode, creates a wallet, signs a transaction, or submits a trade. The Jupiter validation request has no taker wallet and cannot create a signed swap transaction. The Vercel deployment uses the existing project's settings and GitHub connection.

## Deployment activation

Supabase Edge Function secrets are made available to Edge Functions as runtime environment variables. This workflow deploys `telegram-bot-next` after setting them and verifies the configured names before registering the webhook.

Vercel environment variable changes do not alter an already-built deployment. The workflow therefore creates one Production deployment of `9n8-python-engine` after syncing its Production environment. The deployment begins building immediately; check its Vercel deployment status and build logs before treating the Python runtime as ready.

## Treasury wallet security

The Rust utility at `services/solana-core/src/bin/treasury-keygen.rs` generates a standard Solana Ed25519 keypair on a local Linux computer. It writes the Solana CLI JSON keypair format to a new file with mode `0600`, refuses to overwrite an existing file, and prints only the public address.

The treasury private key is not needed by the bot to receive fee transfers. Store it offline with a tested backup. The runtime receives only the public `MASTER_TREASURY_ADDRESS`.

Do not use the treasury keypair as a user trading wallet. Do not generate it in CI, on Vercel, in Supabase, or in a hosted worker. Do not share the private file, 64-byte array, or recovery phrase in chat or Telegram.
