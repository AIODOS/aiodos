//! Tunnels : un service TCP local d'une machine rendu joignable par une autre machine
//! autorisée, à travers la connexion QUIC déjà gardée avec elle. Premier usage : le
//! serveur RPC de llama.cpp, pour répartir un gros modèle entre ses machines
//! (docs/gros-modeles.md).
//!
//! - **côté service** (la machine qui prête) : AIODOS déclare ses services exposés,
//!   `PUT /net/v1/exposed` `{"services": [{"name", "port", "peers": [clé…]}]}` ; le
//!   service n'écoute que sur 127.0.0.1 ;
//! - **côté client** (la machine qui emprunte) : `POST /net/v1/tunnels` `{"peer", "name"}`
//!   ouvre un port local (127.0.0.1) ; chaque connexion à ce port devient un flux QUIC vers
//!   la machine, ouvert par un échange HTTP/1.1 `GET /p2p/v1/tunnel/<nom>` avec
//!   « Upgrade », puis relayé octet par octet vers le service.
//!   `DELETE /net/v1/tunnels/<port>` le ferme.
//!
//! Chiffrement et authentification sont ceux de la connexion (QUIC, clés Iroh) : seules les
//! machines qu'AIODOS a nommées pour ce service le joignent, jamais le réseau.

use std::{
    collections::{HashMap, HashSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
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
};
use tokio_util::sync::CancellationToken;
use tracing::{debug, info};

use crate::{
    outbound,
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

/// Un service exposé : son port local et les machines qui peuvent le joindre.
#[derive(Debug, Clone)]
struct Service {
    port: u16,
    peers: HashSet<EndpointId>,
}

/// Un tunnel ouvert vers le service d'une autre machine.
struct Open {
    peer: EndpointId,
    name: String,
    stop: CancellationToken,
    active: Arc<AtomicUsize>,
    total: Arc<AtomicU64>,
}

/// Services exposés et tunnels ouverts de ce compagnon.
#[derive(Default)]
pub struct Tunnels {
    exposed: Mutex<HashMap<String, Service>>,
    open: Mutex<HashMap<u16, Open>>,
}

impl Tunnels {
    /// Port du service `name`, s'il est exposé à cette machine.
    fn exposed_to(&self, name: &str, peer: &EndpointId) -> Option<u16> {
        let exposed = self.exposed.lock().expect("services");
        exposed.get(name).filter(|s| s.peers.contains(peer)).map(|s| s.port)
    }

    /// Pour l'état : services exposés et tunnels ouverts.
    pub fn snapshot(&self) -> Value {
        let exposed = self.exposed.lock().expect("services");
        let mut services: Vec<Value> = exposed
            .iter()
            .map(|(name, s)| json!({ "name": name, "port": s.port, "peers": s.peers.len() }))
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
}

#[derive(Deserialize)]
pub struct ExposedIn {
    pub services: Vec<ServiceIn>,
}

/// Remplace la liste des services exposés (`PUT /net/v1/exposed`). Les tunnels déjà
/// établis vers un service retiré continuent jusqu'à leur fin : AIODOS arrête le service
/// lui-même quand il n'en veut plus.
pub fn set_exposed(pool: &Pool, wanted: ExposedIn) -> Result<usize, String> {
    if wanted.services.len() > MAX_SERVICES {
        return Err(format!("au plus {MAX_SERVICES} services"));
    }
    let mut next = HashMap::new();
    for service in wanted.services {
        if !valid_name(&service.name) {
            return Err(format!("nom de service invalide : {:?}", service.name));
        }
        if service.port == 0 {
            return Err(format!("port invalide pour {}", service.name));
        }
        let mut peers = HashSet::new();
        for key in &service.peers {
            peers.insert(key.parse::<EndpointId>().map_err(|_| format!("clé illisible : {key}"))?);
        }
        if next.insert(service.name.clone(), Service { port: service.port, peers }).is_some() {
            return Err(format!("service en double : {}", service.name));
        }
    }
    let count = next.len();
    *pool.tunnels.exposed.lock().expect("services") = next;
    Ok(count)
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
    let Some(port) = pool.tunnels.exposed_to(&name, &peer) else {
        return refuse(upgrade, StatusCode::FORBIDDEN, "Ce service n'est pas ouvert à cette machine.");
    };
    let local = match tokio::time::timeout(
        LOCAL_CONNECT_TIMEOUT,
        TcpStream::connect(("127.0.0.1", port)),
    )
    .await
    {
        Ok(Ok(tcp)) => tcp,
        _ => {
            return refuse(
                upgrade,
                StatusCode::BAD_GATEWAY,
                "Le service ne répond pas sur la machine jointe.",
            );
        }
    };
    let _ = local.set_nodelay(true);
    tokio::spawn(async move {
        match upgrade.await {
            Ok(upgraded) => splice(upgraded, local).await,
            Err(err) => debug!("tunnel {name} : flux brut impossible ({err})"),
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
    if !pool.allowlist.contains(&peer) {
        return reply::detail(StatusCode::FORBIDDEN, "Machine absente de la liste blanche du compagnon.");
    }
    let listener = match TcpListener::bind("127.0.0.1:0").await {
        Ok(listener) => listener,
        Err(err) => {
            return reply::detail(StatusCode::INTERNAL_SERVER_ERROR, format!("Port local impossible : {err}."));
        }
    };
    let port = match listener.local_addr() {
        Ok(addr) => addr.port(),
        Err(err) => {
            return reply::detail(StatusCode::INTERNAL_SERVER_ERROR, format!("Port local illisible : {err}."));
        }
    };
    let open = Open {
        peer,
        name: wanted.name.clone(),
        stop: pool.shutdown.child_token(),
        active: Arc::default(),
        total: Arc::default(),
    };
    let (stop, active, total) = (open.stop.clone(), open.active.clone(), open.total.clone());
    pool.tunnels.open.lock().expect("tunnels").insert(port, open);
    info!("Tunnel 127.0.0.1:{port} -> {} ({})", peer.fmt_short(), wanted.name);
    tokio::spawn(serve_local(pool.clone(), listener, peer, wanted.name, stop, active, total));
    reply::json(StatusCode::OK, json!({ "port": port }))
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

async fn serve_local(
    pool: Arc<Pool>,
    listener: TcpListener,
    peer: EndpointId,
    name: String,
    stop: CancellationToken,
    active: Arc<AtomicUsize>,
    total: Arc<AtomicU64>,
) {
    loop {
        let (tcp, _) = tokio::select! {
            _ = stop.cancelled() => break,
            accepted = listener.accept() => match accepted {
                Ok(pair) => pair,
                Err(err) => {
                    debug!("tunnel {name} : connexion locale refusée ({err})");
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    continue;
                }
            },
        };
        let _ = tcp.set_nodelay(true);
        let (pool, name, stop, active) = (pool.clone(), name.clone(), stop.clone(), active.clone());
        total.fetch_add(1, Ordering::SeqCst);
        tokio::spawn(async move {
            let upgraded = match dial(&pool, peer, &name).await {
                Ok(upgraded) => upgraded,
                Err(err) => {
                    pool.note_error(peer, format!("tunnel {name} : {err}"));
                    info!("Tunnel {name} vers {} impossible : {err}", peer.fmt_short());
                    return;
                }
            };
            active.fetch_add(1, Ordering::SeqCst);
            tokio::select! {
                _ = stop.cancelled() => {}
                _ = splice(upgraded, tcp) => {}
            }
            active.fetch_sub(1, Ordering::SeqCst);
        });
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
