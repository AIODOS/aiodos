//! API locale : contrôle du compagnon (`/net/v1/…`) et mandataire (`/peer/<clé>/…`).
//!
//! Elle n'écoute que sur 127.0.0.1 et exige le jeton qu'AIODOS donne au lancement
//! (en-tête `X-Aiodos-Net-Token`) : une page web ouverte dans le navigateur ne peut ni
//! lire l'état, ni changer la liste blanche, ni se servir du tunnel.
//!
//! - `GET  /net/v1/status`   : clé publique, relais, connexions (directes ou relayées, latence)
//! - `PUT  /net/v1/peers`    : liste blanche complète `{"peers": [{"id", "addrs", "relays"} | {"ticket"}]}`
//! - `GET  /net/v1/ticket`   : ticket à donner à une autre machine (clé, relais, adresses)
//! - `POST /net/v1/shutdown` : arrêt propre
//! - `*    /peer/<clé>/…`    : requête relayée vers cette machine (cf. outbound.rs)
//! - `PUT  /net/v1/exposed`  : services locaux ouverts à des machines nommées (cf. tunnel.rs)
//! - `POST /net/v1/tunnels`  : port local relié au service d'une machine ; `DELETE
//!   /net/v1/tunnels/<port>` le ferme
//! - `GET  /net/v1/public`   : réseau public (clé du nœud, voisins, nœuds vus) ; `PUT` le
//!   rejoint ou le quitte, avec le profil à diffuser (cf. public.rs)

use std::{
    convert::Infallible,
    sync::{Arc, atomic::AtomicU64},
    time::{Duration, Instant},
};

use http_body_util::{BodyExt, Limited};
use hyper::{
    HeaderMap, Method, Request, Response, StatusCode, body::Incoming, server::conn::http1,
    service::service_fn,
};
use hyper_util::rt::TokioIo;
use serde::Deserialize;
use serde_json::json;
use tokio::net::TcpListener;
use tracing::{debug, info, warn};

use crate::{
    TOKEN_HEADER,
    allowlist::{self, Entry},
    config::{Config, Relays},
    outbound,
    pool::Pool,
    public::{Public, Wanted},
    reply::{self, Body},
    status, tunnel,
};

/// Taille maximale de la liste blanche reçue.
const MAX_PEERS_BODY: usize = 1 << 20;

/// Ce que l'API locale partage avec le reste du compagnon.
pub struct Ctx {
    pub pool: Arc<Pool>,
    pub config: Config,
    pub started: Instant,
    pub network_changes: Arc<AtomicU64>,
    pub public: Arc<Public>,
}

/// Sert l'API locale jusqu'à l'arrêt du compagnon.
pub async fn serve(listener: TcpListener, ctx: Arc<Ctx>) {
    loop {
        let accepted = tokio::select! {
            _ = ctx.pool.shutdown.cancelled() => break,
            accepted = listener.accept() => accepted,
        };
        let (tcp, remote) = match accepted {
            Ok(pair) => pair,
            Err(err) => {
                warn!("API locale : connexion refusée ({err})");
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            }
        };
        if !remote.ip().is_loopback() {
            continue;
        }
        let _ = tcp.set_nodelay(true);
        let ctx = ctx.clone();
        tokio::spawn(async move {
            let service = service_fn(move |req| {
                let ctx = ctx.clone();
                async move { Ok::<_, Infallible>(route(&ctx, req).await) }
            });
            if let Err(err) = http1::Builder::new()
                .serve_connection(TokioIo::new(tcp), service)
                .await
            {
                debug!("API locale : échange interrompu ({err})");
            }
        });
    }
}

async fn route(ctx: &Ctx, req: Request<Incoming>) -> Response<Body> {
    if !authorized(&ctx.config.token, req.headers()) {
        return reply::detail(StatusCode::UNAUTHORIZED, "Jeton du compagnon manquant ou incorrect.");
    }
    let path = req.uri().path().to_string();
    if let Some(rest) = path.strip_prefix("/peer/") {
        let (target, tail) = rest.split_once('/').unwrap_or((rest, ""));
        let query = req.uri().query().map(|q| format!("?{q}")).unwrap_or_default();
        let tail = format!("/{tail}{query}");
        let target = target.to_string();
        return outbound::proxy(&ctx.pool, req, &target, &tail).await;
    }
    match (req.method(), path.as_str()) {
        (&Method::GET, "/net/v1/status") => reply::json(StatusCode::OK, status::snapshot(ctx)),
        (&Method::GET, "/net/v1/ticket") => {
            wait_for_relay(ctx).await;
            reply::json(StatusCode::OK, status::ticket(ctx))
        }
        (&Method::PUT, "/net/v1/peers") => set_peers(ctx, req).await,
        (&Method::GET, "/net/v1/public") => reply::json(StatusCode::OK, ctx.public.snapshot()),
        (&Method::PUT, "/net/v1/public") => match read_json::<Wanted>(req).await {
            Ok(wanted) => reply::json(StatusCode::OK, ctx.public.configure(wanted).await),
            Err(response) => response,
        },
        (&Method::POST, "/net/v1/shutdown") => {
            info!("Arrêt demandé par AIODOS");
            ctx.pool.shutdown.cancel();
            reply::json(StatusCode::OK, json!({ "ok": true }))
        }
        (&Method::PUT, "/net/v1/exposed") => match read_json::<tunnel::ExposedIn>(req).await {
            Ok(wanted) => match tunnel::set_exposed(&ctx.pool, wanted) {
                Ok(count) => reply::json(StatusCode::OK, json!({ "ok": true, "exposed": count })),
                Err(err) => reply::detail(StatusCode::BAD_REQUEST, format!("Services refusés : {err}.")),
            },
            Err(response) => response,
        },
        (&Method::POST, "/net/v1/tunnels") => match read_json::<tunnel::OpenIn>(req).await {
            Ok(wanted) => tunnel::open(&ctx.pool, wanted).await,
            Err(response) => response,
        },
        (&Method::DELETE, path) if path.starts_with("/net/v1/tunnels/") => {
            match path["/net/v1/tunnels/".len()..].parse::<u16>() {
                Ok(port) if tunnel::close(&ctx.pool, port) => {
                    reply::json(StatusCode::OK, json!({ "ok": true }))
                }
                _ => reply::detail(StatusCode::NOT_FOUND, "Aucun tunnel sur ce port."),
            }
        }
        _ => reply::detail(StatusCode::NOT_FOUND, "Chemin inconnu."),
    }
}

/// Corps JSON d'une requête de l'API locale, ou la réponse d'erreur à renvoyer.
async fn read_json<T: serde::de::DeserializeOwned>(req: Request<Incoming>) -> Result<T, Response<Body>> {
    let body = match Limited::new(req.into_body(), MAX_PEERS_BODY).collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => return Err(reply::detail(StatusCode::PAYLOAD_TOO_LARGE, "Requête trop longue.")),
    };
    serde_json::from_slice(&body)
        .map_err(|err| reply::detail(StatusCode::BAD_REQUEST, format!("Requête illisible : {err}.")))
}

/// Juste après le démarrage, Iroh n'a pas encore joint son relais : un ticket donné alors
/// (invitation, réponse à une invitation) n'aurait que la clé, et sans annuaire (relais
/// auto-hébergé sans ceux de n0) personne ne pourrait joindre cette machine. Le ticket
/// attend donc le relais, mais pas au-delà de ce délai après le démarrage : ensuite, il
/// répond aussitôt (relais qui refuse cette clé, pas de réseau). AIODOS attend 5 s une
/// réponse de l'API locale.
const TICKET_GRACE: Duration = Duration::from_secs(4);

async fn wait_for_relay(ctx: &Ctx) {
    let left = TICKET_GRACE.saturating_sub(ctx.started.elapsed());
    if ctx.config.relays != Relays::Disabled && !left.is_zero() {
        let _ = tokio::time::timeout(left, ctx.pool.endpoint.online()).await;
    }
}

#[derive(Deserialize)]
struct PeersIn {
    peers: Vec<Entry>,
}

/// Remplace la liste blanche ; ferme les connexions des machines retirées.
async fn set_peers(ctx: &Ctx, req: Request<Incoming>) -> Response<Body> {
    let body = match Limited::new(req.into_body(), MAX_PEERS_BODY).collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(_) => return reply::detail(StatusCode::PAYLOAD_TOO_LARGE, "Liste trop longue."),
    };
    let parsed: PeersIn = match serde_json::from_slice(&body) {
        Ok(parsed) => parsed,
        Err(err) => return reply::detail(StatusCode::BAD_REQUEST, format!("Liste illisible : {err}.")),
    };
    let entries = match allowlist::parse_entries(parsed.peers) {
        Ok(entries) => entries,
        Err(err) => return reply::detail(StatusCode::BAD_REQUEST, format!("Liste refusée : {err}.")),
    };
    let allowed = entries.len();
    let before = ctx.pool.allowlist.ids();
    let removed = ctx.pool.allowlist.replace(entries);
    ctx.pool.close_peers(&removed);
    if before != ctx.pool.allowlist.ids() {
        info!("Liste blanche : {allowed} machine(s) autorisée(s)");
    }
    reply::json(StatusCode::OK, json!({ "ok": true, "allowed": allowed, "removed": removed.len() }))
}

/// Comparaison du jeton en temps constant.
fn authorized(token: &str, headers: &HeaderMap) -> bool {
    let Some(given) = headers.get(TOKEN_HEADER) else {
        return false;
    };
    let (given, token) = (given.as_bytes(), token.as_bytes());
    if given.len() != token.len() || token.is_empty() {
        return false;
    }
    given.iter().zip(token).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

#[cfg(test)]
mod tests {
    use hyper::header::HeaderValue;

    use super::*;

    #[test]
    fn token_must_match_exactly() {
        let mut headers = HeaderMap::new();
        assert!(!authorized("secret", &headers));
        headers.insert(TOKEN_HEADER, HeaderValue::from_static("secreT"));
        assert!(!authorized("secret", &headers));
        headers.insert(TOKEN_HEADER, HeaderValue::from_static("secret"));
        assert!(authorized("secret", &headers));
        assert!(!authorized("", &headers));
    }
}
