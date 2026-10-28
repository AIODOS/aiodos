//! Outils partagés des essais à plusieurs compagnons dans le même processus : un faux
//! serveur réseau d'AIODOS (`p2p_app`) derrière chaque compagnon, et de quoi parler à
//! l'API locale et au mandataire d'un compagnon.

#![allow(dead_code)] // chaque fichier d'essai n'utilise qu'une partie de ces outils

use std::{
    convert::Infallible,
    net::SocketAddr,
    path::Path,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use aiodos_net::{Config, Running, config::Relays, start};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use iroh::{RelayUrl, address_lookup::MemoryLookup};
use hyper::{
    Method, Request, Response, StatusCode,
    body::{Frame, Incoming},
    server::conn::http1,
    service::service_fn,
};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::{Notify, mpsc},
    time::timeout,
};

pub const TOKEN: &str = "jeton-de-test";
pub const LIMIT: Duration = Duration::from_secs(20);

// ------------------------------------------------------------ faux p2p_app

/// Corps de réponse alimenté morceau par morceau.
struct ChannelBody(mpsc::Receiver<Bytes>);

impl hyper::body::Body for ChannelBody {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        self.0.poll_recv(cx).map(|chunk| chunk.map(|b| Ok(Frame::data(b))))
    }
}

/// Réponse de `left` octets nuls, morceau par morceau, faite à mesure qu'elle est lue.
struct Zeros(usize);

impl hyper::body::Body for Zeros {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        if self.0 == 0 {
            return Poll::Ready(None);
        }
        let size = self.0.min(64 * 1024);
        self.0 -= size;
        Poll::Ready(Some(Ok(Frame::data(Bytes::from(vec![0u8; size])))))
    }
}

type AppBody = http_body_util::combinators::UnsyncBoxBody<Bytes, Infallible>;

/// Faux serveur réseau d'AIODOS :
/// - `/p2p/v1/echo` (et tout autre chemin) renvoie le chemin, le corps et les en-têtes vus ;
/// - `/p2p/v1/stream/slow` et `/public/v1/stream/slow` répondent « un », puis « deux » quand
///   le test le permet ;
/// - `/p2p/v1/stream/big` répond 256 Mio, faits à mesure qu'ils sont lus ;
/// - `/public/v1/refused` est refusé aussitôt (400 `bad_request`, comme une demande mal
///   formée).
pub async fn fake_app(release: Arc<Notify>) -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let (tcp, _) = listener.accept().await.unwrap();
            let release = release.clone();
            tokio::spawn(async move {
                let service = service_fn(move |req: Request<Incoming>| {
                    let release = release.clone();
                    async move { Ok::<_, Infallible>(answer(req, release).await) }
                });
                let _ = http1::Builder::new().serve_connection(TokioIo::new(tcp), service).await;
            });
        }
    });
    addr
}

async fn answer(req: Request<Incoming>, release: Arc<Notify>) -> Response<AppBody> {
    let path = req.uri().to_string();
    let header = |name: &str| {
        req.headers().get(name).map(|v| v.to_str().unwrap_or("?").to_string())
    };
    let (peer, token) = (header("x-aiodos-peer"), header("x-aiodos-net-token"));
    let seen = json!({
        "public": header("x-aiodos-public"),
        "conversation": header("x-aiodos-conversation"),
        "user_agent": header("user-agent"),
        "forwarded_for": header("x-forwarded-for"),
        "content_type": header("content-type"),
    });
    if path == "/p2p/v1/stream/slow" || path == "/public/v1/stream/slow" {
        let (tx, rx) = mpsc::channel(4);
        tokio::spawn(async move {
            tx.send(Bytes::from_static(b"un\n")).await.unwrap();
            release.notified().await;
            let _ = tx.send(Bytes::from_static(b"deux\n")).await;
        });
        return Response::new(ChannelBody(rx).boxed_unsync());
    }
    if path == "/p2p/v1/stream/big" {
        return Response::new(Zeros(256 << 20).boxed_unsync());
    }
    if path == "/public/v1/refused" {
        let refusal = Bytes::from_static(br#"{"error": "bad_request"}"#);
        let mut response = Response::new(Full::new(refusal).boxed_unsync());
        *response.status_mut() = StatusCode::BAD_REQUEST;
        return response;
    }
    let body = req.into_body().collect().await.unwrap().to_bytes();
    let mut reply = json!({
        "path": path,
        "peer": peer,
        "token": token,
        "body": String::from_utf8_lossy(&body),
    });
    reply.as_object_mut().unwrap().extend(seen.as_object().unwrap().clone());
    Response::new(Full::new(Bytes::from(reply.to_string())).boxed_unsync())
}

// ------------------------------------------------------------ compagnons

pub struct Node {
    pub running: Running,
    pub dir: tempfile::TempDir,
}

impl Node {
    /// Compagnon d'essai local : relais et annuaires coupés, direct sur 127.0.0.1.
    pub async fn start(forward: SocketAddr) -> Node {
        Node::start_with(tempfile::tempdir().unwrap(), |dir| {
            Config::local(dir.to_path_buf(), forward, TOKEN)
        })
        .await
    }

    /// Compagnon lancé sur ce dossier (clé déjà créée ou non), avec les réglages donnés.
    pub async fn start_with(dir: tempfile::TempDir, config: impl FnOnce(&Path) -> Config) -> Node {
        let running = start(config(dir.path())).await.unwrap();
        Node { running, dir }
    }

    /// Arrêt puis relance sur le même dossier (même clé) et le même port UDP.
    pub async fn restart(self, forward: SocketAddr) -> Node {
        let Node { running, dir } = self;
        let port = running.bound[0];
        running.stop().await;
        let mut config = Config::local(dir.path().to_path_buf(), forward, TOKEN);
        config.bind = Some(port);
        let running = start(config).await.unwrap();
        Node { running, dir }
    }

    pub fn id(&self) -> String {
        self.running.id.to_string()
    }

    /// Entrée de liste blanche pour joindre ce compagnon (avec ou sans ses adresses).
    pub fn entry(&self, with_addrs: bool) -> Value {
        let addrs: Vec<String> = if with_addrs {
            self.running.bound.iter().map(|a| a.to_string()).collect()
        } else {
            vec![]
        };
        json!({ "id": self.id(), "addrs": addrs })
    }

    pub async fn allow(&self, entries: Vec<Value>) {
        let (status, body) = call(self, Method::PUT, "/net/v1/peers", json!({ "peers": entries })).await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }

    pub async fn status(&self) -> Value {
        let (status, body) = call(self, Method::GET, "/net/v1/status", Value::Null).await;
        assert_eq!(status, StatusCode::OK);
        body
    }

    pub async fn peer_status(&self, other: &Node) -> Value {
        let status = self.status().await;
        status["peers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["id"] == other.id())
            .cloned()
            .unwrap_or(Value::Null)
    }
}

pub async fn send(
    listen: SocketAddr,
    method: Method,
    path: &str,
    token: Option<&str>,
    body: String,
) -> Response<Incoming> {
    let tcp = TcpStream::connect(listen).await.unwrap();
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(tcp)).await.unwrap();
    tokio::spawn(conn);
    let mut request = Request::builder().method(method).uri(path).header("host", "127.0.0.1");
    if let Some(token) = token {
        request = request.header("x-aiodos-net-token", token);
    }
    let request = request.body(Full::new(Bytes::from(body))).unwrap();
    timeout(LIMIT, sender.send_request(request)).await.expect("délai").unwrap()
}

pub async fn call(node: &Node, method: Method, path: &str, body: Value) -> (StatusCode, Value) {
    let text = if body.is_null() { String::new() } else { body.to_string() };
    let response = send(node.running.listen, method, path, Some(TOKEN), text).await;
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

/// Requête de A vers la machine `to`, par le mandataire de A.
pub async fn via(from: &Node, to: &Node, path: &str, body: &str) -> (StatusCode, Value) {
    let path = format!("/peer/{}{path}", to.id());
    let response = send(from.running.listen, Method::POST, &path, Some(TOKEN), body.into()).await;
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

// ------------------------------------------------------------ réseau public

/// Compagnon d'essai du réseau public (DHT locale, annuaire en mémoire), devant un faux
/// AIODOS ; par ce relais s'il est donné (le nœud public est alors en relais seulement).
pub async fn public_node(
    bootstrap: &[String],
    lookup: &MemoryLookup,
    release: Arc<Notify>,
    relay: Option<&RelayUrl>,
) -> Node {
    let app = fake_app(release).await;
    let (bootstrap, lookup, relay) = (bootstrap.to_vec(), lookup.clone(), relay.cloned());
    Node::start_with(tempfile::tempdir().unwrap(), move |dir| {
        let mut config = Config::local(dir.to_path_buf(), app, TOKEN);
        config.public_dht_bootstrap = Some(bootstrap);
        config.public_lookup = Some(lookup);
        config.dial_timeout = Duration::from_secs(5);
        if let Some(url) = relay {
            config.relays = Relays::Custom(vec![url]);
        }
        config
    })
    .await
}

/// Rejoint le réseau public en prêtant (ou non) ; → la clé publique du nœud.
pub async fn lend(node: &Node, lends: bool) -> String {
    let wanted = json!({
        "enabled": true, "version": "2.1.0", "models": ["qwen3.5:9b"], "lends": lends, "free": 1, "tps": 30,
    });
    let (status, body) = call(node, Method::PUT, "/net/v1/public", wanted).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["error"].is_null(), "{body}");
    body["key"].as_str().unwrap().to_string()
}

/// Deux compagnons qui s'autorisent l'un l'autre, B devant un faux p2p_app.
pub async fn pair_of_nodes() -> (Node, Node, Arc<Notify>) {
    let release = Arc::new(Notify::new());
    let app_b = fake_app(release.clone()).await;
    let app_a = fake_app(Arc::new(Notify::new())).await;
    let a = Node::start(app_a).await;
    let b = Node::start(app_b).await;
    a.allow(vec![b.entry(true)]).await;
    b.allow(vec![a.entry(true)]).await;
    (a, b, release)
}
