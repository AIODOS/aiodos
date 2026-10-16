//! Deux compagnons dans le même processus, relais et annuaires coupés : connexion
//! directe sur 127.0.0.1. Un faux serveur réseau d'AIODOS (`p2p_app`) répond derrière
//! le compagnon B.

use std::{
    convert::Infallible,
    net::SocketAddr,
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use aiodos_net::{Config, Running, start};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
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

const TOKEN: &str = "jeton-de-test";
const LIMIT: Duration = Duration::from_secs(20);

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

type AppBody = http_body_util::combinators::UnsyncBoxBody<Bytes, Infallible>;

/// Faux serveur réseau d'AIODOS :
/// - `/p2p/v1/echo` renvoie le chemin, le corps et les en-têtes vus ;
/// - `/p2p/v1/stream/slow` répond « un », puis « deux » quand le test le permet.
async fn fake_app(release: Arc<Notify>) -> SocketAddr {
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
    if path == "/p2p/v1/stream/slow" {
        let (tx, rx) = mpsc::channel(4);
        tokio::spawn(async move {
            tx.send(Bytes::from_static(b"un\n")).await.unwrap();
            release.notified().await;
            let _ = tx.send(Bytes::from_static(b"deux\n")).await;
        });
        return Response::new(ChannelBody(rx).boxed_unsync());
    }
    let body = req.into_body().collect().await.unwrap().to_bytes();
    let reply = json!({
        "path": path,
        "peer": peer,
        "token": token,
        "body": String::from_utf8_lossy(&body),
    });
    Response::new(Full::new(Bytes::from(reply.to_string())).boxed_unsync())
}

// ------------------------------------------------------------ compagnons

struct Node {
    running: Running,
    _dir: tempfile::TempDir,
}

impl Node {
    async fn start(forward: SocketAddr) -> Node {
        let dir = tempfile::tempdir().unwrap();
        let running = start(Config::local(dir.path().to_path_buf(), forward, TOKEN))
            .await
            .unwrap();
        Node { running, _dir: dir }
    }

    /// Arrêt puis relance sur le même dossier (même clé) et le même port UDP.
    async fn restart(self, forward: SocketAddr) -> Node {
        let Node { running, _dir: dir } = self;
        let port = running.bound[0];
        running.stop().await;
        let mut config = Config::local(dir.path().to_path_buf(), forward, TOKEN);
        config.bind = Some(port);
        let running = start(config).await.unwrap();
        Node { running, _dir: dir }
    }

    fn id(&self) -> String {
        self.running.id.to_string()
    }

    /// Entrée de liste blanche pour joindre ce compagnon (avec ou sans ses adresses).
    fn entry(&self, with_addrs: bool) -> Value {
        let addrs: Vec<String> = if with_addrs {
            self.running.bound.iter().map(|a| a.to_string()).collect()
        } else {
            vec![]
        };
        json!({ "id": self.id(), "addrs": addrs })
    }

    async fn allow(&self, entries: Vec<Value>) {
        let (status, body) = call(self, Method::PUT, "/net/v1/peers", json!({ "peers": entries })).await;
        assert_eq!(status, StatusCode::OK, "{body}");
    }

    async fn status(&self) -> Value {
        let (status, body) = call(self, Method::GET, "/net/v1/status", Value::Null).await;
        assert_eq!(status, StatusCode::OK);
        body
    }

    async fn peer_status(&self, other: &Node) -> Value {
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

async fn send(
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

async fn call(node: &Node, method: Method, path: &str, body: Value) -> (StatusCode, Value) {
    let text = if body.is_null() { String::new() } else { body.to_string() };
    let response = send(node.running.listen, method, path, Some(TOKEN), text).await;
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

/// Requête de A vers la machine `to`, par le mandataire de A.
async fn via(from: &Node, to: &Node, path: &str, body: &str) -> (StatusCode, Value) {
    let path = format!("/peer/{}{path}", to.id());
    let response = send(from.running.listen, Method::POST, &path, Some(TOKEN), body.into()).await;
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

/// Deux compagnons qui s'autorisent l'un l'autre, B devant un faux p2p_app.
async fn pair_of_nodes() -> (Node, Node, Arc<Notify>) {
    let release = Arc::new(Notify::new());
    let app_b = fake_app(release.clone()).await;
    let app_a = fake_app(Arc::new(Notify::new())).await;
    let a = Node::start(app_a).await;
    let b = Node::start(app_b).await;
    a.allow(vec![b.entry(true)]).await;
    b.allow(vec![a.entry(true)]).await;
    (a, b, release)
}

// ------------------------------------------------------------ tests

#[tokio::test]
async fn requests_reach_aiodos_through_a_single_connection() {
    let (a, b, _) = pair_of_nodes().await;
    for i in 0..5 {
        let (status, reply) = via(&a, &b, "/p2p/v1/echo?n=1", &format!("enveloppe {i}")).await;
        assert_eq!(status, StatusCode::OK, "{reply}");
        assert_eq!(reply["path"], "/p2p/v1/echo?n=1");
        assert_eq!(reply["body"], format!("enveloppe {i}"));
        // AIODOS reçoit la clé authentifiée de l'appelant, jamais le jeton local
        assert_eq!(reply["peer"], a.id());
        assert_eq!(reply["token"], Value::Null);
    }
    let seen_by_a = a.peer_status(&b).await;
    assert_eq!(seen_by_a["connected"], true);
    assert_eq!(seen_by_a["connections"], 1);
    assert_eq!(seen_by_a["dials"], 1, "une seule connexion pour cinq requêtes");
    assert_eq!(seen_by_a["path"], "direct");
    assert_eq!(seen_by_a["requests_out"], 5);
    assert!(seen_by_a["rtt_ms"].as_f64().is_some());
    assert_eq!(b.peer_status(&a).await["requests_in"], 5);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn streamed_responses_arrive_piece_by_piece() {
    let (a, b, release) = pair_of_nodes().await;
    let path = format!("/peer/{}/p2p/v1/stream/slow", b.id());
    let response = send(a.running.listen, Method::POST, &path, Some(TOKEN), "{}".into()).await;
    assert_eq!(response.status(), StatusCode::OK);
    let mut body = response.into_body();
    let mut first = Vec::new();
    // Le premier morceau arrive alors que le serveur n'a pas fini sa réponse
    while !first.ends_with(b"\n") {
        let frame = timeout(LIMIT, body.frame()).await.expect("premier morceau").unwrap().unwrap();
        first.extend_from_slice(&frame.into_data().unwrap());
    }
    assert_eq!(first, b"un\n");
    release.notify_one();
    let rest = timeout(LIMIT, body.collect()).await.expect("fin du flux").unwrap().to_bytes();
    assert_eq!(&rest[..], b"deux\n");
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn the_connection_serves_both_directions() {
    // B ne connaît pas les adresses de A (téléphone en 4G, injoignable) : il ne peut
    // que répondre sur la connexion ouverte par A, et s'en servir pour ses requêtes.
    let release = Arc::new(Notify::new());
    let a = Node::start(fake_app(release.clone()).await).await;
    let b = Node::start(fake_app(release).await).await;
    a.allow(vec![b.entry(true)]).await;
    b.allow(vec![a.entry(false)]).await;
    let (status, _) = via(&a, &b, "/p2p/v1/echo", "ping").await;
    assert_eq!(status, StatusCode::OK);
    let (status, reply) = via(&b, &a, "/p2p/v1/echo", "pong").await;
    assert_eq!(status, StatusCode::OK, "{reply}");
    assert_eq!(reply["peer"], b.id());
    let seen_by_b = b.peer_status(&a).await;
    assert_eq!(seen_by_b["dials"], 0, "B n'a ouvert aucune connexion");
    assert_eq!(seen_by_b["direction"], "in");
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn unknown_keys_are_refused_at_the_handshake() {
    let app = fake_app(Arc::new(Notify::new())).await;
    let a = Node::start(app).await;
    let b = Node::start(app).await;
    let stranger = Node::start(app).await;
    a.allow(vec![b.entry(true)]).await;
    b.allow(vec![]).await; // B n'a pas (encore) appairé A
    let (status, reply) = via(&a, &b, "/p2p/v1/echo", "ping").await;
    assert!(status.is_server_error(), "{status} {reply}");
    assert!(b.status().await["refused"]["count"].as_u64().unwrap() >= 1);
    // Une machine absente de la liste blanche n'est pas composée du tout
    let (status, _) = via(&a, &stranger, "/p2p/v1/echo", "ping").await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    for node in [a, b, stranger] {
        node.running.stop().await;
    }
}

#[tokio::test]
async fn a_removed_machine_loses_its_connection() {
    let (a, b, _) = pair_of_nodes().await;
    assert_eq!(via(&a, &b, "/p2p/v1/echo", "1").await.0, StatusCode::OK);
    b.allow(vec![]).await;
    // La connexion est fermée par B ; une nouvelle est refusée à la poignée de main
    let mut refused = false;
    for _ in 0..20 {
        if via(&a, &b, "/p2p/v1/echo", "2").await.0 != StatusCode::OK {
            refused = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(refused);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn a_restarted_machine_is_joined_again() {
    let (a, b, _) = pair_of_nodes().await;
    assert_eq!(via(&a, &b, "/p2p/v1/echo", "avant").await.0, StatusCode::OK);
    let b_id = b.id();
    let b = b.restart(fake_app(Arc::new(Notify::new())).await).await;
    assert_eq!(b.id(), b_id, "même clé après la relance");
    b.allow(vec![a.entry(true)]).await;
    // A apprend la fermeture de l'ancienne connexion et en ouvre une autre, seul
    let mut joined = false;
    for _ in 0..50 {
        if via(&a, &b, "/p2p/v1/echo", "après").await.0 == StatusCode::OK {
            joined = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(joined);
    let seen_by_a = a.peer_status(&b).await;
    assert_eq!(seen_by_a["dials"], 2);
    assert_eq!(seen_by_a["connections"], 1);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn the_local_api_requires_the_token_and_only_relays_p2p_paths() {
    let (a, b, _) = pair_of_nodes().await;
    let listen = a.running.listen;
    let no_token = send(listen, Method::GET, "/net/v1/status", None, String::new()).await;
    assert_eq!(no_token.status(), StatusCode::UNAUTHORIZED);
    let wrong = send(listen, Method::GET, "/net/v1/status", Some("autre"), String::new()).await;
    assert_eq!(wrong.status(), StatusCode::UNAUTHORIZED);
    let path = format!("/peer/{}/p2p/v1/echo", b.id());
    let proxied_without_token = send(listen, Method::POST, &path, None, String::new()).await;
    assert_eq!(proxied_without_token.status(), StatusCode::UNAUTHORIZED);
    let (status, _) = via(&a, &b, "/api/settings", "{}").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, ticket) = call(&a, Method::GET, "/net/v1/ticket", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    assert!(ticket["ticket"].as_str().unwrap().starts_with("endpoint"));
    assert_eq!(ticket["id"], a.id());
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn shutdown_through_the_local_api() {
    let (a, b, _) = pair_of_nodes().await;
    let stop = a.running.shutdown_token();
    let (status, _) = call(&a, Method::POST, "/net/v1/shutdown", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    timeout(LIMIT, stop.cancelled()).await.expect("arrêt demandé");
    a.running.stop().await;
    b.running.stop().await;
}
