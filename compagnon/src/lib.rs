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
//! - **réseau public** (phase 3.0, désactivé par défaut) : une autre clé et un autre point
//!   de terminaison, qui ne servent qu'à l'annuaire public (cf. public.rs).
//!
//! Le contenu des requêtes reste scellé par AIODOS (enveloppes ChaCha20) : le compagnon
//! n'est qu'un tuyau, qui ne laisse entrer que les machines appairées.

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
pub mod public_profile;
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

/// Ce que ce compagnon sait faire, annoncé à AIODOS (ligne « ready », état) : un AIODOS
/// plus récent refuse ce qu'un compagnon plus ancien ferait sans protection.
/// - `tunnel-owner` : tunnels gardés, qui ne relaient que le programme nommé par AIODOS
///   (absent si ce système ne dit pas quel programme se connecte, cf. owner.rs) ;
/// - `service-usage` : l'état dit les flux en cours de chaque service exposé ;
/// - `service-hold` : services tenus, que seul le compagnon joint (`"hold"`, cf. hold.rs ;
///   absent comme `tunnel-owner`) ;
/// - `body-pace` : corps reçus d'une autre machine coupés sous un débit minimal, réponses
///   jamais lues coupées (cf. relay_body.rs).
pub fn features() -> Vec<&'static str> {
    let mut features = vec!["service-usage", "body-pace"];
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
}
