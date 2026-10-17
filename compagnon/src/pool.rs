//! Une seule connexion QUIC par machine, réutilisée pour toutes les requêtes.
//!
//! L'étape 2.0 (dumbpipe) ouvrait une connexion par requête : 5 à 10 s chacune en 4G.
//! Ici, la première requête vers une machine ouvre la connexion (en direct, ou par un
//! relais puis en direct dès que le perçage réussit), et les suivantes n'y ouvrent qu'un
//! flux. Une connexion ouverte par l'autre machine sert aussi dans l'autre sens : un
//! téléphone injoignable en 4G ouvre la connexion, et le PC s'en sert pour ses propres
//! requêtes.
//!
//! Si les deux machines se connectent en même temps, chacune garde celle qu'a ouverte la
//! plus petite des deux clés (le même choix des deux côtés) ; l'autre est fermée dès
//! qu'elle est inactive. Une connexion bien plus récente l'emporte sur l'ancienne (l'autre
//! machine a redémarré, ou changé de réseau). Une connexion morte est retirée quand Iroh
//! la déclare fermée ; la requête suivante en ouvre une nouvelle.

use std::{
    collections::HashMap,
    fmt,
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant, SystemTime},
};

use iroh::{
    Endpoint, EndpointId, TransportAddr,
    endpoint::{Connection, PathEvent, Side},
};
use n0_future::StreamExt;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info};

use crate::{ALPN, allowlist::Allowlist, close, inbound, tunnel::Tunnels};

/// Passage du ménage dans les connexions en double.
const SWEEP_EVERY: Duration = Duration::from_secs(10);
/// Une connexion en double n'est fermée qu'après ce temps sans aucun flux.
const DUPLICATE_IDLE: Duration = Duration::from_secs(20);
/// Deux connexions ouvertes à moins de 10 s d'écart : appels simultanés, pas un
/// redémarrage (cf. `choose`).
const RACE_WINDOW: Duration = Duration::from_secs(10);

/// Activité d'une connexion : flux en cours, et fin du dernier flux.
#[derive(Debug)]
struct ActivityState {
    active: AtomicUsize,
    last: Mutex<Instant>,
}

/// Un flux en cours sur une connexion, compté jusqu'à sa fin.
#[derive(Debug)]
pub struct Activity(Arc<ActivityState>);

impl Drop for Activity {
    fn drop(&mut self) {
        *self.0.last.lock().expect("activité") = Instant::now();
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Une connexion établie avec une machine autorisée.
#[derive(Debug, Clone)]
pub struct Tracked {
    pub conn: Connection,
    /// Ouverte par cette machine-ci (sinon par l'autre).
    pub outgoing: bool,
    pub opened: SystemTime,
    opened_at: Instant,
    activity: Arc<ActivityState>,
}

impl Tracked {
    fn new(conn: Connection) -> Self {
        let now = Instant::now();
        Tracked {
            outgoing: conn.side() == Side::Client,
            conn,
            opened: SystemTime::now(),
            opened_at: now,
            activity: Arc::new(ActivityState { active: AtomicUsize::new(0), last: Mutex::new(now) }),
        }
    }

    /// Compte un nouveau flux sur cette connexion.
    pub fn activity(&self) -> Activity {
        self.activity.active.fetch_add(1, Ordering::SeqCst);
        Activity(self.activity.clone())
    }

    fn is_open(&self) -> bool {
        self.conn.close_reason().is_none()
    }

    /// Temps depuis la fin du dernier flux, `None` si un flux est en cours.
    fn idle_for(&self) -> Option<Duration> {
        if self.activity.active.load(Ordering::SeqCst) > 0 {
            return None;
        }
        Some(self.activity.last.lock().expect("activité").elapsed())
    }
}

/// Compteurs par machine, pour l'état.
#[derive(Debug, Default, Clone)]
pub struct PeerStats {
    pub dials: u64,
    pub dial_failures: u64,
    pub requests_out: u64,
    pub requests_in: u64,
    pub last_error: Option<(String, SystemTime)>,
}

#[derive(Debug, Default)]
struct PeerState {
    conns: Vec<Tracked>,
    stats: PeerStats,
}

/// Vue d'une machine pour l'état : sa connexion retenue, combien sont ouvertes.
#[derive(Debug, Clone)]
pub struct PeerView {
    pub id: EndpointId,
    pub current: Option<Tracked>,
    pub open: usize,
    pub stats: PeerStats,
}

/// Échec pour joindre une machine.
#[derive(Debug)]
pub enum DialError {
    NotAllowed,
    Timeout(Duration),
    Failed(String),
    Stopping,
}

impl fmt::Display for DialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DialError::NotAllowed => write!(f, "machine absente de la liste blanche"),
            DialError::Timeout(after) => write!(f, "aucune réponse en {} s", after.as_secs()),
            DialError::Failed(reason) => write!(f, "{reason}"),
            DialError::Stopping => write!(f, "compagnon en cours d'arrêt"),
        }
    }
}

pub struct Pool {
    pub endpoint: Endpoint,
    pub allowlist: Allowlist,
    /// Serveur réseau local d'AIODOS, destinataire des requêtes reçues.
    pub forward: SocketAddr,
    pub shutdown: CancellationToken,
    /// Services ouverts à d'autres machines, et tunnels vers les leurs.
    pub tunnels: Tunnels,
    dial_timeout: Duration,
    me: EndpointId,
    peers: Mutex<HashMap<EndpointId, PeerState>>,
    dialing: Mutex<HashMap<EndpointId, Arc<tokio::sync::Mutex<()>>>>,
}

impl Pool {
    pub fn new(
        endpoint: Endpoint,
        allowlist: Allowlist,
        forward: SocketAddr,
        dial_timeout: Duration,
        shutdown: CancellationToken,
    ) -> Arc<Self> {
        Arc::new(Pool {
            me: endpoint.id(),
            endpoint,
            allowlist,
            forward,
            shutdown,
            tunnels: Tunnels::default(),
            dial_timeout,
            peers: Mutex::default(),
            dialing: Mutex::default(),
        })
    }

    /// Connexion à utiliser pour joindre cette machine : celle qui existe déjà (ouverte
    /// par l'une ou l'autre machine), sinon une nouvelle. Une seule ouverture à la fois
    /// par machine : les requêtes simultanées attendent la même.
    pub async fn connection(self: &Arc<Self>, id: EndpointId) -> Result<Tracked, DialError> {
        if let Some(tracked) = self.current(&id) {
            return Ok(tracked);
        }
        if !self.allowlist.contains(&id) {
            return Err(DialError::NotAllowed);
        }
        let lock = self
            .dialing
            .lock()
            .expect("ouvertures")
            .entry(id)
            .or_default()
            .clone();
        let _guard = lock.lock().await;
        if let Some(tracked) = self.current(&id) {
            return Ok(tracked);
        }
        let addr = self.allowlist.addr(&id).ok_or(DialError::NotAllowed)?;
        self.with_stats(id, |s| s.dials += 1);
        let started = Instant::now();
        let result = tokio::select! {
            _ = self.shutdown.cancelled() => Err(DialError::Stopping),
            outcome = tokio::time::timeout(self.dial_timeout, self.endpoint.connect(addr, ALPN)) => {
                match outcome {
                    Ok(Ok(conn)) => Ok(conn),
                    Ok(Err(err)) => Err(DialError::Failed(err.to_string())),
                    Err(_) => Err(DialError::Timeout(self.dial_timeout)),
                }
            }
        };
        match result {
            Ok(conn) => {
                debug!("{} jointe en {} ms", id.fmt_short(), started.elapsed().as_millis());
                Ok(self.register(conn))
            }
            Err(err) => {
                self.with_stats(id, |s| s.dial_failures += 1);
                self.note_error(id, format!("connexion impossible : {err}"));
                info!("{} injoignable : {err}", id.fmt_short());
                Err(err)
            }
        }
    }

    /// Range une connexion établie (sortante ou entrante), sert les flux que l'autre
    /// machine y ouvrira, et suit ses changements de chemin.
    pub fn register(self: &Arc<Self>, conn: Connection) -> Tracked {
        let id = conn.remote_id();
        let tracked = Tracked::new(conn);
        let open = {
            let mut peers = self.peers.lock().expect("pool");
            let state = peers.entry(id).or_default();
            state.conns.retain(Tracked::is_open);
            state.conns.push(tracked.clone());
            state.conns.len()
        };
        info!(
            "Connexion {} {} ({}){}",
            if tracked.outgoing { "ouverte vers" } else { "reçue de" },
            id.fmt_short(),
            path_label(&tracked.conn),
            if open > 1 { ", en double : la moins utile sera fermée une fois inactive" } else { "" }
        );
        tokio::spawn(watch_paths(id, tracked.conn.path_events()));
        tokio::spawn(inbound::serve_connection(self.clone(), tracked.clone()));
        tracked
    }

    /// Retire une connexion fermée.
    pub(crate) fn forget(&self, id: &EndpointId, conn: &Connection) {
        let mut peers = self.peers.lock().expect("pool");
        if let Some(state) = peers.get_mut(id) {
            state.conns.retain(|t| t.conn.stable_id() != conn.stable_id());
        }
    }

    /// Connexion retenue pour cette machine, s'il y en a une ouverte.
    fn current(&self, id: &EndpointId) -> Option<Tracked> {
        let peers = self.peers.lock().expect("pool");
        let state = peers.get(id)?;
        choose(self.me, *id, state.conns.iter().filter(|t| t.is_open())).cloned()
    }

    /// Ferme les connexions des machines retirées de la liste blanche.
    pub fn close_peers(&self, ids: &[EndpointId]) {
        let mut peers = self.peers.lock().expect("pool");
        for id in ids {
            if let Some(state) = peers.remove(id) {
                for tracked in &state.conns {
                    tracked.conn.close(close::REFUSED.into(), b"removed");
                }
                info!("{} retirée de la liste blanche", id.fmt_short());
            }
        }
    }

    /// Arrêt : ferme proprement toutes les connexions (les autres machines le savent
    /// aussitôt, au lieu d'attendre l'expiration).
    pub fn close_all(&self) {
        let peers = self.peers.lock().expect("pool");
        for state in peers.values() {
            for tracked in &state.conns {
                tracked.conn.close(close::STOPPING.into(), b"stopping");
            }
        }
    }

    pub fn note_error(&self, id: EndpointId, message: String) {
        self.with_stats(id, |s| s.last_error = Some((message, SystemTime::now())));
    }

    pub fn count_out(&self, id: EndpointId) {
        self.with_stats(id, |s| s.requests_out += 1);
    }

    pub fn count_in(&self, id: EndpointId) {
        self.with_stats(id, |s| s.requests_in += 1);
    }

    fn with_stats(&self, id: EndpointId, update: impl FnOnce(&mut PeerStats)) {
        let mut peers = self.peers.lock().expect("pool");
        update(&mut peers.entry(id).or_default().stats);
    }

    /// Machines autorisées ou connues, avec leur connexion retenue.
    pub fn snapshot(&self) -> Vec<PeerView> {
        let peers = self.peers.lock().expect("pool");
        let mut ids = self.allowlist.ids();
        for id in peers.keys() {
            if !ids.contains(id) {
                ids.push(*id);
            }
        }
        ids.into_iter()
            .map(|id| {
                let state = peers.get(&id);
                let open: Vec<&Tracked> = state
                    .map(|s| s.conns.iter().filter(|t| t.is_open()).collect())
                    .unwrap_or_default();
                PeerView {
                    id,
                    current: choose(self.me, id, open.iter().copied()).cloned(),
                    open: open.len(),
                    stats: state.map(|s| s.stats.clone()).unwrap_or_default(),
                }
            })
            .collect()
    }

    /// Passe régulièrement fermer les connexions en double devenues inactives.
    pub fn spawn_sweeper(self: &Arc<Self>) {
        let pool = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = pool.shutdown.cancelled() => break,
                    _ = tokio::time::sleep(SWEEP_EVERY) => pool.sweep(),
                }
            }
        });
    }

    fn sweep(&self) {
        let mut peers = self.peers.lock().expect("pool");
        for (id, state) in peers.iter_mut() {
            state.conns.retain(Tracked::is_open);
            if state.conns.len() < 2 {
                continue;
            }
            let keep = choose(self.me, *id, state.conns.iter()).map(|t| t.conn.stable_id());
            for tracked in &state.conns {
                if Some(tracked.conn.stable_id()) != keep
                    && tracked.idle_for().is_some_and(|idle| idle >= DUPLICATE_IDLE)
                {
                    info!("Connexion en double avec {} fermée", id.fmt_short());
                    tracked.conn.close(close::DUPLICATE.into(), b"duplicate");
                }
            }
        }
    }
}

/// Connexion à garder quand il y en a plusieurs avec la même machine.
///
/// - Ouvertes à plus de `RACE_WINDOW` d'écart : la plus récente. L'autre machine a sans
///   doute perdu l'ancienne (redémarrage, changement de réseau), même si on ne s'en est
///   pas encore aperçu ici.
/// - Ouvertes presque en même temps (les deux machines se sont appelées ensemble) : celle
///   qu'a ouverte la plus petite des deux clés, le même choix des deux côtés.
fn choose<'a>(
    me: EndpointId,
    peer: EndpointId,
    conns: impl Iterator<Item = &'a Tracked>,
) -> Option<&'a Tracked> {
    let conns: Vec<&Tracked> = conns.collect();
    let opened: Vec<(bool, Instant)> = conns.iter().map(|t| (t.outgoing, t.opened_at)).collect();
    pick(me, peer, &opened).map(|i| conns[i])
}

/// Cœur de `choose` : (ouverte par cette machine-ci, instant d'ouverture) → rang retenu.
fn pick(me: EndpointId, peer: EndpointId, opened: &[(bool, Instant)]) -> Option<usize> {
    let newest = opened.iter().map(|(_, at)| *at).max()?;
    let lower = me.min(peer);
    opened
        .iter()
        .enumerate()
        .filter(|(_, (_, at))| newest.duration_since(*at) <= RACE_WINDOW)
        .max_by_key(|(_, (outgoing, at))| {
            let opener = if *outgoing { me } else { peer };
            (opener == lower, *at)
        })
        .map(|(i, _)| i)
}

#[cfg(test)]
mod tests {
    use iroh::SecretKey;

    use super::*;

    fn keys() -> (EndpointId, EndpointId) {
        let (a, b) = (SecretKey::generate().public(), SecretKey::generate().public());
        (a.min(b), a.max(b))
    }

    #[test]
    fn simultaneous_calls_keep_the_same_connection_on_both_sides() {
        let (low, high) = keys();
        let t0 = Instant::now();
        let t1 = t0 + Duration::from_millis(300);
        // Vue de la petite clé : sa connexion sortante (t0), l'entrante (t1)
        assert_eq!(pick(low, high, &[(true, t0), (false, t1)]), Some(0));
        // Vue de la grande clé : l'entrante est celle de la petite clé
        assert_eq!(pick(high, low, &[(false, t0), (true, t1)]), Some(0));
    }

    #[test]
    fn a_much_newer_connection_wins_after_a_restart() {
        let (low, high) = keys();
        let old = Instant::now();
        let new = old + Duration::from_secs(120);
        // L'ancienne, ouverte par la petite clé, est sans doute morte : la nouvelle l'emporte
        assert_eq!(pick(low, high, &[(true, old), (false, new)]), Some(1));
        assert_eq!(pick(high, low, &[(false, old), (true, new)]), Some(1));
        assert_eq!(pick(low, high, &[]), None);
    }
}

/// Chemin actuel d'une connexion : direct, par un relais, ou pas encore choisi.
pub fn path_kind(conn: &Connection) -> Option<(&'static str, String, Duration)> {
    let paths = conn.paths();
    let selected = paths.iter().find(|p| p.is_selected())?;
    let (kind, remote) = describe(selected.remote_addr());
    Some((kind, remote, selected.rtt()))
}

fn path_label(conn: &Connection) -> String {
    match path_kind(conn) {
        Some(("direct", remote, _)) => format!("direct, {remote}"),
        Some((_, remote, _)) => format!("par le relais {remote}"),
        None => "chemin en cours de choix".into(),
    }
}

fn describe(addr: &TransportAddr) -> (&'static str, String) {
    match addr {
        TransportAddr::Ip(ip) => ("direct", ip.to_string()),
        TransportAddr::Relay(url) => ("relay", url.to_string()),
        other => ("other", other.to_string()),
    }
}

/// Note au journal chaque changement de chemin (passage en direct, retour au relais
/// après un changement de réseau…). La tâche s'arrête avec la connexion.
async fn watch_paths(id: EndpointId, mut events: iroh::endpoint::PathEventStream) {
    let mut last_kind = None;
    while let Some(event) = events.next().await {
        if let PathEvent::Selected { remote_addr, .. } = event {
            let (kind, remote) = describe(&remote_addr);
            if last_kind != Some(kind) {
                match kind {
                    "direct" => info!("Chemin vers {} : direct ({remote})", id.fmt_short()),
                    _ => info!("Chemin vers {} : par le relais {remote}", id.fmt_short()),
                }
                last_kind = Some(kind);
            }
        }
    }
}
