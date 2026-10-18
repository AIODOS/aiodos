//! Profil public d'un nœud (phase 3.0) : ce qu'il dit de lui au réseau public.
//!
//! Rien qui désigne la personne ou la machine : sa clé publique (son seul nom : des clés,
//! pas de pseudonymes, décision de l'auteur pour l'anonymat), la version d'AIODOS, les
//! modèles qu'il sait calculer, et s'il prête en ce moment. Le profil est signé par la
//! clé publique du nœud : le groupe de discussion relaie les messages de proche en
//! proche, et la machine qui en remet un n'en est pas l'auteur. Un profil trop vieux, ou
//! daté du futur, est refusé (un ancien message rejoué ne fait pas revivre un nœud).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use bytes::Bytes;
use iroh::{EndpointId, SecretKey, Signature};
use serde::{Deserialize, Serialize};

/// Taille maximale d'un message du groupe de discussion public.
pub const MAX_MESSAGE: usize = 4096;
const MAX_MODELS: usize = 24;
const MAX_TEXT: usize = 64;
/// Âge maximal d'un profil reçu, et avance tolérée (horloges décalées).
const MAX_AGE: Duration = Duration::from_secs(10 * 60);
const MAX_AHEAD: Duration = Duration::from_secs(2 * 60);

/// Ce qu'AIODOS dit de ce nœud (cf. `PUT /net/v1/public`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Profile {
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub models: Vec<String>,
    #[serde(default)]
    pub lends: bool,
}

impl Profile {
    /// Bornes : au plus 24 modèles, des textes courts et imprimables.
    pub fn clean(mut self) -> Self {
        let tidy = |text: &str| -> String {
            text.chars().filter(|c| !c.is_control()).take(MAX_TEXT).collect::<String>().trim().to_string()
        };
        self.version = tidy(&self.version);
        let mut models: Vec<String> = Vec::new();
        for model in &self.models {
            let model = tidy(model);
            if !model.is_empty() && !models.contains(&model) && models.len() < MAX_MODELS {
                models.push(model);
            }
        }
        self.models = models;
        self
    }
}

/// Le corps signé : la version du format, la clé de l'auteur, la date, le profil.
#[derive(Serialize, Deserialize)]
struct Body {
    v: u8,
    key: String,
    ts: u64,
    #[serde(flatten)]
    profile: Profile,
}

/// Sur le fil : le corps tel que signé (texte JSON), et la signature en hexadécimal.
#[derive(Serialize, Deserialize)]
struct Wire {
    p: String,
    s: String,
}

fn seconds(now: SystemTime) -> u64 {
    now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs()
}

/// Le message à diffuser pour ce profil, signé par la clé publique du nœud.
pub fn seal(secret: &SecretKey, profile: &Profile, now: SystemTime) -> Result<Bytes> {
    let body = Body {
        v: 1,
        key: secret.public().to_string(),
        ts: seconds(now),
        profile: profile.clone().clean(),
    };
    let text = serde_json::to_string(&body)?;
    let signature = secret.sign(text.as_bytes());
    let wire = serde_json::to_vec(&Wire { p: text, s: hex(&signature.to_bytes()) })?;
    if wire.len() > MAX_MESSAGE {
        bail!("profil public trop long");
    }
    Ok(wire.into())
}

/// Un profil reçu : sa signature, sa date et ses bornes vérifiées. → (auteur, date, profil).
pub fn open(content: &[u8], now: SystemTime) -> Result<(EndpointId, u64, Profile)> {
    if content.len() > MAX_MESSAGE {
        bail!("message trop long");
    }
    let wire: Wire = serde_json::from_slice(content).context("message illisible")?;
    let body: Body = serde_json::from_str(&wire.p).context("profil illisible")?;
    if body.v != 1 {
        bail!("format de profil inconnu ({})", body.v);
    }
    let author: EndpointId = body.key.parse().context("clé illisible")?;
    let raw: [u8; 64] = unhex(&wire.s)
        .and_then(|bytes| bytes.try_into().ok())
        .context("signature illisible")?;
    author
        .verify(wire.p.as_bytes(), &Signature::from_bytes(&raw))
        .map_err(|_| anyhow::anyhow!("signature fausse"))?;
    let now = seconds(now);
    if body.ts > now + MAX_AHEAD.as_secs() || body.ts + MAX_AGE.as_secs() < now {
        bail!("profil périmé ou daté du futur");
    }
    let profile = body.profile.clone().clean();
    Ok((author, body.ts, profile))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(text: &str) -> Option<Vec<u8>> {
    if !text.len().is_multiple_of(2) || !text.is_ascii() {
        return None;
    }
    (0..text.len()).step_by(2).map(|i| u8::from_str_radix(&text[i..i + 2], 16).ok()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> Profile {
        Profile { version: "2.1.0".into(), models: vec!["embeddinggemma".into()], lends: true }
    }

    #[test]
    fn a_sealed_profile_opens_with_its_author() {
        let secret = SecretKey::generate();
        let now = SystemTime::now();
        let message = seal(&secret, &profile(), now).unwrap();
        let (author, ts, got) = open(&message, now).unwrap();
        assert_eq!(author, secret.public());
        assert_eq!(ts, seconds(now));
        assert_eq!(got, profile());
    }

    #[test]
    fn a_forged_or_replayed_profile_is_refused() {
        let secret = SecretKey::generate();
        let now = SystemTime::now();
        let message = seal(&secret, &profile(), now).unwrap();
        // Un autre auteur, le même profil : la signature ne correspond plus
        let mut wire: Wire = serde_json::from_slice(&message).unwrap();
        let other = SecretKey::generate().public().to_string();
        wire.p = wire.p.replace(&secret.public().to_string(), &other);
        assert!(open(&serde_json::to_vec(&wire).unwrap(), now).is_err());
        // Un vieux message rejoué
        assert!(open(&message, now + Duration::from_secs(3600)).is_err());
        // Daté du futur
        assert!(open(&message, now - Duration::from_secs(600)).is_err());
        assert!(open(b"{}", now).is_err());
    }

    #[test]
    fn a_profile_is_bounded() {
        let long = Profile {
            version: "x".repeat(500),
            models: (0..100).map(|i| format!("modèle-{i}\n")).collect(),
            lends: false,
        }
        .clean();
        assert_eq!(long.version.len(), MAX_TEXT);
        assert_eq!(long.models.len(), MAX_MODELS);
        assert!(long.models.iter().all(|m| !m.contains('\n')));
        let secret = SecretKey::generate();
        assert!(seal(&secret, &long, SystemTime::now()).unwrap().len() <= MAX_MESSAGE);
    }
}
