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
//!   nommée, joint par un port local de l'autre côté (cf. tunnel.rs) ;
//! - **réseau public** (phase 3.0, désactivé par défaut) : une autre clé et un autre point
//!   de terminaison, qui ne servent qu'à l'annuaire public (cf. public.rs).
//!
//! Le contenu des requêtes reste scellé par AIODOS (enveloppes ChaCha20) : le compagnon
//! n'est qu'un tuyau, qui ne laisse entrer que les machines appairées.

pub mod allowlist;
pub mod config;
pub mod control;
mod dns;
pub mod endpoint;
pub mod identity;
pub mod inbound;
pub mod netmon;
pub mod outbound;
pub mod pool;
pub mod public;
pub mod public_profile;
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

/// Codes de fermeture d'une connexion entre compagnons.
pub mod close {
    /// Arrêt du compagnon.
    pub const STOPPING: u32 = 0;
    /// Connexion en double avec la même machine (on garde l'autre).
    pub const DUPLICATE: u32 = 1;
    /// Clé absente de la liste blanche, ou retirée.
    pub const REFUSED: u32 = 403;
}
