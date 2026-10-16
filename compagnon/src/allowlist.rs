//! Liste blanche : les seules clés acceptées, celles des machines appairées.
//!
//! AIODOS la fournit et la met à jour par l'API locale (`PUT /net/v1/peers`), à partir
//! de sa table des machines appairées. Une connexion venant d'une autre clé est refusée
//! dès la fin de la poignée de main TLS, avant le moindre flux ; une clé retirée voit
//! ses connexions fermées (cf. pool.rs). Chaque entrée peut porter des indices pour
//! joindre la machine (adresses directes, relais) : indispensables sans annuaire, et
//! plus rapides avec.

use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::{Arc, Mutex, RwLock},
    time::{Duration, Instant, SystemTime},
};

use iroh::{
    EndpointAddr, EndpointId, RelayUrl, TransportAddr,
    endpoint::{AfterHandshakeOutcome, BeforeConnectOutcome, Connection, EndpointHooks},
};
use iroh_tickets::endpoint::EndpointTicket;
use serde::Deserialize;
use tracing::{debug, info};

use crate::{ALPN, close};

const MAX_ENTRIES: usize = 1024;
const MAX_HINTS: usize = 16;
/// Une même clé refusée n'est écrite au journal qu'une fois toutes les 10 minutes.
const REFUSAL_LOG_EVERY: Duration = Duration::from_secs(600);

/// Indices pour joindre une machine : ses relais et ses adresses directes connues.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Hints {
    pub relays: Vec<RelayUrl>,
    pub addrs: Vec<SocketAddr>,
}

/// Liste partagée entre l'API locale, la poignée de main et le pool de connexions.
#[derive(Debug, Clone, Default)]
pub struct Allowlist {
    peers: Arc<RwLock<HashMap<EndpointId, Hints>>>,
    refusals: Arc<Mutex<Refusals>>,
}

#[derive(Debug, Default)]
struct Refusals {
    count: u64,
    last: Option<(EndpointId, SystemTime)>,
    logged: HashMap<EndpointId, Instant>,
}

impl Allowlist {
    pub fn contains(&self, id: &EndpointId) -> bool {
        self.peers.read().expect("liste blanche").contains_key(id)
    }

    /// Adresse à composer pour joindre cette machine : sa clé, plus les indices connus
    /// (Iroh complète par l'annuaire s'il y en a un).
    pub fn addr(&self, id: &EndpointId) -> Option<EndpointAddr> {
        let peers = self.peers.read().expect("liste blanche");
        let hints = peers.get(id)?;
        let relays = hints.relays.iter().cloned().map(TransportAddr::Relay);
        let addrs = hints.addrs.iter().copied().map(TransportAddr::Ip);
        Some(EndpointAddr::from_parts(*id, relays.chain(addrs)))
    }

    pub fn ids(&self) -> Vec<EndpointId> {
        let mut ids: Vec<_> = self.peers.read().expect("liste blanche").keys().copied().collect();
        ids.sort();
        ids
    }

    pub fn len(&self) -> usize {
        self.peers.read().expect("liste blanche").len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Remplace la liste entière ; rend les clés retirées (leurs connexions sont à fermer).
    pub fn replace(&self, next: HashMap<EndpointId, Hints>) -> Vec<EndpointId> {
        let mut peers = self.peers.write().expect("liste blanche");
        let removed = peers.keys().filter(|id| !next.contains_key(*id)).copied().collect();
        *peers = next;
        removed
    }

    /// Nombre de connexions refusées, et la dernière clé refusée.
    pub fn refused(&self) -> (u64, Option<(EndpointId, SystemTime)>) {
        let refusals = self.refusals.lock().expect("refus");
        (refusals.count, refusals.last)
    }

    fn note_refusal(&self, id: EndpointId) {
        let mut refusals = self.refusals.lock().expect("refus");
        refusals.count += 1;
        refusals.last = Some((id, SystemTime::now()));
        let now = Instant::now();
        let due = refusals
            .logged
            .get(&id)
            .is_none_or(|at| now.duration_since(*at) > REFUSAL_LOG_EVERY);
        if due {
            if refusals.logged.len() > 256 {
                refusals.logged.clear();
            }
            refusals.logged.insert(id, now);
            info!(
                "Connexion refusée : la clé {} n'est pas celle d'une machine appairée",
                id.fmt_short()
            );
        } else {
            debug!("Connexion refusée : {}", id.fmt_short());
        }
    }
}

/// Une machine telle qu'AIODOS l'envoie : sa clé (hexadécimal ou base32), des indices
/// pour la joindre, ou un ticket Iroh qui contient les deux.
#[derive(Debug, Default, Deserialize)]
pub struct Entry {
    #[serde(default)]
    pub id: Option<String>,
    #[serde(default)]
    pub ticket: Option<String>,
    #[serde(default)]
    pub addrs: Vec<String>,
    #[serde(default)]
    pub relays: Vec<String>,
}

/// Lit la liste envoyée par AIODOS ; une entrée illisible fait refuser toute la liste
/// (mieux vaut garder l'ancienne qu'en appliquer une moitié).
pub fn parse_entries(entries: Vec<Entry>) -> Result<HashMap<EndpointId, Hints>, String> {
    if entries.len() > MAX_ENTRIES {
        return Err(format!("trop de machines (plus de {MAX_ENTRIES})"));
    }
    let mut out: HashMap<EndpointId, Hints> = HashMap::new();
    for (i, entry) in entries.into_iter().enumerate() {
        let (id, hints) = parse_entry(entry).map_err(|e| format!("machine n° {} : {e}", i + 1))?;
        let merged = out.entry(id).or_default();
        merged.relays.extend(hints.relays);
        merged.addrs.extend(hints.addrs);
        dedup(merged);
    }
    Ok(out)
}

fn parse_entry(entry: Entry) -> Result<(EndpointId, Hints), String> {
    let mut hints = Hints::default();
    let mut id = None;
    if let Some(text) = entry.ticket.as_deref().filter(|t| !t.is_empty()) {
        let ticket: EndpointTicket = text.parse().map_err(|e| format!("ticket illisible ({e})"))?;
        let addr = ticket.endpoint_addr();
        id = Some(addr.id);
        for transport in &addr.addrs {
            match transport {
                TransportAddr::Relay(url) => hints.relays.push(url.clone()),
                TransportAddr::Ip(addr) => hints.addrs.push(*addr),
                _ => {}
            }
        }
    }
    if let Some(text) = entry.id.as_deref().filter(|t| !t.is_empty()) {
        let parsed: EndpointId = text.parse().map_err(|e| format!("clé illisible ({e})"))?;
        if id.is_some_and(|from_ticket| from_ticket != parsed) {
            return Err("la clé ne correspond pas au ticket".into());
        }
        id = Some(parsed);
    }
    let id = id.ok_or("clé manquante")?;
    // Un indice illisible est ignoré : la clé seule suffit (annuaire, ou connexion ouverte
    // par l'autre machine), et une adresse au format inattendu ne doit pas priver toutes
    // les machines de leur place dans la liste.
    for text in &entry.addrs {
        match text.parse() {
            Ok(addr) => hints.addrs.push(addr),
            Err(_) => debug!("{} : adresse ignorée « {text} »", id.fmt_short()),
        }
    }
    for text in &entry.relays {
        match text.parse() {
            Ok(url) => hints.relays.push(url),
            Err(_) => debug!("{} : relais ignoré « {text} »", id.fmt_short()),
        }
    }
    dedup(&mut hints);
    Ok((id, hints))
}

fn dedup(hints: &mut Hints) {
    let mut seen = Vec::new();
    hints.addrs.retain(|a| {
        let fresh = !seen.contains(a);
        seen.push(*a);
        fresh
    });
    let mut seen_relays: Vec<RelayUrl> = Vec::new();
    hints.relays.retain(|r| {
        let fresh = !seen_relays.contains(r);
        seen_relays.push(r.clone());
        fresh
    });
    hints.addrs.truncate(MAX_HINTS);
    hints.relays.truncate(MAX_HINTS);
}

/// Contrôle d'accès branché sur le point de terminaison Iroh : refuse toute clé absente
/// de la liste, dans les deux sens (on ne compose pas non plus une clé inconnue).
#[derive(Debug, Clone)]
pub struct Gate {
    list: Allowlist,
}

impl Gate {
    pub fn new(list: Allowlist) -> Self {
        Gate { list }
    }
}

impl EndpointHooks for Gate {
    async fn before_connect<'a>(
        &'a self,
        remote_addr: &'a EndpointAddr,
        _alpn: &'a [u8],
    ) -> BeforeConnectOutcome {
        if self.list.contains(&remote_addr.id) {
            BeforeConnectOutcome::Accept
        } else {
            BeforeConnectOutcome::Reject
        }
    }

    async fn after_handshake<'a>(&'a self, conn: &'a Connection) -> AfterHandshakeOutcome {
        let id = conn.remote_id();
        if conn.alpn() == ALPN && self.list.contains(&id) {
            return AfterHandshakeOutcome::Accept;
        }
        self.list.note_refusal(id);
        AfterHandshakeOutcome::Reject {
            error_code: close::REFUSED.into(),
            reason: b"refused".to_vec(),
        }
    }
}

#[cfg(test)]
mod tests {
    use iroh::SecretKey;

    use super::*;

    fn key() -> EndpointId {
        SecretKey::generate().public()
    }

    #[test]
    fn entries_accept_hex_keys_addresses_and_tickets() {
        let a = key();
        let b = key();
        let ticket = EndpointTicket::new(EndpointAddr::from_parts(
            b,
            [TransportAddr::Ip("192.0.2.7:4242".parse().unwrap())],
        ));
        let parsed = parse_entries(vec![
            Entry {
                id: Some(a.to_string()),
                addrs: vec!["127.0.0.1:1234".into(), "127.0.0.1:1234".into()],
                ..Default::default()
            },
            Entry { ticket: Some(ticket.to_string()), ..Default::default() },
        ])
        .unwrap();
        assert_eq!(parsed[&a].addrs, vec!["127.0.0.1:1234".parse::<SocketAddr>().unwrap()]);
        assert_eq!(parsed[&b].addrs, vec!["192.0.2.7:4242".parse::<SocketAddr>().unwrap()]);
    }

    #[test]
    fn a_bad_key_rejects_the_whole_list_a_bad_hint_is_ignored() {
        assert!(parse_entries(vec![Entry { id: Some("pas une clé".into()), ..Default::default() }]).is_err());
        assert!(parse_entries(vec![Entry::default()]).is_err());
        let a = key();
        let odd = Entry {
            id: Some(a.to_string()),
            addrs: vec!["machin".into(), "127.0.0.1:9".into()],
            relays: vec!["pas un relais".into()],
            ..Default::default()
        };
        let parsed = parse_entries(vec![odd]).unwrap();
        assert_eq!(parsed[&a].addrs, vec!["127.0.0.1:9".parse::<SocketAddr>().unwrap()]);
        assert!(parsed[&a].relays.is_empty());
    }

    #[test]
    fn replace_reports_removed_keys() {
        let list = Allowlist::default();
        let (a, b) = (key(), key());
        list.replace(HashMap::from([(a, Hints::default()), (b, Hints::default())]));
        assert!(list.contains(&a) && list.contains(&b));
        let removed = list.replace(HashMap::from([(b, Hints::default())]));
        assert_eq!(removed, vec![a]);
        assert!(!list.contains(&a));
        assert_eq!(list.addr(&b).unwrap().id, b);
        assert!(list.addr(&a).is_none());
    }
}
