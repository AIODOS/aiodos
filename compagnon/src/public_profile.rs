//! Profil public d'un nœud (phase 3.0) : ce qu'il dit de lui au réseau public.
//!
//! Rien qui désigne la personne ou la machine : sa clé publique (son seul nom : des clés,
//! pas de pseudonymes, décision de l'auteur pour l'anonymat), la version d'AIODOS, les
//! modèles qu'il sait calculer, et s'il prête en ce moment. Le profil est signé par la
//! clé publique du nœud : le groupe de discussion relaie les messages de proche en
//! proche, et la machine qui en remet un n'en est pas l'auteur. Un profil trop vieux, ou
//! daté du futur, est refusé (un ancien message rejoué ne fait pas revivre un nœud).
//!
//! Le 28/10/2026 (réseau public qui calcule, docs/conception/reseau-public-calcul.md § 3) :
//! deux champs **facultatifs**, `free` (places de conversation libres) et `tps` (vitesse
//! mesurée, en jetons par seconde), pour que le demandeur choisisse une machine. Absents,
//! ils ne sont pas écrits : un nœud d'avant les ignore (champs inconnus, le profil reste lu
//! et sa signature porte sur le texte tel quel), et un profil d'avant se lit encore (`None`).
//! Le profil signé tient toujours dans un message (4 Kio) : au besoin, les derniers modèles
//! de la liste sont laissés de côté.
//!
//! Le 29/10/2026 (relecture de sécurité du 28/10) : la version et les noms de modèles perdent
//! aussi les caractères invisibles et de mise en forme (`invisible` : sens d'écriture,
//! largeur nulle, sélecteurs de variante…), à l'envoi comme à la réception. Ce qu'un profil
//! reçu devient ensuite (preuve de travail de sa clé, rejeu, place) : public_nodes.rs.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use bytes::Bytes;
use iroh::{EndpointId, SecretKey, Signature};
use serde::{Deserialize, Deserializer, Serialize};
use tracing::debug;

/// Taille maximale d'un message du groupe de discussion public.
pub const MAX_MESSAGE: usize = 4096;
const MAX_MODELS: usize = 24;
const MAX_TEXT: usize = 64;
/// Âge maximal d'un profil reçu, et avance tolérée (horloges décalées). Celui d'un auteur
/// que l'annuaire ne connaît pas encore est borné plus court (`public_nodes::NEW_MAX_AGE`).
pub const MAX_AGE: Duration = Duration::from_secs(10 * 60);
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
    /// Places de conversation libres en ce moment (0 à 255), si AIODOS les dit.
    #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "free_places")]
    pub free: Option<u8>,
    /// Vitesse mesurée, en jetons par seconde (0 à 65535), si AIODOS la dit.
    #[serde(default, skip_serializing_if = "Option::is_none", deserialize_with = "tokens_per_second")]
    pub tps: Option<u16>,
}

/// Un nombre JSON quelconque (entier ou non, négatif, trop grand) ramené dans `0..=max` et
/// arrondi : AIODOS peut donner une vitesse mesurée à virgule, et un nœud d'une autre
/// version un nombre hors bornes, sans que tout le profil soit refusé pour autant.
fn clamped<'de, D: Deserializer<'de>>(deserializer: D, max: f64) -> Result<Option<f64>, D::Error> {
    let value = Option::<f64>::deserialize(deserializer)?;
    Ok(value.filter(|v| v.is_finite()).map(|v| v.round().clamp(0.0, max)))
}

fn free_places<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<u8>, D::Error> {
    Ok(clamped(deserializer, f64::from(u8::MAX))?.map(|v| v as u8))
}

fn tokens_per_second<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Option<u16>, D::Error> {
    Ok(clamped(deserializer, f64::from(u16::MAX))?.map(|v| v as u16))
}

/// Caractère invisible ou de mise en forme : contrôle (Cc), mais aussi ce qui change le
/// sens d'écriture (U+202A–U+202E, U+2066–U+2069, U+200E/F, U+061C), les largeurs nulles
/// (U+200B–U+200D, U+2060, U+FEFF), les sélecteurs de variante, les étiquettes, et les
/// autres caractères de format (Cf) ou de remplissage invisibles. Un nom de modèle qui en
/// porte s'afficherait comme un autre (relecture du 28/10/2026 : « qwen3.5:9b » inversé ou
/// doublé d'un caractère invisible passait pour le vrai chez le demandeur).
pub fn invisible(c: char) -> bool {
    c.is_control()
        || matches!(
            c,
            '\u{00AD}'                       // trait d'union conditionnel
            | '\u{034F}'                     // liant de graphèmes
            | '\u{061C}'                     // marque de lettre arabe (direction)
            | '\u{115F}' | '\u{1160}' | '\u{3164}' | '\u{FFA0}' // remplissages hangûl
            | '\u{17B4}' | '\u{17B5}'        // voyelles khmères invisibles
            | '\u{180B}'..='\u{180F}'        // sélecteurs et séparateur mongols
            | '\u{200B}'..='\u{200F}'        // largeur nulle, liants, marques de direction
            | '\u{2028}'..='\u{202E}'        // séparateurs, enchâssements et forçages de direction
            | '\u{2060}'..='\u{206F}'        // gluon, opérateurs invisibles, isolats de direction
            | '\u{FE00}'..='\u{FE0F}'        // sélecteurs de variante
            | '\u{FEFF}'                     // indicateur d'ordre des octets, gluon
            | '\u{FFF9}'..='\u{FFFB}'        // annotations interlinéaires
            | '\u{110BD}' | '\u{110CD}'      // formats kaithi
            | '\u{13430}'..='\u{1343F}'      // formats hiéroglyphiques
            | '\u{1BCA0}'..='\u{1BCA3}'      // formats sténographiques
            | '\u{1D173}'..='\u{1D17A}'      // formats musicaux
            | '\u{E0000}'..='\u{E007F}'      // étiquettes
            | '\u{E0100}'..='\u{E01EF}'      // sélecteurs de variante, supplément
        )
}

/// Un texte du profil : sans caractère invisible (`invisible`), `MAX_TEXT` caractères au
/// plus, sans espace en tête ni en fin.
fn tidy(text: &str) -> String {
    text.chars().filter(|c| !invisible(*c)).take(MAX_TEXT).collect::<String>().trim().to_string()
}

impl Profile {
    /// Bornes : au plus 24 modèles, des textes courts, imprimables et visibles.
    pub fn clean(mut self) -> Self {
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

/// Le message à diffuser pour ce profil, signé par la clé publique du nœud. Des noms de
/// modèles longs, en caractères hors ASCII ou pleins de guillemets (échappés deux fois :
/// dans le corps, puis dans le message) dépasseraient les 4 Kio : les derniers modèles de
/// la liste sont alors laissés de côté, plutôt que de ne plus rien diffuser.
pub fn seal(secret: &SecretKey, profile: &Profile, now: SystemTime) -> Result<Bytes> {
    let mut profile = profile.clone().clean();
    loop {
        let body = Body { v: 1, key: secret.public().to_string(), ts: seconds(now), profile };
        let text = serde_json::to_string(&body)?;
        let signature = secret.sign(text.as_bytes());
        let wire = serde_json::to_vec(&Wire { p: text, s: hex(&signature.to_bytes()) })?;
        if wire.len() <= MAX_MESSAGE {
            return Ok(wire.into());
        }
        profile = body.profile;
        if profile.models.pop().is_none() {
            bail!("profil public trop long");
        }
        debug!("Profil public trop long : un modèle laissé de côté");
    }
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
        Profile {
            version: "2.1.0".into(),
            models: vec!["embeddinggemma".into()],
            lends: true,
            ..Profile::default()
        }
    }

    /// Le message tel qu'un nœud d'une version d'avant le 28/10 le signait.
    fn old_message(secret: &SecretKey, now: SystemTime) -> Vec<u8> {
        let text = serde_json::json!({
            "v": 1, "key": secret.public().to_string(), "ts": seconds(now),
            "version": "2.0.9", "models": ["qwen3.5:9b"], "lends": true,
        })
        .to_string();
        let signature = hex(&secret.sign(text.as_bytes()).to_bytes());
        serde_json::to_vec(&Wire { p: text, s: signature }).unwrap()
    }

    /// Profil tel qu'un nœud d'avant le 28/10 le lit.
    #[derive(Deserialize)]
    struct OldProfile {
        #[serde(default)]
        models: Vec<String>,
        #[serde(default)]
        lends: bool,
    }

    #[derive(Deserialize)]
    struct OldBody {
        v: u8,
        #[serde(flatten)]
        profile: OldProfile,
    }

    #[test]
    fn free_places_and_speed_travel_signed_with_the_rest() {
        let secret = SecretKey::generate();
        let now = SystemTime::now();
        let busy = Profile { free: Some(2), tps: Some(41), ..profile() };
        let message = seal(&secret, &busy, now).unwrap();
        let (_, _, got) = open(&message, now).unwrap();
        assert_eq!((got.free, got.tps), (Some(2), Some(41)));
        // Changer la vitesse annoncée sans la clé : la signature ne correspond plus
        let mut wire: Wire = serde_json::from_slice(&message).unwrap();
        wire.p = wire.p.replace("\"tps\":41", "\"tps\":4100");
        assert!(open(&serde_json::to_vec(&wire).unwrap(), now).is_err());
    }

    #[test]
    fn old_and_new_profiles_still_read_each_other() {
        let secret = SecretKey::generate();
        let now = SystemTime::now();
        // Un profil d'avant (sans `free` ni `tps`) se lit encore : rien n'est inventé
        let (author, _, got) = open(&old_message(&secret, now), now).unwrap();
        assert_eq!(author, secret.public());
        assert_eq!((got.free, got.tps), (None, None));
        assert_eq!(got.models, vec!["qwen3.5:9b".to_string()]);
        // Un nœud d'avant lit un profil d'aujourd'hui en ignorant les nouveaux champs
        let message = seal(&secret, &Profile { free: Some(1), tps: Some(30), ..profile() }, now).unwrap();
        let wire: Wire = serde_json::from_slice(&message).unwrap();
        let old: OldBody = serde_json::from_str(&wire.p).unwrap();
        assert_eq!(old.v, 1);
        assert!(old.profile.lends);
        assert_eq!(old.profile.models, vec!["embeddinggemma".to_string()]);
        // Sans ces champs, le profil d'aujourd'hui s'écrit comme celui d'avant
        assert!(!serde_json::to_string(&profile()).unwrap().contains("free"));
    }

    #[test]
    fn free_places_and_speed_are_clamped_not_refused() {
        let read = |text: &str| serde_json::from_str::<Profile>(text).unwrap();
        let got = read(r#"{"free": 300, "tps": 12.6}"#);
        assert_eq!((got.free, got.tps), (Some(255), Some(13)));
        let got = read(r#"{"free": -3, "tps": 1e9}"#);
        assert_eq!((got.free, got.tps), (Some(0), Some(u16::MAX)));
        let got = read(r#"{"free": null}"#);
        assert_eq!((got.free, got.tps), (None, None));
        assert!(serde_json::from_str::<Profile>(r#"{"free": "beaucoup"}"#).is_err());
        // Tel qu'AIODOS l'envoie (`PUT /net/v1/public`), à côté de `enabled`
        let wanted: crate::public::Wanted =
            serde_json::from_str(r#"{"enabled": true, "lends": true, "free": 1, "tps": 25}"#).unwrap();
        assert_eq!((wanted.profile.free, wanted.profile.tps), (Some(1), Some(25)));
    }

    #[test]
    fn a_profile_full_of_long_model_names_still_fits_in_a_message() {
        // 24 noms de 64 guillemets : échappés deux fois, bien plus de 4 Kio
        let full = Profile {
            models: (0..MAX_MODELS).map(|i| format!("{i:02}{}", "\"".repeat(62))).collect(),
            free: Some(3),
            tps: Some(65535),
            ..profile()
        };
        let secret = SecretKey::generate();
        let now = SystemTime::now();
        let message = seal(&secret, &full, now).unwrap();
        assert!(message.len() <= MAX_MESSAGE);
        let (_, _, got) = open(&message, now).unwrap();
        assert!(!got.models.is_empty() && got.models.len() < MAX_MODELS, "{}", got.models.len());
        assert_eq!(got.models[0], full.models[0], "les premiers modèles restent");
        assert_eq!((got.free, got.tps), (Some(3), Some(65535)));
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
            ..Profile::default()
        }
        .clean();
        assert_eq!(long.version.len(), MAX_TEXT);
        assert_eq!(long.models.len(), MAX_MODELS);
        assert!(long.models.iter().all(|m| !m.contains('\n')));
        let secret = SecretKey::generate();
        assert!(seal(&secret, &long, SystemTime::now()).unwrap().len() <= MAX_MESSAGE);
    }

    #[test]
    fn model_names_lose_their_direction_and_invisible_characters() {
        // Relecture du 28/10 : ces caractères passaient (seuls les contrôles étaient retirés)
        let tricky = [
            "\u{202E}b9:5.3newq",            // forçage de droite à gauche
            "qwen3.5\u{200B}:9b",            // espace de largeur nulle
            "\u{2066}qwen3.5:9b\u{2069}",    // isolat de gauche à droite
            "qwen3.5:9b\u{200E}\u{200F}",    // marques de direction
            "qwen\u{2060}3.5:9b\u{FEFF}",    // gluons
            "qwen3.5:9b\u{E0041}\u{FE0F}",   // étiquette, sélecteur de variante
            "\u{061C}qwen3.5\u{00AD}:9b",    // marque arabe, trait d'union conditionnel
        ];
        let got = Profile { models: tricky.iter().map(|s| s.to_string()).collect(), ..profile() }.clean();
        for model in &got.models {
            assert!(!model.chars().any(invisible), "{model:?}");
        }
        assert_eq!(got.models[1], "qwen3.5:9b", "devenu le vrai nom, et non un sosie");
        // Doublons une fois nettoyés : un seul gardé ; un nom fait d'invisibles disparaît
        assert_eq!(got.models.iter().filter(|m| *m == "qwen3.5:9b").count(), 1);
        assert_eq!(got.models.len(), 2, "{:?}", got.models);
        let blank = Profile { models: vec!["\u{200B}\u{202E}".into()], ..profile() }.clean();
        assert!(blank.models.is_empty());
        // Le reste passe : accents, idéogrammes
        let kept = Profile { models: vec!["modèle-日本語:7b".into()], ..profile() }.clean();
        assert_eq!(kept.models, vec!["modèle-日本語:7b".to_string()]);
        // Reçu d'un autre nœud : nettoyé à l'ouverture
        let secret = SecretKey::generate();
        let now = SystemTime::now();
        let text = serde_json::json!({
            "v": 1, "key": secret.public().to_string(), "ts": seconds(now),
            "version": "2.1.0\u{202E}", "models": ["qwen3.5\u{200D}:9b"], "lends": true,
        })
        .to_string();
        let signature = hex(&secret.sign(text.as_bytes()).to_bytes());
        let message = serde_json::to_vec(&Wire { p: text, s: signature }).unwrap();
        let (_, _, opened) = open(&message, now).unwrap();
        assert_eq!((opened.version.as_str(), opened.models[0].as_str()), ("2.1.0", "qwen3.5:9b"));
    }
}
