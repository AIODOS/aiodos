//! Sortant : le mandataire HTTP local.
//!
//! `POST /peer/<clé>/p2p/v1/work/lease` part vers la machine <clé> comme
//! `POST /p2p/v1/work/lease`, dans un flux de la connexion QUIC gardée avec elle ; la
//! réponse revient telle quelle, en flux pour `/p2p/v1/stream/…` (une conversation
//! arrive morceau par morceau). Un client qui s'en va (Stop d'une conversation) coupe
//! le flux, ce qui arrête le calcul chez l'autre machine.

use std::sync::Arc;

use hyper::{
    Request, Response, StatusCode, Uri,
    body::Incoming,
    header::{HOST, HeaderValue},
};
use hyper_util::rt::TokioIo;
use iroh::EndpointId;
use tracing::debug;

use crate::{
    PEER_HEADER, TOKEN_HEADER,
    pool::{DialError, Pool},
    reply::{self, Body},
    stream_io::QuicIo,
};

enum Failure {
    Dial(DialError),
    Exchange(String),
}

/// Relaie une requête locale vers la machine `target` (sa clé), chemin `rest`.
pub async fn proxy(
    pool: &Arc<Pool>,
    mut req: Request<Incoming>,
    target: &str,
    rest: &str,
) -> Response<Body> {
    let Ok(id) = target.parse::<EndpointId>() else {
        return reply::detail(StatusCode::BAD_REQUEST, "Clé de machine illisible.");
    };
    if !pool.allowlist.contains(&id) {
        return reply::detail(
            StatusCode::FORBIDDEN,
            "Machine absente de la liste blanche du compagnon (pas appairée, ou clé pas \
             encore échangée).",
        );
    }
    if !rest.starts_with("/p2p/") {
        return reply::detail(StatusCode::NOT_FOUND, "Seule l'API /p2p/ d'AIODOS passe par le compagnon.");
    }
    let Ok(uri) = rest.parse::<Uri>() else {
        return reply::detail(StatusCode::BAD_REQUEST, "Chemin illisible.");
    };
    *req.uri_mut() = uri;
    let headers = req.headers_mut();
    reply::strip_hop_by_hop(headers);
    headers.remove(TOKEN_HEADER);
    headers.remove(PEER_HEADER);
    headers.insert(HOST, HeaderValue::from_static("aiodos"));
    pool.count_out(id);
    match exchange(pool, id, req.map(reply::boxed)).await {
        Ok(response) => {
            let mut response = response.map(reply::boxed);
            reply::strip_hop_by_hop(response.headers_mut());
            response
        }
        Err(Failure::Dial(DialError::Timeout(after))) => reply::detail(
            StatusCode::GATEWAY_TIMEOUT,
            format!("Machine injoignable par Internet (pas de réponse en {} s).", after.as_secs()),
        ),
        Err(Failure::Dial(err)) => reply::detail(
            StatusCode::BAD_GATEWAY,
            format!("Machine injoignable par Internet : {err}."),
        ),
        Err(Failure::Exchange(err)) => {
            pool.note_error(id, format!("échange interrompu : {err}"));
            reply::detail(
                StatusCode::BAD_GATEWAY,
                format!("Échange interrompu avec la machine : {err}."),
            )
        }
    }
}

/// Ouvre un flux sur la connexion gardée avec la machine, ou sur une nouvelle si elle
/// vient de mourir (réseau changé, autre machine redémarrée) : rien n'était parti, on en
/// ouvre une autre, une fois.
async fn stream(pool: &Arc<Pool>, id: EndpointId) -> Result<QuicIo, Failure> {
    let mut tracked = pool.connection(id).await.map_err(Failure::Dial)?;
    let (send, recv) = match tracked.conn.open_bi().await {
        Ok(pair) => pair,
        Err(err) => {
            debug!("flux impossible vers {} ({err}) : nouvelle connexion", id.fmt_short());
            pool.forget(&id, &tracked.conn);
            tracked = pool.connection(id).await.map_err(Failure::Dial)?;
            tracked
                .conn
                .open_bi()
                .await
                .map_err(|err| Failure::Exchange(err.to_string()))?
        }
    };
    Ok(QuicIo::new(send, recv, tracked.activity()))
}

/// Flux brut vers la machine (tunnels, cf. tunnel.rs), erreur en clair.
pub(crate) async fn open_stream(pool: &Arc<Pool>, id: EndpointId) -> Result<QuicIo, String> {
    stream(pool, id).await.map_err(|failure| match failure {
        Failure::Dial(err) => format!("machine injoignable : {err}"),
        Failure::Exchange(err) => format!("flux impossible : {err}"),
    })
}

/// Fait passer la requête dans un flux vers la machine.
async fn exchange(
    pool: &Arc<Pool>,
    id: EndpointId,
    req: Request<Body>,
) -> Result<Response<Incoming>, Failure> {
    let io = stream(pool, id).await?;
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(io))
        .await
        .map_err(|err| Failure::Exchange(err.to_string()))?;
    tokio::spawn(async move {
        if let Err(err) = conn.await {
            debug!("flux interrompu : {err}");
        }
    });
    sender
        .send_request(req)
        .await
        .map_err(|err| Failure::Exchange(err.to_string()))
}
