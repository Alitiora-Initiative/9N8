# Linux treasury keypair generator

This utility creates a **standard Solana Ed25519 wallet** on the machine where you run it. It is not a smart contract, PDA, or hosted key service.

## Requirements

- Linux
- Rust stable toolchain (rustup, cargo)
- Network access for the first Cargo dependency build

## Generate the treasury wallet offline from the bot

From the repository root:

```bash
install -d -m 700 "$HOME/.local/share/9n8/treasury"
cargo run --release --manifest-path services/solana-core/Cargo.toml --bin treasury-keygen -- \
  "$HOME/.local/share/9n8/treasury/treasury-keypair.json"
```

The command prints only the public MASTER_TREASURY_ADDRESS. It writes the 64-byte Solana keypair in the Solana CLI JSON-array format to a new file with mode 0600 (owner read/write only). It refuses to overwrite an existing file.

Check the file permissions:

```bash
stat -c '%a %n' "$HOME/.local/share/9n8/treasury/treasury-keypair.json"
```

Expected permissions: 600.

## Connect the public address

1. Add the printed public address as a **GitHub Actions variable** named MASTER_TREASURY_ADDRESS (not as a private-key secret).
2. Run the 9N8 runtime secret sync workflow. It copies that public address into the Production environment of 9n8-python-engine.
3. Keep the JSON keypair file encrypted/offline with a tested backup. The service only needs the public recipient address to receive fee transfers; it must never receive the treasury private key.

Never paste the JSON keypair, its 64-byte array, or a seed phrase into Telegram, GitHub, Vercel, Supabase, a CI log, a screenshot, or this chat. Do not run this generator in GitHub Actions or on a hosted server.
