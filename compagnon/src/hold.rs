//! Service tenu, chez la machine qui prête : le serveur RPC de llama.cpp écoute sur
//! 127.0.0.1, sans mot de passe, et n'importe quel programme de la machine (un autre compte
//! du PC) peut s'y connecter. Ce serveur ne sert qu'un client à la fois, dans l'ordre
//! d'arrivée, et ne lâche un client que quand celui-ci ferme sa connexion.
//!
//! Le compagnon s'y connecte donc lui-même dès que le service est exposé avec `"hold":
//! true`, et ne le lâche plus : une connexion à lui est servie, une autre attend derrière,
//! et chaque flux du tunnel (le llama-server de la machine qui emprunte) prend la plus
//! ancienne libre. Un autre programme qui se connecte se range derrière elles et n'est
//! jamais servi : avant de rendre une connexion (fin d'un flux), le compagnon vérifie dans
//! la table des connexions du système qu'aucune autre que les siennes n'attend, et qu'une
//! des siennes passera ensuite. Sinon il garde la connexion ouverte (le serveur reste
//! occupé, l'intrus attend pour rien), refuse les flux suivants et le dit dans l'état
//! (`"blocked"`) ; AIODOS arrête alors le serveur. La table est aussi relue toutes les
//! `CHECK_EVERY` : un intrus est vu sans attendre la fin d'un flux.
//!
//! Le serveur RPC de llama.cpp n'a qu'une place dans sa file d'attente (essai du 21/10,
//! b11707) : avec la connexion servie et celle de relève du compagnon, le système refuse
//! d'emblée toute autre connexion. Une tentative refusée ne bloque pas le service : elle
//! n'entre jamais dans la file ; si elle y entre plus tard (une place libérée), elle est vue
//! avant que le compagnon ne rende sa connexion suivante.
//!
//! Restent ouverts : l'instant entre le démarrage du serveur et la première connexion du
//! compagnon (un intrus servi alors et toujours connecté est vu ensuite, et le prêt
//! arrêté) ; un serveur qui fermerait de lui-même une connexion (il ne le fait que sur une
//! erreur de protocole) ; et ce que permettrait une faille du serveur RPC face au
//! llama-server légitime.

use std::{
    collections::{HashSet, VecDeque},
    fmt, io,
    net::{Ipv4Addr, SocketAddr},
    sync::{Arc, Mutex},
    time::Duration,
};

use hyper::upgrade::Upgraded;
use hyper_util::rt::TokioIo;
use tokio::{
    io::AsyncWriteExt,
    net::{TcpSocket, TcpStream},
    sync::Notify,
    time::timeout,
};
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

use crate::owner;

/// Table des connexions relue au moins aussi souvent.
pub const CHECK_EVERY: Duration = Duration::from_secs(1);
/// Connexion du compagnon au service.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
/// Une connexion rendue : le serveur la ferme aussitôt (il lit la fin), au plus ce temps.
const RELEASE_WAIT: Duration = Duration::from_secs(5);
/// Connexions du compagnon gardées au service quand rien ne se passe : la servie, et une
/// derrière.
const KEPT: usize = 2;

/// Le service est-il tenable ici ? Il faut lire la table des connexions du système.
pub fn available() -> bool {
    owner::available()
}

/// Où en est une connexion du compagnon au service.
enum Stage {
    /// En cours d'ouverture (son adresse est déjà connue : elle compte pour une des siennes).
    Connecting,
    /// Prête pour le prochain flux.
    Free(TcpStream),
    /// Relaie un flux du tunnel.
    Lent,
    /// Rendue : le serveur la ferme et passe à la suivante.
    Releasing,
    /// Gardée ouverte pour toujours (jamais lue) : un autre programme attendait derrière.
    Kept(#[allow(dead_code)] TcpStream),
}

struct Own {
    id: u64,
    addr: SocketAddr,
    stage: Stage,
}

#[derive(Default)]
struct State {
    next: u64,
    /// Dans l'ordre d'ouverture, qui est celui de la file du serveur.
    conns: VecDeque<Own>,
    blocked: Option<String>,
    /// Connexions d'autres programmes vues vers le service.
    foreign: HashSet<SocketAddr>,
}

impl State {
    /// Une connexion du compagnon, établie et pas encore rendue, ouverte après `id` :
    /// elle passe avant tout programme arrivé après.
    fn has_newer(&self, id: u64) -> bool {
        self.conns.iter().any(|c| c.id > id && matches!(c.stage, Stage::Free(_) | Stage::Lent))
    }

    /// Faut-il une connexion de plus ? Au moins `KEPT` établies, dont la plus récente libre.
    fn wants_more(&self) -> bool {
        let open: Vec<&Own> = self
            .conns
            .iter()
            .filter(|c| matches!(c.stage, Stage::Free(_) | Stage::Lent | Stage::Connecting))
            .collect();
        open.len() < KEPT || open.last().is_some_and(|c| matches!(c.stage, Stage::Lent))
    }
}

/// Le service tenu : ses connexions, et ce que la table des connexions a montré.
pub struct Holder {
    name: String,
    server: SocketAddr,
    stop: CancellationToken,
    state: Mutex<State>,
    /// Connexions ouvertes une à une, et jamais pendant une lecture de la table : l'ordre
    /// d'ouverture est celui de la file, et une connexion en cours d'ouverture est déjà connue.
    serial: tokio::sync::Mutex<()>,
    wake: Notify,
}

impl fmt::Debug for Holder {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Holder").field("name", &self.name).field("server", &self.server).finish()
    }
}

/// Une connexion du compagnon confiée à un flux du tunnel.
pub struct Lent {
    holder: Arc<Holder>,
    id: u64,
    tcp: TcpStream,
}

impl Holder {
    /// Commence à tenir le service `name` (127.0.0.1:`port`), jusqu'à `stop` ou l'arrêt
    /// de `parent`.
    pub fn start(name: &str, port: u16, parent: &CancellationToken) -> Arc<Holder> {
        let holder = Arc::new(Holder {
            name: name.to_string(),
            server: SocketAddr::from((Ipv4Addr::LOCALHOST, port)),
            stop: parent.child_token(),
            state: Mutex::default(),
            serial: tokio::sync::Mutex::new(()),
            wake: Notify::new(),
        });
        info!("Service {name} tenu : le compagnon garde ses connexions au port {port}");
        tokio::spawn(holder.clone().maintain());
        holder
    }

    /// Ne plus tenir le service (retiré, ou remplacé) : ses connexions se ferment.
    pub fn stop(&self) {
        self.stop.cancel();
    }

    /// Pourquoi le compagnon ne peut plus garantir que lui seul est servi, s'il ne le peut plus.
    pub fn blocked(&self) -> Option<String> {
        self.state.lock().expect("service tenu").blocked.clone()
    }

    /// Connexions d'autres programmes vues vers le service.
    pub fn foreign(&self) -> usize {
        self.state.lock().expect("service tenu").foreign.len()
    }

    /// Connexions du compagnon établies au service.
    pub fn holding(&self) -> usize {
        let state = self.state.lock().expect("service tenu");
        state.conns.iter().filter(|c| matches!(c.stage, Stage::Free(_) | Stage::Lent)).count()
    }

    fn block(&self, reason: String) {
        let mut state = self.state.lock().expect("service tenu");
        if state.blocked.is_none() {
            warn!("Service {} : {reason} ; plus aucun flux, le serveur doit être arrêté", self.name);
            state.blocked = Some(reason);
        }
    }

    async fn maintain(self: Arc<Self>) {
        loop {
            if self.blocked().is_none() {
                self.top_up().await;
                let _ = self.inspect().await;
            }
            tokio::select! {
                _ = self.stop.cancelled() => break,
                _ = tokio::time::sleep(CHECK_EVERY) => {}
                _ = self.wake.notified() => {}
            }
        }
        self.state.lock().expect("service tenu").conns.clear(); // connexions fermées
        debug!("service {} : plus tenu", self.name);
    }

    /// Ouvre ce qu'il faut de connexions (cf. `State::wants_more`).
    async fn top_up(&self) {
        for _ in 0..KEPT {
            if !self.state.lock().expect("service tenu").wants_more() {
                return;
            }
            if let Err(err) = self.connect_one().await {
                debug!("service {} : connexion de plus impossible ({err})", self.name);
                return;
            }
        }
    }

    /// Une connexion de plus au service, rangée derrière les autres.
    async fn connect_one(&self) -> io::Result<()> {
        let _serial = self.serial.lock().await;
        if self.stop.is_cancelled() {
            return Err(io::Error::other("service plus tenu"));
        }
        let socket = TcpSocket::new_v4()?;
        socket.bind(SocketAddr::from((Ipv4Addr::LOCALHOST, 0)))?;
        let addr = socket.local_addr()?;
        let id = {
            let mut state = self.state.lock().expect("service tenu");
            let id = state.next;
            state.next += 1;
            state.conns.push_back(Own { id, addr, stage: Stage::Connecting });
            id
        };
        let connected = match timeout(CONNECT_TIMEOUT, socket.connect(self.server)).await {
            Ok(Ok(tcp)) => Ok(tcp),
            Ok(Err(err)) => Err(err),
            Err(_) => Err(io::Error::new(io::ErrorKind::TimedOut, "pas de réponse")),
        };
        let mut state = self.state.lock().expect("service tenu");
        let Some(at) = state.conns.iter().position(|c| c.id == id) else {
            return Err(io::Error::other("service plus tenu"));
        };
        match connected {
            Ok(tcp) => {
                let _ = tcp.set_nodelay(true);
                state.conns[at].stage = Stage::Free(tcp);
                Ok(())
            }
            Err(err) => {
                state.conns.remove(at);
                Err(err)
            }
        }
    }

    /// Relit la table des connexions : vrai si seules celles du compagnon visent le service.
    /// Sinon (ou table illisible), le service est bloqué.
    async fn inspect(&self) -> bool {
        let _serial = self.serial.lock().await; // aucune connexion à lui ne s'ouvre pendant ce temps
        let own: HashSet<SocketAddr> =
            self.state.lock().expect("service tenu").conns.iter().map(|c| c.addr).collect();
        let server = self.server;
        let clients = match tokio::task::spawn_blocking(move || owner::clients_of(server)).await {
            Ok(Ok(clients)) => clients,
            Ok(Err(err)) => {
                self.block(format!("table des connexions illisible ({err})"));
                return false;
            }
            Err(err) => {
                self.block(format!("contrôle interrompu ({err})"));
                return false;
            }
        };
        let others: Vec<SocketAddr> = clients.into_iter().filter(|c| !own.contains(c)).collect();
        if others.is_empty() {
            return true;
        }
        let count = {
            let mut state = self.state.lock().expect("service tenu");
            state.foreign.extend(others.iter().copied());
            state.foreign.len()
        };
        self.block(format!(
            "un autre programme de cette machine s'est connecté au service ({count} connexion(s), \
             dont {})",
            others[0]
        ));
        false
    }

    /// La plus ancienne connexion libre, pour un flux du tunnel.
    pub async fn take(self: &Arc<Self>) -> Result<Lent, String> {
        for _ in 0..2 {
            if let Some(reason) = self.blocked() {
                return Err(format!("Service bloqué sur la machine jointe : {reason}."));
            }
            if self.stop.is_cancelled() {
                return Err("Ce service n'est plus ouvert.".into());
            }
            let taken = {
                let mut state = self.state.lock().expect("service tenu");
                state.conns.iter_mut().find(|c| matches!(c.stage, Stage::Free(_))).and_then(|c| {
                    match std::mem::replace(&mut c.stage, Stage::Lent) {
                        Stage::Free(tcp) => Some((c.id, tcp)),
                        _ => None,
                    }
                })
            };
            if let Some((id, tcp)) = taken {
                self.wake.notify_one(); // une connexion de relève
                return Ok(Lent { holder: self.clone(), id, tcp });
            }
            if let Err(err) = self.connect_one().await {
                return Err(format!("Le service ne répond pas sur la machine jointe ({err})."));
            }
        }
        Err("Le service ne répond pas sur la machine jointe.".into())
    }

    /// Fin d'un flux : la connexion `id` peut-elle être rendue au serveur ? Il faut qu'une
    /// connexion du compagnon passe ensuite, et qu'aucun autre programme n'attende.
    async fn may_release(&self, id: u64) -> bool {
        if self.blocked().is_some() {
            return false;
        }
        if !self.state.lock().expect("service tenu").has_newer(id) {
            let _ = self.connect_one().await;
            if !self.state.lock().expect("service tenu").has_newer(id) {
                self.block("aucune connexion du compagnon pour passer ensuite".into());
                return false;
            }
        }
        if !self.inspect().await {
            return false;
        }
        self.set_stage(id, Stage::Releasing);
        true
    }

    fn set_stage(&self, id: u64, stage: Stage) {
        let mut state = self.state.lock().expect("service tenu");
        if let Some(own) = state.conns.iter_mut().find(|c| c.id == id) {
            own.stage = stage;
        }
    }

    /// La connexion `id` est fermée des deux côtés : oubliée.
    fn done(&self, id: u64) {
        let mut state = self.state.lock().expect("service tenu");
        state.conns.retain(|c| c.id != id);
        drop(state);
        self.wake.notify_one();
    }
}

impl Lent {
    /// Le flux n'a pas commencé (échange abandonné) : rien n'est passé, la connexion resservira.
    pub fn put_back(self) {
        let Lent { holder, id, tcp } = self;
        holder.set_stage(id, Stage::Free(tcp));
    }
}

/// Relaie un flux du tunnel par une connexion tenue. Quand la machine qui emprunte a fini
/// (fin de son côté), la connexion n'est rendue au serveur que si `may_release` le
/// permet ; sinon elle reste ouverte, et le serveur occupé.
pub async fn relay(lent: Lent, upgraded: Upgraded) {
    let Lent { holder, id, mut tcp } = lent;
    let stop = holder.stop.clone();
    let (mut local_read, mut local_write) = tcp.split();
    let (mut remote_read, mut remote_write) = tokio::io::split(TokioIo::new(upgraded));
    let mut from_service = Box::pin(async {
        let _ = tokio::io::copy(&mut local_read, &mut remote_write).await;
        let _ = remote_write.shutdown().await;
    });
    let borrower_done = {
        let to_service = tokio::io::copy(&mut remote_read, &mut local_write);
        tokio::select! {
            _ = stop.cancelled() => return,
            _ = to_service => true,
            _ = &mut from_service => false,
        }
    };
    if !borrower_done {
        // Le serveur a fermé de lui-même (erreur de protocole) : il est passé au suivant
        debug!("service {} : connexion fermée par le serveur", holder.name);
        holder.done(id);
        return;
    }
    if holder.may_release(id).await {
        let _ = local_write.shutdown().await;
        let _ = timeout(RELEASE_WAIT, &mut from_service).await;
        drop(from_service);
        holder.done(id);
    } else {
        drop(from_service);
        holder.set_stage(id, Stage::Kept(tcp));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn own(id: u64, stage: Stage) -> Own {
        Own { id, addr: SocketAddr::from(([127, 0, 0, 1], 40000 + id as u16)), stage }
    }

    #[tokio::test]
    async fn a_connection_is_released_only_with_one_of_ours_behind_it() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let tcp = || TcpStream::from_std({
            let s = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
            s.set_nonblocking(true).unwrap();
            s
        }).unwrap();
        let mut state = State::default();
        state.conns.push_back(own(0, Stage::Lent));
        assert!(!state.has_newer(0), "aucune derrière");
        assert!(state.wants_more());
        state.conns.push_back(own(1, Stage::Releasing));
        assert!(!state.has_newer(0), "une connexion rendue ne compte pas");
        state.conns.push_back(own(2, Stage::Free(tcp())));
        assert!(state.has_newer(0) && !state.has_newer(2));
        assert!(!state.wants_more(), "la plus récente est libre");
        state.conns.push_back(own(3, Stage::Lent));
        assert!(state.wants_more(), "la plus récente est prise : une de relève");
    }
}
