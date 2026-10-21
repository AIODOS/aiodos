//! Tunnels : un service TCP local d'une machine rendu joignable par une autre machine
//! autorisée, à travers la connexion QUIC déjà gardée avec elle. Premier usage : le
//! serveur RPC de llama.cpp, pour répartir un gros modèle entre ses machines
//! (docs/gros-modeles.md).
//!
//! - **côté service** (la machine qui prête) : AIODOS déclare ses services exposés,
//!   `PUT /net/v1/exposed` `{"services": [{"name", "port", "peers": [clé…], "hold"}]}` ;
//!   le service n'écoute que sur 127.0.0.1. L'état dit, pour chacun, les flux en cours et
//!   depuis quand le dernier s'est fermé (`active`, `total`, `idle_s`) : AIODOS arrête le
//!   serveur RPC dès que le tunnel est retombé. Avec `"hold": true`, le compagnon **tient**
//!   le service : il y garde ses propres connexions, pour qu'aucun autre programme de la
//!   machine ne soit servi (cf. hold.rs) ; l'état dit alors `held`, `holding` (connexions
//!   du compagnon établies), `foreign` (connexions d'autres programmes vues) et `blocked`
//!   (pourquoi plus aucun flux ne passe : AIODOS arrête alors le serveur) ;
//! - **côté client** (la machine qui emprunte) : `POST /net/v1/tunnels` `{"peer", "name"}`
//!   ouvre un port local (127.0.0.1) ; chaque connexion à ce port devient un flux QUIC vers
//!   la machine, ouvert par un échange HTTP/1.1 `GET /p2p/v1/tunnel/<nom>` avec
//!   « Upgrade », puis relayé octet par octet vers le service.
//!   `DELETE /net/v1/tunnels/<port>` le ferme.
//!
//! **Tunnel gardé** : n'importe quel programme de la machine peut se connecter à
//! 127.0.0.1. Avec `"await_owner": true` (ou `"owner_pid"`), le port ne relaie que le
//! programme qu'AIODOS nomme ensuite, `PUT /net/v1/tunnels/<port>` `{"owner_pid"}` (le
//! llama-server qu'il vient de lancer) : le compagnon cherche quel programme est au bout de
//! chaque connexion (cf. owner.rs) et ferme les autres. Une connexion arrivée avant que le
//! programme soit nommé attend. Les connexions en attente sont contrôlées ensemble, d'une
//! seule lecture de la table des connexions : un intrus qui en ouvre des milliers ne fait
//! pas écarter celle de llama-server. `"max_connections"` borne les connexions relayées en
//! même temps. La réponse dit `"guarded": true` : un compagnon plus ancien ne le dit pas,
//! et AIODOS referme alors le tunnel aussitôt.
//!
//! Chiffrement et authentification sont ceux de la connexion (QUIC, clés Iroh) : seules les
//! machines qu'AIODOS a nommées pour ce service le joignent, jamais le réseau.

use std::{
    collections::{HashMap, HashSet},
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use hyper::{
    Method, Request, Response, StatusCode,
    body::Incoming,
    header::{CONNECTION, HOST, HeaderMap, HeaderValue, UPGRADE},
    upgrade::{OnUpgrade, Upgraded},
};
use hyper_util::rt::TokioIo;
use iroh::EndpointId;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::{
    io::AsyncWriteExt,
    net::{TcpListener, TcpStream},
    sync::{Semaphore, mpsc, oneshot, watch},
};
use tokio_util::sync::CancellationToken;
use tracing::{debug, info};

use crate::{
    admission::{Admission, Ticket},
    hold::{self, Holder},
    outbound, owner,
    pool::Pool,
    reply::{self, Body},
};

/// Chemin de l'échange qui ouvre un tunnel, suivi du nom du service.
pub const PATH_PREFIX: &str = "/p2p/v1/tunnel/";
/// Valeur de l'en-tête « Upgrade » d'un tunnel.
pub const PROTOCOL: &str = "aiodos-tunnel";
/// Nom d'un service : minuscules, chiffres et tirets.
const MAX_NAME: usize = 64;
/// Services exposés au plus.
const MAX_SERVICES: usize = 64;
/// Connexion au service local.
const LOCAL_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
/// Connexions relayées en même temps par un tunnel, au plus : llama-server n'en garde
/// qu'une par serveur RPC, plus quelques brèves l'une après l'autre au démarrage.
pub const MAX_CONNECTIONS: usize = 8;
/// Connexions locales en attente du contrôle de leur programme, au plus. Au-delà, la plus
/// ancienne en attente est écartée, pas la nouvelle, et jamais une dont le contrôle a
/// commencé : un intrus qui occupe ces places ne fait pas refuser la connexion de
/// llama-server qui arrive ensuite. Les contrôles se font par lots (cf. `checker`) : la
/// file se vide en une lecture de la table, elle ne se remplit pas.
pub const MAX_CHECKING: usize = 512;
/// Connexions contrôlées d'une seule lecture de la table, au plus.
const MAX_BATCH: usize = 1024;
/// Une connexion arrivée avant qu'AIODOS ait nommé le programme du tunnel attend au plus
/// ce temps.
const OWNER_WAIT: Duration = Duration::from_secs(30);
/// Flux d'une autre machine vers un même service, en même temps, au plus.
pub const MAX_SERVICE_STREAMS: usize = 8;

/// Un service exposé : son port local, les machines qui peuvent le joindre, ses flux, et
/// ses connexions tenues par le compagnon (cf. hold.rs).
#[derive(Debug, Clone)]
struct Service {
    port: u16,
    peers: HashSet<EndpointId>,
    usage: Arc<Usage>,
    hold: Option<Arc<Holder>>,
}

/// Flux en cours vers un service, depuis son ouverture, et fin du dernier.
#[derive(Debug, Default)]
struct Usage {
    active: AtomicUsize,
    total: AtomicU64,
    last_end: Mutex<Option<Instant>>,
}

/// Un flux vers un service, compté jusqu'à sa fin.
struct InUse(Arc<Usage>);

impl Drop for InUse {
    fn drop(&mut self) {
        *self.0.last_end.lock().expect("flux") = Some(Instant::now());
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Un tunnel ouvert vers le service d'une autre machine.
struct Open {
    peer: EndpointId,
    name: String,
    stop: CancellationToken,
    active: Arc<AtomicUsize>,
    total: Arc<AtomicU64>,
    refused: Arc<AtomicU64>,
    limit: usize,
    /// Programme seul autorisé (PID) ; `None` : tunnel non gardé (demande d'un AIODOS
    /// plus ancien).
    owner: Option<watch::Sender<Option<u32>>>,
}

/// Services exposés et tunnels ouverts de ce compagnon.
#[derive(Default)]
pub struct Tunnels {
    exposed: Mutex<HashMap<String, Service>>,
    open: Mutex<HashMap<u16, Open>>,
}

impl Tunnels {
    /// Le service `name`, s'il est exposé à cette machine.
    fn exposed_to(&self, name: &str, peer: &EndpointId) -> Option<Service> {
        let exposed = self.exposed.lock().expect("services");
        exposed.get(name).filter(|s| s.peers.contains(peer)).cloned()
    }

    /// Pour l'état : services exposés et tunnels ouverts.
    pub fn snapshot(&self) -> Value {
        let exposed = self.exposed.lock().expect("services");
        let mut services: Vec<Value> = exposed
            .iter()
            .map(|(name, s)| {
                let active = s.usage.active.load(Ordering::SeqCst);
                let last_end = *s.usage.last_end.lock().expect("flux");
                json!({
                    "name": name,
                    "port": s.port,
                    "peers": s.peers.len(),
                    "active": active,
                    "total": s.usage.total.load(Ordering::SeqCst),
                    // Secondes depuis la fin du dernier flux, s'il n'y en a plus en cours
                    "idle_s": last_end.filter(|_| active == 0).map(|at| at.elapsed().as_secs_f64()),
                    "held": s.hold.is_some(),
                    "holding": s.hold.as_ref().map(|h| h.holding()),
                    "foreign": s.hold.as_ref().map(|h| h.foreign()),
                    "blocked": s.hold.as_ref().and_then(|h| h.blocked()),
                })
            })
            .collect();
        services.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        let open = self.open.lock().expect("tunnels");
        let mut tunnels: Vec<Value> = open
            .iter()
            .map(|(port, t)| {
                json!({
                    "port": port,
                    "peer": t.peer.to_string(),
                    "name": t.name,
                    "connections": t.active.load(Ordering::SeqCst),
                    "total": t.total.load(Ordering::SeqCst),
                    "refused": t.refused.load(Ordering::SeqCst),
                    "max_connections": t.limit,
                    "guarded": t.owner.is_some(),
                    "owner_pid": t.owner.as_ref().and_then(|o| *o.borrow()),
                })
            })
            .collect();
        tunnels.sort_by_key(|t| t["port"].as_u64());
        json!({ "exposed": services, "tunnels": tunnels })
    }
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME
        && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

// ------------------------------------------------------------ côté service

#[derive(Deserialize)]
pub struct ServiceIn {
    name: String,
    port: u16,
    peers: Vec<String>,
    /// Le compagnon tient le service : lui seul y est servi (cf. hold.rs).
    #[serde(default)]
    hold: bool,
}

#[derive(Deserialize)]
pub struct ExposedIn {
    pub services: Vec<ServiceIn>,
}

/// Remplace la liste des services exposés (`PUT /net/v1/exposed`). Les tunnels déjà
/// établis vers un service retiré continuent jusqu'à leur fin, sauf ceux d'un service tenu
/// (ses connexions se ferment) : AIODOS arrête le service lui-même quand il n'en veut
/// plus. Un service gardé (même nom, même port, tenu ou non comme avant) garde ses
/// compteurs de flux et ses connexions tenues.
pub fn set_exposed(pool: &Pool, wanted: ExposedIn) -> Result<usize, String> {
    if wanted.services.len() > MAX_SERVICES {
        return Err(format!("au plus {MAX_SERVICES} services"));
    }
    // Tout est vérifié avant de tenir un nouveau service : un refus ne laisse rien derrière
    let mut checked = Vec::new();
    let mut names = HashSet::new();
    for service in wanted.services {
        if !valid_name(&service.name) {
            return Err(format!("nom de service invalide : {:?}", service.name));
        }
        if service.port == 0 {
            return Err(format!("port invalide pour {}", service.name));
        }
        if !names.insert(service.name.clone()) {
            return Err(format!("service en double : {}", service.name));
        }
        if service.hold && !hold::available() {
            return Err(format!(
                "{} : ce système ne dit pas quels programmes se connectent, le compagnon ne peut \
                 pas tenir ce service",
                service.name
            ));
        }
        let mut peers = HashSet::new();
        for key in &service.peers {
            peers.insert(key.parse::<EndpointId>().map_err(|_| format!("clé illisible : {key}"))?);
        }
        checked.push((service, peers));
    }
    let mut exposed = pool.tunnels.exposed.lock().expect("services");
    let mut next = HashMap::new();
    for (service, peers) in checked {
        let kept = exposed
            .get(&service.name)
            .filter(|s| s.port == service.port && s.hold.is_some() == service.hold);
        let usage = kept.map(|s| s.usage.clone()).unwrap_or_default();
        let hold = match kept.and_then(|s| s.hold.clone()) {
            Some(holder) => Some(holder),
            None if service.hold => Some(Holder::start(&service.name, service.port, &pool.shutdown)),
            None => None,
        };
        next.insert(service.name, Service { port: service.port, peers, usage, hold });
    }
    // Les services tenus qui ne le sont plus lâchent leurs connexions
    for (name, old) in exposed.iter() {
        if let Some(holder) = &old.hold
            && !next.get(name).and_then(|s| s.hold.as_ref()).is_some_and(|h| Arc::ptr_eq(h, holder))
        {
            holder.stop();
        }
    }
    let count = next.len();
    *exposed = next;
    Ok(count)
}

/// Où vont les octets d'un flux vers un service.
enum Upstream {
    /// Une connexion ouverte pour ce flux.
    Plain(TcpStream),
    /// Une connexion tenue par le compagnon (cf. hold.rs).
    Held(hold::Lent),
}

/// Une machine autorisée demande un tunnel vers un service (`GET /p2p/v1/tunnel/<nom>`,
/// « Upgrade ») : si le service lui est exposé et répond, l'échange devient un flux brut.
pub async fn accept(pool: &Pool, peer: EndpointId, mut req: Request<Incoming>) -> Response<Body> {
    let name = req.uri().path()[PATH_PREFIX.len()..].to_string();
    // Pris dès le début : hyper confie le flux à la mise à niveau même quand elle est
    // refusée ; un refus doit alors fermer ce flux proprement, sinon la réponse se perd.
    let upgrade = hyper::upgrade::on(&mut req);
    if req.method() != Method::GET || !wants_tunnel(req.headers()) {
        return refuse(upgrade, StatusCode::BAD_REQUEST, "Tunnel : échange « Upgrade » attendu.");
    }
    let Some(service) = pool.tunnels.exposed_to(&name, &peer) else {
        return refuse(upgrade, StatusCode::FORBIDDEN, "Ce service n'est pas ouvert à cette machine.");
    };
    let usage = service.usage.clone();
    if usage.active.fetch_add(1, Ordering::SeqCst) >= MAX_SERVICE_STREAMS {
        usage.active.fetch_sub(1, Ordering::SeqCst);
        return refuse(
            upgrade,
            StatusCode::SERVICE_UNAVAILABLE,
            "Ce service a déjà toutes les connexions qu'il accepte.",
        );
    }
    let in_use = InUse(usage.clone());
    let upstream = match &service.hold {
        Some(holder) => match holder.take().await {
            Ok(lent) => Upstream::Held(lent),
            Err(reason) => return refuse(upgrade, StatusCode::SERVICE_UNAVAILABLE, &reason),
        },
        None => match tokio::time::timeout(
            LOCAL_CONNECT_TIMEOUT,
            TcpStream::connect(("127.0.0.1", service.port)),
        )
        .await
        {
            Ok(Ok(tcp)) => {
                let _ = tcp.set_nodelay(true);
                Upstream::Plain(tcp)
            }
            _ => {
                return refuse(
                    upgrade,
                    StatusCode::BAD_GATEWAY,
                    "Le service ne répond pas sur la machine jointe.",
                );
            }
        },
    };
    usage.total.fetch_add(1, Ordering::SeqCst);
    tokio::spawn(async move {
        let _in_use = in_use;
        match (upgrade.await, upstream) {
            (Ok(upgraded), Upstream::Plain(tcp)) => splice(upgraded, tcp).await,
            (Ok(upgraded), Upstream::Held(lent)) => hold::relay(lent, upgraded).await,
            (Err(err), upstream) => {
                debug!("tunnel {name} : flux brut impossible ({err})");
                if let Upstream::Held(lent) = upstream {
                    lent.put_back(); // rien n'est passé : la connexion resservira
                }
            }
        }
    });
    let mut response = Response::new(reply::empty());
    *response.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    let headers = response.headers_mut();
    headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
    headers.insert(UPGRADE, HeaderValue::from_static(PROTOCOL));
    response
}

/// Refus lisible : la réponse part, puis le flux se ferme normalement.
fn refuse(upgrade: OnUpgrade, status: StatusCode, message: &str) -> Response<Body> {
    tokio::spawn(async move {
        if let Ok(upgraded) = upgrade.await {
            let _ = TokioIo::new(upgraded).shutdown().await;
        }
    });
    reply::detail(status, message)
}

fn wants_tunnel(headers: &HeaderMap) -> bool {
    headers
        .get(UPGRADE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case(PROTOCOL))
}

// ------------------------------------------------------------ côté client

#[derive(Deserialize)]
pub struct OpenIn {
    pub peer: String,
    pub name: String,
    /// Seul programme (PID) dont les connexions sont relayées, s'il est déjà connu.
    #[serde(default)]
    pub owner_pid: Option<u32>,
    /// Le programme sera nommé ensuite (`PUT /net/v1/tunnels/<port>`) : d'ici là, les
    /// connexions attendent.
    #[serde(default)]
    pub await_owner: bool,
    /// Connexions relayées en même temps, au plus (de 1 à `MAX_CONNECTIONS`).
    #[serde(default)]
    pub max_connections: Option<usize>,
}

#[derive(Deserialize)]
pub struct OwnerIn {
    pub owner_pid: u32,
}

/// Ouvre un port local relié au service `name` de la machine `peer`
/// (`POST /net/v1/tunnels`).
pub async fn open(pool: &Arc<Pool>, wanted: OpenIn) -> Response<Body> {
    let Ok(peer) = wanted.peer.parse::<EndpointId>() else {
        return reply::detail(StatusCode::BAD_REQUEST, "Clé de machine illisible.");
    };
    if !valid_name(&wanted.name) {
        return reply::detail(StatusCode::BAD_REQUEST, "Nom de service invalide.");
    }
    let limit = wanted.max_connections.unwrap_or(MAX_CONNECTIONS);
    if !(1..=MAX_CONNECTIONS).contains(&limit) {
        return reply::detail(
            StatusCode::BAD_REQUEST,
            format!("« max_connections » : de 1 à {MAX_CONNECTIONS}."),
        );
    }
    if wanted.owner_pid == Some(0) {
        return reply::detail(StatusCode::BAD_REQUEST, "« owner_pid » : numéro de programme invalide.");
    }
    let guarded = wanted.await_owner || wanted.owner_pid.is_some();
    if guarded && !owner::available() {
        return reply::detail(
            StatusCode::NOT_IMPLEMENTED,
            "Sur ce système, le compagnon ne peut pas savoir quel programme se connecte à un \
             tunnel : tunnel gardé impossible.",
        );
    }
    if !pool.allowlist.contains(&peer) {
        return reply::detail(StatusCode::FORBIDDEN, "Machine absente de la liste blanche du compagnon.");
    }
    let listener = match TcpListener::bind("127.0.0.1:0").await {
        Ok(listener) => listener,
        Err(err) => {
            return reply::detail(StatusCode::INTERNAL_SERVER_ERROR, format!("Port local impossible : {err}."));
        }
    };
    let local = match listener.local_addr() {
        Ok(addr) => addr,
        Err(err) => {
            return reply::detail(StatusCode::INTERNAL_SERVER_ERROR, format!("Port local illisible : {err}."));
        }
    };
    let port = local.port();
    let stop = pool.shutdown.child_token();
    let (owner, checks) = if guarded {
        let (tx, rx) = watch::channel(wanted.owner_pid);
        let (checks, queue) = mpsc::unbounded_channel();
        tokio::spawn(checker(rx, local, stop.clone(), queue));
        (Some(tx), Some(checks))
    } else {
        (None, None)
    };
    let open = Open {
        peer,
        name: wanted.name.clone(),
        stop,
        active: Arc::default(),
        total: Arc::default(),
        refused: Arc::default(),
        limit,
        owner,
    };
    let local_tunnel = Local {
        pool: pool.clone(),
        peer,
        name: wanted.name.clone(),
        stop: open.stop.clone(),
        active: open.active.clone(),
        total: open.total.clone(),
        refused: open.refused.clone(),
        checks,
        relayed: Arc::new(Semaphore::new(limit)),
        checking: Admission::new(MAX_CHECKING),
        said: Arc::default(),
    };
    pool.tunnels.open.lock().expect("tunnels").insert(port, open);
    info!(
        "Tunnel 127.0.0.1:{port} -> {} ({}){}",
        peer.fmt_short(),
        wanted.name,
        if guarded { ", réservé au programme nommé par AIODOS" } else { ", non gardé" }
    );
    tokio::spawn(local_tunnel.serve(listener));
    reply::json(
        StatusCode::OK,
        json!({ "port": port, "guarded": guarded, "max_connections": limit }),
    )
}

/// Nomme le seul programme dont un tunnel gardé relaie les connexions
/// (`PUT /net/v1/tunnels/<port>` `{"owner_pid"}`), une fois pour toutes.
pub fn set_owner(pool: &Pool, port: u16, pid: u32) -> Result<(), (StatusCode, &'static str)> {
    if pid == 0 {
        return Err((StatusCode::BAD_REQUEST, "Numéro de programme invalide."));
    }
    let open = pool.tunnels.open.lock().expect("tunnels");
    let Some(tunnel) = open.get(&port) else {
        return Err((StatusCode::NOT_FOUND, "Aucun tunnel sur ce port."));
    };
    let Some(owner) = &tunnel.owner else {
        return Err((
            StatusCode::CONFLICT,
            "Ce tunnel n'est pas gardé : il faut l'ouvrir avec « await_owner ».",
        ));
    };
    match *owner.borrow() {
        Some(current) if current != pid => {
            return Err((StatusCode::CONFLICT, "Ce tunnel est déjà réservé à un autre programme."));
        }
        _ => {}
    }
    owner.send_replace(Some(pid));
    info!("Tunnel 127.0.0.1:{port} réservé au programme {pid}");
    Ok(())
}

/// Ferme un tunnel ouvert (`DELETE /net/v1/tunnels/<port>`) : plus aucune connexion n'est
/// acceptée sur son port, et celles en cours sont coupées.
pub fn close(pool: &Pool, port: u16) -> bool {
    match pool.tunnels.open.lock().expect("tunnels").remove(&port) {
        Some(open) => {
            open.stop.cancel();
            info!("Tunnel 127.0.0.1:{port} fermé");
            true
        }
        None => false,
    }
}

/// Une connexion locale d'un tunnel gardé, qui attend le contrôle de son programme.
struct Pending {
    client: SocketAddr,
    ticket: Arc<Ticket>,
    answer: oneshot::Sender<Result<(), String>>,
}

/// Contrôle les connexions d'un tunnel gardé, par lots : une fois le programme nommé,
/// toutes celles en attente le sont d'une seule lecture de la table des connexions. Une
/// connexion prise dans un lot n'est plus écartée (cf. `MAX_CHECKING`).
async fn checker(
    mut owner: watch::Receiver<Option<u32>>,
    server: SocketAddr,
    stop: CancellationToken,
    mut queue: mpsc::UnboundedReceiver<Pending>,
) {
    // Le numéro est lu aussitôt : la valeur prêtée par `wait_for` ne passe pas d'un fil à l'autre
    let named = async move { owner.wait_for(Option::is_some).await.map(|pid| pid.unwrap_or(0)) };
    let pid = tokio::select! {
        _ = stop.cancelled() => return,
        named = named => match named {
            Ok(pid) => pid,
            Err(_) => return,
        },
    };
    loop {
        let first = tokio::select! {
            _ = stop.cancelled() => return,
            next = queue.recv() => match next {
                Some(pending) => pending,
                None => return,
            },
        };
        let mut batch = vec![first];
        while batch.len() < MAX_BATCH {
            match queue.try_recv() {
                Ok(pending) => batch.push(pending),
                Err(_) => break,
            }
        }
        // Celles déjà écartées, ou abandonnées, ne sont pas contrôlées
        batch.retain(|p| !p.ticket.evicted().is_cancelled() && !p.answer.is_closed());
        if batch.is_empty() {
            continue;
        }
        for pending in &batch {
            pending.ticket.admit();
        }
        let clients: Vec<SocketAddr> = batch.iter().map(|p| p.client).collect();
        let found = tokio::task::spawn_blocking(move || owner::connected_by_all(pid, &clients, server)).await;
        let answers: Vec<Result<(), String>> = match found {
            Ok(Ok(found)) => found
                .into_iter()
                .map(|ok| if ok { Ok(()) } else { Err(format!("connexion d'un autre programme que {pid}")) })
                .collect(),
            Ok(Err(err)) => vec![Err(format!("programme de la connexion illisible ({err})")); batch.len()],
            Err(err) => vec![Err(format!("contrôle interrompu ({err})")); batch.len()],
        };
        for (pending, answer) in batch.into_iter().zip(answers) {
            let _ = pending.answer.send(answer);
        }
    }
}

/// Le port local d'un tunnel et ce qu'il faut pour en servir les connexions.
#[derive(Clone)]
struct Local {
    pool: Arc<Pool>,
    peer: EndpointId,
    name: String,
    stop: CancellationToken,
    active: Arc<AtomicUsize>,
    total: Arc<AtomicU64>,
    refused: Arc<AtomicU64>,
    /// Tunnel gardé : file du contrôle des connexions (cf. `checker`).
    checks: Option<mpsc::UnboundedSender<Pending>>,
    relayed: Arc<Semaphore>,
    /// Places d'attente du contrôle du programme : au-delà, la plus ancienne est écartée.
    checking: Arc<Admission>,
    /// Un premier refus a été écrit au journal (les suivants, seulement en débogage).
    said: Arc<AtomicBool>,
}

impl Local {
    async fn serve(self, listener: TcpListener) {
        loop {
            let (tcp, client) = tokio::select! {
                _ = self.stop.cancelled() => break,
                accepted = listener.accept() => match accepted {
                    Ok(pair) => pair,
                    Err(err) => {
                        debug!("tunnel {} : connexion locale refusée ({err})", self.name);
                        tokio::time::sleep(Duration::from_millis(100)).await;
                        continue;
                    }
                },
            };
            // Une place d'attente du contrôle : au-delà de MAX_CHECKING, la plus ancienne
            // en attente est écartée (son jeton est annulé), pas celle qui arrive.
            let ticket = self.checking.enter();
            let evicted = ticket.evicted();
            let _ = tcp.set_nodelay(true);
            let this = self.clone();
            tokio::spawn(async move {
                let checked = tokio::select! {
                    _ = evicted.cancelled() => Err("trop de connexions en attente".to_string()),
                    result = this.check(client, ticket.clone()) => result,
                };
                if let Err(reason) = checked {
                    this.refuse(client, &reason);
                    return;
                }
                ticket.admit();                   // contrôle passé : place d'attente rendue
                let Ok(_slot) = this.relayed.clone().try_acquire_owned() else {
                    this.refuse(client, "toutes ses connexions sont déjà prises");
                    return;
                };
                this.relay(tcp).await;
            });
        }
    }

    /// Tunnel gardé : la connexion attend que le programme soit nommé (au plus
    /// `OWNER_WAIT`), puis son contrôle (cf. `checker`).
    async fn check(&self, client: SocketAddr, ticket: Arc<Ticket>) -> Result<(), String> {
        let Some(checks) = &self.checks else {
            return Ok(());
        };
        let (answer, reply) = oneshot::channel();
        if checks.send(Pending { client, ticket, answer }).is_err() {
            return Err("tunnel fermé".into());
        }
        tokio::select! {
            _ = self.stop.cancelled() => Err("tunnel fermé".into()),
            answered = tokio::time::timeout(OWNER_WAIT, reply) => match answered {
                Ok(Ok(result)) => result,
                Ok(Err(_)) => Err("tunnel fermé".into()),
                Err(_) => Err("aucun programme nommé pour ce tunnel, ou contrôle trop long".into()),
            },
        }
    }

    fn refuse(&self, client: SocketAddr, reason: &str) {
        self.refused.fetch_add(1, Ordering::SeqCst);
        if !self.said.swap(true, Ordering::SeqCst) {
            info!("Tunnel {} : connexion de {client} refusée, {reason}", self.name);
        } else {
            debug!("tunnel {} : connexion de {client} refusée, {reason}", self.name);
        }
    }

    async fn relay(&self, tcp: TcpStream) {
        self.total.fetch_add(1, Ordering::SeqCst);
        let upgraded = match dial(&self.pool, self.peer, &self.name).await {
            Ok(upgraded) => upgraded,
            Err(err) => {
                self.pool.note_error(self.peer, format!("tunnel {} : {err}", self.name));
                info!("Tunnel {} vers {} impossible : {err}", self.name, self.peer.fmt_short());
                return;
            }
        };
        self.active.fetch_add(1, Ordering::SeqCst);
        tokio::select! {
            _ = self.stop.cancelled() => {}
            _ = splice(upgraded, tcp) => {}
        }
        self.active.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Ouvre un flux vers la machine et y demande le service `name`.
async fn dial(pool: &Arc<Pool>, peer: EndpointId, name: &str) -> Result<Upgraded, String> {
    let io = outbound::open_stream(pool, peer).await?;
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(io))
        .await
        .map_err(|err| err.to_string())?;
    tokio::spawn(async move {
        if let Err(err) = conn.with_upgrades().await {
            debug!("tunnel : échange interrompu ({err})");
        }
    });
    let req = Request::builder()
        .method(Method::GET)
        .uri(format!("{PATH_PREFIX}{name}"))
        .header(HOST, "aiodos")
        .header(CONNECTION, "upgrade")
        .header(UPGRADE, PROTOCOL)
        .body(reply::empty())
        .map_err(|err| err.to_string())?;
    pool.count_out(peer);
    let response = sender.send_request(req).await.map_err(|err| err.to_string())?;
    if response.status() != StatusCode::SWITCHING_PROTOCOLS {
        return Err(format!("refusé par la machine ({})", response.status()));
    }
    hyper::upgrade::on(response).await.map_err(|err| err.to_string())
}

/// Relaie les octets dans les deux sens jusqu'à la fin des deux côtés.
async fn splice(upgraded: Upgraded, mut tcp: TcpStream) {
    let mut remote = TokioIo::new(upgraded);
    if let Err(err) = tokio::io::copy_bidirectional(&mut remote, &mut tcp).await {
        debug!("tunnel : relais interrompu ({err})");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn service_names_are_plain() {
        assert!(valid_name("rpc-3f2a"));
        assert!(!valid_name(""));
        assert!(!valid_name("RPC"));
        assert!(!valid_name("../p2p"));
        assert!(!valid_name(&"a".repeat(MAX_NAME + 1)));
    }
}
