//! Entrant : les flux ouverts par une machine autorisée sont relayés vers le serveur
//! réseau local d'AIODOS (`p2p_app`), un échange HTTP/1.1 par flux.
//!
//! La clé de l'appelant, authentifiée par la poignée de main TLS, est ajoutée dans
//! l'en-tête `X-Aiodos-Peer` : AIODOS la compare à l'émetteur de l'enveloppe scellée,
//! et sait que la requête vient du compagnon (l'adresse qu'il voit, 127.0.0.1, n'est
//! alors pas celle de la machine). Seule l'API `/p2p/…` passe.

use std::{convert::Infallible, net::SocketAddr, sync::Arc, time::Duration};

use hyper::{
    Method, Request, Response, StatusCode,
    body::Incoming,
    header::{HOST, HeaderValue},
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
    reply::{self, Body, BoxError},
    stream_io::QuicIo,
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

/// Un flux = un échange HTTP/1.1, relayé vers AIODOS.
async fn serve_stream(pool: Arc<Pool>, peer: EndpointId, io: QuicIo) {
    let service = service_fn(move |req| {
        let pool = pool.clone();
        async move { Ok::<_, Infallible>(forward(&pool, peer, req).await) }
    });
    let mut builder = http1::Builder::new();
    builder
        .keep_alive(false)
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_TIMEOUT);
    if let Err(err) = builder.serve_connection(TokioIo::new(io), service).await {
        debug!("échange avec {} interrompu : {err}", peer.fmt_short());
    }
}

/// Relaie une requête reçue vers le serveur réseau local d'AIODOS.
async fn forward(pool: &Pool, peer: EndpointId, mut req: Request<Incoming>) -> Response<Body> {
    pool.count_in(peer);
    if !req.uri().path().starts_with("/p2p/") {
        return reply::detail(StatusCode::NOT_FOUND, "Chemin inconnu.");
    }
    if !matches!(*req.method(), Method::POST | Method::GET) {
        return reply::detail(StatusCode::METHOD_NOT_ALLOWED, "Méthode refusée.");
    }
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
    match send_local(pool.forward, req.map(reply::boxed)).await {
        Ok(response) => {
            let mut response = response.map(reply::boxed);
            reply::strip_hop_by_hop(response.headers_mut());
            response
        }
        Err(err) => {
            debug!("serveur réseau local injoignable ({}) : {err}", pool.forward);
            reply::detail(
                StatusCode::BAD_GATEWAY,
                "Le serveur réseau d'AIODOS ne répond pas sur la machine jointe.",
            )
        }
    }
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
