use solana_keypair::Keypair;
use solana_signer::Signer;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;

/// Generate a standard Solana Ed25519 keypair locally.
///
/// The private key is written only to a new file with owner-only permissions.
/// The function returns the public address and never returns or prints the
/// private key. Existing files are never overwritten.
fn generate_to_path(path: &Path) -> io::Result<String> {
    let keypair = Keypair::new();
    let public_address = keypair.pubkey().to_string();
    let bytes = keypair.to_bytes();

    // Solana CLI-compatible JSON byte-array format: 32-byte secret + 32-byte public.
    let json = format!(
        "[{}]\n",
        bytes
            .iter()
            .map(u8::to_string)
            .collect::<Vec<_>>()
            .join(",")
    );

    let mut options = OpenOptions::new();
    options.write(true).create_new(true);

    #[cfg(unix)]
    options.mode(0o600);

    #[cfg(not(unix))]
    {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "treasury key generation is supported only on Unix/Linux",
        ));
    }

    let mut file = options.open(path)?;
    if let Err(error) = file.write_all(json.as_bytes()).and_then(|_| file.sync_all()) {
        drop(file);
        let _ = fs::remove_file(path);
        return Err(error);
    }

    Ok(public_address)
}

fn main() -> ExitCode {
    let mut args = env::args_os().skip(1);
    let output = args
        .next()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("treasury-keypair.json"));

    if args.next().is_some() {
        eprintln!("Usage: treasury-keygen [output-keypair.json]");
        return ExitCode::from(2);
    }

    match generate_to_path(&output) {
        Ok(address) => {
            println!("MASTER_TREASURY_ADDRESS={address}");
            println!("Private key file: {}", output.display());
            println!("File permissions: owner read/write only (0600)");
            println!("The private key was not printed. Back it up offline; never commit or upload it.");
            ExitCode::SUCCESS
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            eprintln!(
                "Refusing to overwrite existing key file: {}",
                output.display()
            );
            eprintln!("Choose a new path or securely back up the existing key first.");
            ExitCode::from(1)
        }
        Err(error) => {
            eprintln!("Could not create treasury keypair: {error}");
            ExitCode::from(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_path(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock before Unix epoch")
            .as_nanos();
        env::temp_dir().join(format!("9n8-{label}-{}-{nonce}.json", std::process::id()))
    }

    #[test]
    fn writes_solana_compatible_keypair_with_private_permissions() {
        let path = temp_path("treasury");
        let address = generate_to_path(&path).expect("keypair generation should succeed");

        assert!((32..=44).contains(&address.len()));
        let raw = fs::read_to_string(&path).expect("keypair file should be readable");
        let inner = raw.trim().strip_prefix('[').unwrap().strip_suffix(']').unwrap();
        let secret_bytes: Vec<u8> = inner
            .split(',')
            .map(|part| part.trim().parse::<u8>().expect("keypair byte should be u8"))
            .collect();
        assert_eq!(secret_bytes.len(), 64);

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }

        fs::remove_file(path).expect("test keypair should be deleted");
    }

    #[test]
    fn refuses_to_overwrite_an_existing_file() {
        let path = temp_path("existing");
        fs::write(&path, "keep-this-file").unwrap();

        let error = generate_to_path(&path).expect_err("existing file must not be overwritten");
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(fs::read_to_string(&path).unwrap(), "keep-this-file");

        fs::remove_file(path).unwrap();
    }
}
