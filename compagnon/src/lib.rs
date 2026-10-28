//! aiodos-net : compagnon réseau d'AIODOS (étape 2.2 de docs/phase2-internet.md).
//!
//! Relie par Internet les machines appairées d'AIODOS, avec Iroh : chaque machine est
//! désignée par sa clé publique Ed25519 ; la connexion passe en direct quand les box le
//! permettent, sinon par un relais qui ne voit que du chiffré. Le nœud Python lance ce
//! programme, lui donne la liste des machines autorisées et lui confie ses requêtes :
//!
//! - **entrant** : chaque flux d'une machine autorisée est relayé vers le serveur réseau
//!   local d'AIODOS (`p2p_app`), avec la clé authentifiée de l'appelant ;
//! - **sortant** : un mandataire HTTP local (`/peer/<clé>/…`) garde une seule connexion
//!   QUIC par machine et y ouvre un flux par requête ;
//! - **contrôle** : état, liste blanche, ticket (`/net/v1/…`), sur 127.0.0.1 seulement ;
//! - **tunnels** : un service TCP local (serveur RPC de llama.cpp) ouvert à une machine
//!   nommée, joint par un port local de l'autre côté (cf. tunnel.rs) ; ni l'un ni l'autre
//!   ne sert les autres programmes de la machine (cf. owner.rs et hold.rs) ;
//! - **réseau public** : une autre clé et un autre point de terminaison, pour l'annuaire
//!   public (cf. public.rs) et les conversations que des inconnus confient à cette machine
//!   (cf. public_serve.rs) ; pour en confier aux leurs, une clé jetable par conversation
//!   (cf. public_ask.rs).
//!
//! Entre machines appairées, le contenu des requêtes reste scellé par AIODOS (enveloppes
//! ChaCha20) : le compagnon n'est qu'un tuyau, qui ne laisse entrer que ces machines. Le
//! réseau public a son propre protocole (`PUBLIC_ALPN`), ouvert à toute clé, et borné.

pub mod admission;
pub mod allowlist;
pub mod config;
pub mod control;
mod dns;
pub mod endpoint;
pub mod hold;
pub mod identity;
pub mod inbound;
pub mod netmon;
pub mod outbound;
pub mod owner;
pub mod pool;
pub mod public;
pub mod public_ask;
pub mod public_profile;
pub mod public_serve;
pub mod relay_body;
pub mod rendezvous;
pub mod reply;
pub mod service;
pub mod status;
pub mod stream_io;
pub mod tunnel;

pub use config::Config;
pub use service::{Running, start};

/// Protocole entre compagnons (ALPN) : un échange HTTP/1.1 de l'API `/p2p/…` d'AIODOS
/// par flux QUIC bidirectionnel. Le contenu, lui, reste l'enveloppe scellée d'`aiodos/1`.
pub const ALPN: &[u8] = b"aiodos/2";

/// En-tête ajouté aux requêtes reçues : clé (EndpointId, hexadécimal) de la machine qui
/// les envoie, authentifiée par la poignée de main TLS d'Iroh.
pub const PEER_HEADER: &str = "x-aiodos-peer";

/// En-tête qui porte le jeton de l'API locale (contrôle et mandataire).
pub const TOKEN_HEADER: &str = "x-aiodos-net-token";

/// Protocole de calcul du réseau public (ALPN) : un échange HTTP/1.1 de l'API
/// `/public/v1/…` d'AIODOS par flux, d'une clé jetable (le demandeur) vers le nœud public
/// d'un prêteur. Rien n'y est scellé : le prêteur lit la conversation qu'il calcule, et
/// seul TLS d'Iroh la protège en route (docs/conception/reseau-public-calcul.md, § 4).
pub const PUBLIC_ALPN: &[u8] = b"aiodos/public/1";

/// En-tête ajouté aux requêtes publiques reçues : clé (hexadécimal) de l'appelant,
/// authentifiée par la poignée de main TLS. Jamais `PEER_HEADER` : un inconnu ne passe
/// jamais pour une machine appairée.
pub const PUBLIC_HEADER: &str = "x-aiodos-public";

/// En-tête d'une demande publique (API locale) : la conversation, qui choisit la clé
/// jetable (cf. public_ask.rs). Jamais transmis au prêteur.
pub const CONVERSATION_HEADER: &str = "x-aiodos-conversation";

/// Ce que ce compagnon sait faire, annoncé à AIODOS (ligne « ready », état) : un AIODOS
/// plus récent refuse ce qu'un compagnon plus ancien ferait sans protection.
/// - `tunnel-owner` : tunnels gardés, qui ne relaient que le programme nommé par AIODOS
///   (absent si ce système ne dit pas quel programme se connecte, cf. owner.rs) ;
/// - `service-usage` : l'état dit les flux en cours de chaque service exposé ;
/// - `service-hold` : services tenus, que seul le compagnon joint (`"hold"`, cf. hold.rs ;
///   absent comme `tunnel-owner`) ;
/// - `body-pace` : corps reçus d'une autre machine coupés sous un débit minimal, réponses
///   jamais lues coupées (cf. relay_body.rs) ;
/// - `public-ask` : conversations du réseau public, servies aux inconnus par le nœud public
///   (`aiodos/public/1`, cf. public_serve.rs) et demandées par une clé jetable
///   (`POST /net/v1/public/ask/…`, cf. public_ask.rs) ; profil public avec `free` et `tps`.
pub fn features() -> Vec<&'static str> {
    let mut features = vec!["service-usage", "body-pace", "public-ask"];
    if owner::available() {
        features.push("tunnel-owner");
    }
    if hold::available() {
        features.push("service-hold");
    }
    features
}

/// Codes de fermeture d'une connexion entre compagnons.
pub mod close {
    /// Arrêt du compagnon.
    pub const STOPPING: u32 = 0;
    /// Connexion en double avec la même machine (on garde l'autre).
    pub const DUPLICATE: u32 = 1;
    /// Clé absente de la liste blanche, ou retirée.
    pub const REFUSED: u32 = 403;
    /// Réseau public : trop de connexions d'inconnus en même temps (la plus ancienne
    /// inactive est fermée, ou la nouvelle si toutes servent), ou connexion inactive.
    pub const BUSY: u32 = 503;
}
