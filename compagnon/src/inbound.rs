//! Entrant : les flux ouverts par une machine autorisée sont relayés vers le serveur
//! réseau local d'AIODOS (`p2p_app`), un échange HTTP/1.1 par flux.
//!
//! La clé de l'appelant, authentifiée par la poignée de main TLS, est ajoutée dans
//! l'en-tête `X-Aiodos-Peer` : AIODOS la compare à l'émetteur de l'enveloppe scellée,
//! et sait que la requête vient du compagnon (l'adresse qu'il voit, 127.0.0.1, n'est
//! alors pas celle de la machine). Seule l'API `/p2p/…` passe, et les tunnels vers les
//! services qu'AIODOS a ouverts à cette machine (`/p2p/v1/tunnel/…`, cf. tunnel.rs).
//!
//! Même une machine autorisée (un ami appairé) est bornée : en-têtes attendus au plus
//! `HEADER_TIMEOUT`, corps relayé coupé au-delà de `Config::max_body` octets (un résultat
//! de calcul, au-delà de `relay_body::MAX_RESULT_BODY`), d'une minute de silence ou sous
//! le débit minimal `relay_body::PACE` (408, cf. relay_body.rs) ; réponse coupée quand la
//! machine ne la lit plus depuis `Config::response_stall` ; requêtes relayées en même temps
//! bornées par machine, conversations à part (`pool::MAX_STREAMS_PER_PEER` et
//! `pool::MAX_REQUESTS_PER_PEER` : au-delà, la requête attend sa place
//! `Config::slot_wait`, puis 503) ; flux et connexions par machine limités aussi (cf.
//! endpoint.rs et pool.rs).

use std::{convert::Infallible, net::SocketAddr, sync::Arc, time::Duration};

use hyper::{
    Method, Request, Response, StatusCode,
    body::Incoming,
    header::{CONNECTION, HOST, HeaderValue},
    server::conn::http1,
    service::service_fn,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use iroh::{
    EndpointId,
    endpoint::{ConnectionError, VarInt},
};
use tokio::net::TcpStream;
use tracing::{debug, info};

use crate::{
    PEER_HEADER, TOKEN_HEADER, close,
    pool::{Pool, Tracked},
    relay_body::{self, Progress},
    reply::{self, Body, BoxError},
    stream_io::QuicIo,
    tunnel,
};

/// Poignée de main d'une connexion entrante : au-delà, abandon.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);
/// En-têtes d'une requête attendus au plus pendant ce temps après l'ouverture du flux.
const HEADER_TIMEOUT: Duration = Duration::from_secs(60);
/// Connexion au serveur réseau local d'AIODOS.
const LOCAL_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Accepte les connexions entrantes. La liste blanche est appliquée par la poignée de
/// main (`allowlist::Gate`) : une clé inconnue n'arrive jamais jusqu'au pool.
pub async fn accept_loop(pool: Arc<Pool>) {
    loop {
        let incoming = tokio::select! {
            _ = pool.shutdown.cancelled() => break,
            incoming = pool.endpoint.accept() => match incoming {
                Some(incoming) => incoming,
                None => break,
            },
        };
        let pool = pool.clone();
        tokio::spawn(async move {
            let accepting = match incoming.accept() {
                Ok(accepting) => accepting,
                Err(err) => {
                    debug!("connexion entrante abandonnée : {err}");
                    return;
                }
            };
            match tokio::time::timeout(HANDSHAKE_TIMEOUT, accepting).await {
                Ok(Ok(conn)) => {
                    pool.register(conn);
                }
                Ok(Err(err)) => debug!("connexion entrante refusée ou échouée : {err}"),
                Err(_) => debug!("poignée de main trop longue : abandon"),
            }
        });
    }
}

/// Sert les flux que l'autre machine ouvre sur une connexion (qu'elle l'ait ouverte ou
/// non), jusqu'à sa fermeture ; puis la retire du pool.
pub(crate) async fn serve_connection(pool: Arc<Pool>, tracked: Tracked) {
    let peer = tracked.conn.remote_id();
    let reason = loop {
        let accepted = tokio::select! {
            _ = pool.shutdown.cancelled() => break None,
            accepted = tracked.conn.accept_bi() => accepted,
        };
        match accepted {
            Ok((send, recv)) => {
                if !pool.allowlist.contains(&peer) {
                    tracked.conn.close(close::REFUSED.into(), b"removed");
                    continue;
                }
                let io = QuicIo::new(send, recv, tracked.activity());
                tokio::spawn(serve_stream(pool.clone(), peer, io));
            }
            Err(err) => break Some(err),
        }
    };
    pool.forget(&peer, &tracked.conn);
    if let Some(reason) = reason {
        info!("Connexion avec {} fermée : {}", peer.fmt_short(), explain(&reason));
    }
}

/// Un flux = un échange HTTP/1.1, relayé vers AIODOS. Si la machine ne lit plus la
/// réponse (rien depuis `response_stall`), le flux est coupé : sa place et la connexion au
/// serveur local sont rendues.
async fn serve_stream(pool: Arc<Pool>, peer: EndpointId, io: QuicIo) {
    let progress = Arc::new(Progress::default());
    let stall = pool.response_stall;
    let service = {
        let progress = progress.clone();
        service_fn(move |req| {
            let (pool, progress) = (pool.clone(), progress.clone());
            async move { Ok::<_, Infallible>(forward(&pool, peer, req, progress).await) }
        })
    };
    let mut builder = http1::Builder::new();
    builder
        .keep_alive(false)
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_TIMEOUT);
    tokio::select! {
        served = builder.serve_connection(TokioIo::new(io), service).with_upgrades() => {
            if let Err(err) = served {
                debug!("échange avec {} interrompu : {err}", peer.fmt_short());
            }
        }
        _ = progress.stalled(stall) => {
            info!("Réponse non lue par {} depuis {} s : flux coupé", peer.fmt_short(), stall.as_secs());
        }
    }
}

/// Relaie une requête reçue vers le serveur réseau local d'AIODOS.
async fn forward(
    pool: &Pool,
    peer: EndpointId,
    mut req: Request<Incoming>,
    progress: Arc<Progress>,
) -> Response<Body> {
    pool.count_in(peer);
    if req.uri().path().starts_with(tunnel::PATH_PREFIX) {
        return tunnel::accept(pool, peer, req).await;
    }
    if !req.uri().path().starts_with("/p2p/") {
        return reply::detail(StatusCode::NOT_FOUND, "Chemin inconnu.");
    }
    if !matches!(*req.method(), Method::POST | Method::GET) {
        return reply::detail(StatusCode::METHOD_NOT_ALLOWED, "Méthode refusée.");
    }
    // Une place pour cette machine, tenue jusqu'à la fin de la réponse : une seule machine
    // ne prend pas toutes celles du serveur local (ni au goutte-à-goutte, ni en ne lisant
    // jamais la réponse). Les autres machines restent servies, et ses requêtes courtes ont
    // leur propre part (une rafale de conversations ne les arrête pas). Faute de place, la
    // requête attend son tour un moment avant d'être refusée.
    let Some(slot) = pool.relay_slot(peer, req.uri().path()).await else {
        return too_busy();
    };
    let headers = req.headers_mut();
    reply::strip_hop_by_hop(headers);
    headers.remove(TOKEN_HEADER);
    headers.insert(
        PEER_HEADER,
        HeaderValue::from_str(&peer.to_string()).expect("clé en hexadécimal"),
    );
    headers.insert(
        HOST,
        HeaderValue::from_str(&pool.forward.to_string()).expect("adresse locale"),
    );
    let path = req.uri().path().to_owned();
    let max = relay_body::limit_for(&path, pool.max_body);
    if relay_body::announced_too_large(req.headers(), max) {
        return too_large(max);
    }
    // Débit minimal en moyenne, pour toutes, résultats de calcul compris : un
    // goutte-à-goutte ne tient pas une place sans fin (BODY_IDLE se remet à zéro à chaque
    // octet), et une grosse requête sur un lien lent a le temps qu'il lui faut.
    let req = req.map(|body| relay_body::bounded(body, max, relay_body::BODY_IDLE, Some(relay_body::PACE)));
    match send_local(pool.forward, req).await {
        Ok(response) => {
            let mut response = response.map(reply::boxed);
            reply::strip_hop_by_hop(response.headers_mut());
            // La place n'est rendue qu'une fois la réponse entièrement renvoyée (ou le flux
            // coupé, cf. serve_stream).
            response.map(|body| relay_body::guarded(slot, progress, body))
        }
        Err(err) if relay_body::is_too_large(err.as_ref()) => too_large(max),
        Err(err) if relay_body::is_too_slow(err.as_ref()) => too_slow(),
        Err(err) => {
            debug!("serveur réseau local injoignable ({}) : {err}", pool.forward);
            reply::detail(
                StatusCode::BAD_GATEWAY,
                "Le serveur réseau d'AIODOS ne répond pas sur la machine jointe.",
            )
        }
    }
}

/// Trop de requêtes en cours pour cette machine, depuis `Config::slot_wait` : refusée, flux
/// fermé. Les autres machines gardent leurs places.
fn too_busy() -> Response<Body> {
    let mut response = reply::detail(
        StatusCode::SERVICE_UNAVAILABLE,
        "Trop de requêtes en même temps depuis cette machine : réessaie dans un instant.",
    );
    response.headers_mut().insert(CONNECTION, HeaderValue::from_static("close"));
    response
}

/// Corps arrivé trop lentement (muet, ou sous le débit minimal) : refusé, flux fermé.
fn too_slow() -> Response<Body> {
    let mut response = reply::detail(
        StatusCode::REQUEST_TIMEOUT,
        format!(
            "Envoi trop lent : la requête n'est pas arrivée à temps (plus rien pendant {} s, ou \
             moins de {} Kio/s en moyenne). Réessaie avec une meilleure connexion.",
            relay_body::BODY_IDLE.as_secs(),
            relay_body::PACE.min_rate / 1024
        ),
    );
    response.headers_mut().insert(CONNECTION, HeaderValue::from_static("close"));
    response
}

/// Corps trop long : refusé, et le flux fermé (le reste du corps n'est pas lu).
fn too_large(max: usize) -> Response<Body> {
    let mut response = reply::detail(
        StatusCode::PAYLOAD_TOO_LARGE,
        format!("Requête trop grosse pour être relayée (au plus {} Mio).", max >> 20),
    );
    response.headers_mut().insert(CONNECTION, HeaderValue::from_static("close"));
    response
}

async fn send_local(addr: SocketAddr, req: Request<Body>) -> Result<Response<Incoming>, BoxError> {
    let tcp = tokio::time::timeout(LOCAL_CONNECT_TIMEOUT, TcpStream::connect(addr)).await??;
    tcp.set_nodelay(true)?;
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(tcp)).await?;
    tokio::spawn(async move {
        if let Err(err) = conn.await {
            debug!("liaison locale interrompue : {err}");
        }
    });
    Ok(sender.send_request(req).await?)
}

/// Raison de fermeture d'une connexion, pour le journal.
pub(crate) fn explain(reason: &ConnectionError) -> String {
    match reason {
        ConnectionError::ApplicationClosed(app) => {
            let code = app.error_code;
            let what = if code == VarInt::from(close::STOPPING) {
                "arrêt de l'autre compagnon"
            } else if code == VarInt::from(close::DUPLICATE) {
                "connexion en double"
            } else if code == VarInt::from(close::REFUSED) {
                "refusée (clé non autorisée)"
            } else {
                "fermée par l'autre machine"
            };
            what.to_string()
        }
        ConnectionError::LocallyClosed => "fermée ici".into(),
        ConnectionError::TimedOut => "plus de nouvelles de l'autre machine".into(),
        ConnectionError::Reset => "réinitialisée par l'autre machine".into(),
        other => other.to_string(),
    }
}
