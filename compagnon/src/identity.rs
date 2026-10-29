//! Identité de la machine sur Internet : une clé Ed25519 gardée dans le dossier de
//! données d'AIODOS.
//!
//! Sa moitié publique (l'EndpointId d'Iroh) est l'adresse de la machine : les autres
//! machines composent cette clé, pas une adresse IP. La clé secrète ne quitte jamais son
//! fichier : jamais dans les journaux, jamais dans l'API (le type `SecretKey` d'Iroh
//! s'affiche lui-même « SecretKey(..) »). Une clé illisible arrête le compagnon plutôt
//! que d'en créer une autre en silence : les machines appairées ne reconnaîtraient plus
//! celle-ci.

use std::{
    fs,
    io::{ErrorKind, Write},
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use iroh::SecretKey;

/// Nom du fichier de la clé, dans le dossier de données d'AIODOS.
pub const KEY_FILE: &str = "aiodos-net.key";

/// Clé de la machine, et si elle vient d'être créée.
pub fn load_or_create(data_dir: &Path) -> Result<(SecretKey, bool)> {
    load_or_create_file(data_dir, KEY_FILE)
}

/// Une clé gardée dans ce fichier du dossier de données (celle de la machine, ou celle du
/// nœud public, cf. public.rs), et si elle vient d'être créée.
pub fn load_or_create_file(data_dir: &Path, file: &str) -> Result<(SecretKey, bool)> {
    match read_file(data_dir, file)? {
        Some(key) => Ok((key, false)),
        None => {
            let key = SecretKey::generate();
            write_file(data_dir, file, &key)?;
            Ok((key, true))
        }
    }
}

/// La clé gardée dans ce fichier du dossier de données, `None` s'il n'existe pas ; une clé
/// illisible est une erreur (jamais remplacée en silence).
pub fn read_file(data_dir: &Path, file: &str) -> Result<Option<SecretKey>> {
    let path = data_dir.join(file);
    match fs::read_to_string(&path) {
        Ok(text) => {
            let key = parse(text.trim()).with_context(|| {
                format!(
                    "la clé de cette machine est illisible ({}) : la mettre de côté pour en \
                     créer une nouvelle (les machines appairées devront alors réapprendre \
                     la clé de celle-ci)",
                    path.display()
                )
            })?;
            Ok(Some(key))
        }
        Err(err) if err.kind() == ErrorKind::NotFound => Ok(None),
        Err(err) => Err(err).with_context(|| format!("lecture de {}", path.display())),
    }
}

/// Garde cette clé dans ce fichier du dossier de données (cf. `save`), à la place de
/// l'ancienne s'il y en avait une.
pub fn write_file(data_dir: &Path, file: &str, key: &SecretKey) -> Result<()> {
    save(&data_dir.join(file), key)
}

fn parse(text: &str) -> Result<SecretKey> {
    if text.len() != 64 || !text.is_ascii() {
        bail!("64 caractères hexadécimaux attendus");
    }
    let mut bytes = [0u8; 32];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[2 * i..2 * i + 2], 16).context("caractère non hexadécimal")?;
    }
    Ok(SecretKey::from_bytes(&bytes))
}

/// Écrit la clé de façon atomique (fichier temporaire puis renommage), lisible par
/// l'utilisateur seul sous Unix (Termux).
fn save(path: &Path, key: &SecretKey) -> Result<()> {
    let text: String = key.to_bytes().iter().map(|b| format!("{b:02x}")).collect();
    let tmp: PathBuf = path.with_extension("key.tmp");
    {
        let mut options = fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&tmp)
            .with_context(|| format!("création de {}", tmp.display()))?;
        file.write_all(text.as_bytes())?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, path).with_context(|| format!("écriture de {}", path.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_is_created_once_then_reused() {
        let dir = tempfile::tempdir().unwrap();
        let (first, created) = load_or_create(dir.path()).unwrap();
        assert!(created);
        let (again, created) = load_or_create(dir.path()).unwrap();
        assert!(!created);
        assert_eq!(first.public(), again.public());
        let text = fs::read_to_string(dir.path().join(KEY_FILE)).unwrap();
        assert_eq!(text.trim().len(), 64);
    }

    #[test]
    fn unreadable_key_is_not_replaced() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join(KEY_FILE), "abîmée").unwrap();
        assert!(load_or_create(dir.path()).is_err());
        assert_eq!(fs::read_to_string(dir.path().join(KEY_FILE)).unwrap(), "abîmée");
    }

    #[test]
    fn secret_never_shows_in_debug_output() {
        let dir = tempfile::tempdir().unwrap();
        let (key, _) = load_or_create(dir.path()).unwrap();
        let text = fs::read_to_string(dir.path().join(KEY_FILE)).unwrap();
        assert!(!format!("{key:?}").contains(text.trim()));
    }
}
