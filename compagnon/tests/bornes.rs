//! Bornes et gardes contre les programmes locaux et les machines trop gourmandes (audit de
//! sécurité du 20/10) : tunnel gardé, qui ne relaie que le programme nommé par AIODOS, même
//! sous une avalanche de connexions d'un intrus ; service tenu, qui ne sert jamais un autre
//! programme que le compagnon ; connexions d'un tunnel et flux d'un service bornés ; corps
//! relayés bornés ; réponses jamais lues coupées ; API locale qui ne garde ni les
//! connexions muettes ni celles refusées. Deux compagnons dans le même processus, en direct
//! sur 127.0.0.1 (cf. common/mod.rs) ; l'intrus d'une inondation est un autre programme
//! (ce même exécutable, relancé sur `flood_helper`).

mod common;

use std::{
    convert::Infallible,
    net::SocketAddr,
    pin::Pin,
    process::{Command, Stdio},
    sync::Arc,
    task::{Context, Poll},
    time::{Duration, Instant},
};

use aiodos_net::{Config, admission, control, pool, tunnel};
use bytes::Bytes;
use common::{LIMIT, Node, TOKEN, call, fake_app, pair_of_nodes, send};
use http_body_util::BodyExt;
use hyper::{
    Method, Request, StatusCode,
    body::{Body, Frame},
};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpSocket, TcpStream},
    sync::Notify,
    time::timeout,
};

/// Service d'essai : renvoie tout ce qu'il reçoit (comme le serveur RPC répond).
async fn echo_service() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let (mut tcp, _) = listener.accept().await.unwrap();
            tokio::spawn(async move {
                let (mut read, mut write) = tcp.split();
                let _ = tokio::io::copy(&mut read, &mut write).await;
            });
        }
    });
    port
}

async fn expose(node: &Node, name: &str, port: u16, peer: &Node) {
    let services = json!([{ "name": name, "port": port, "peers": [peer.id()] }]);
    let (status, body) = call(node, Method::PUT, "/net/v1/exposed", json!({ "services": services })).await;
    assert_eq!(status, StatusCode::OK, "{body}");
}

async fn open(node: &Node, wanted: Value) -> Value {
    let (status, body) = call(node, Method::POST, "/net/v1/tunnels", wanted).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    body
}

/// Une connexion qui envoie `data` et relit autant d'octets.
async fn echoed(tcp: &mut TcpStream, data: &[u8]) -> bool {
    if tcp.write_all(data).await.is_err() {
        return false;
    }
    let mut got = vec![0; data.len()];
    matches!(timeout(LIMIT, tcp.read_exact(&mut got)).await, Ok(Ok(_))) && got == data
}

/// La connexion est fermée sans rien recevoir.
async fn closed_without_answer(tcp: &mut TcpStream) -> bool {
    let _ = tcp.write_all(b"ping").await;
    let mut buf = [0u8; 16];
    matches!(timeout(LIMIT, tcp.read(&mut buf)).await, Ok(Ok(0)) | Ok(Err(_)))
}

async fn connect(port: u16) -> TcpStream {
    TcpStream::connect(("127.0.0.1", port)).await.unwrap()
}

fn tunnel_status(status: &Value, port: u16) -> Value {
    status["tunnels"]["tunnels"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["port"] == port)
        .cloned()
        .unwrap_or(Value::Null)
}

// ------------------------------------------------------------ tunnel gardé

#[tokio::test]
async fn a_guarded_tunnel_relays_only_the_program_named_by_aiodos() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    expose(&b, "rpc-garde", service, &a).await;
    assert!(a.status().await["features"].as_array().unwrap().contains(&json!("tunnel-owner")));

    // Tunnel gardé, programme pas encore nommé : la connexion attend, sans être relayée
    let opened = open(&a, json!({ "peer": b.id(), "name": "rpc-garde", "await_owner": true })).await;
    assert_eq!(opened["guarded"], true);
    let port = opened["port"].as_u64().unwrap() as u16;
    let mut early = connect(port).await;
    early.write_all(b"avant").await.unwrap();
    let mut got = [0u8; 5];
    assert!(
        timeout(Duration::from_millis(500), early.read_exact(&mut got)).await.is_err(),
        "rien ne passe avant que le programme soit nommé"
    );
    // Ce programme-ci est nommé : la connexion en attente passe, et les suivantes aussi
    let me = std::process::id();
    let path = format!("/net/v1/tunnels/{port}");
    let (status, body) = call(&a, Method::PUT, &path, json!({ "owner_pid": me })).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    timeout(LIMIT, early.read_exact(&mut got)).await.expect("relayée").unwrap();
    assert_eq!(&got, b"avant");
    assert!(echoed(&mut connect(port).await, b"apres").await);
    let seen = tunnel_status(&a.status().await, port);
    assert_eq!((seen["guarded"].clone(), seen["owner_pid"].clone()), (json!(true), json!(me)));
    // Un autre programme ne peut pas prendre sa place
    let (status, _) = call(&a, Method::PUT, &path, json!({ "owner_pid": me + 1 })).await;
    assert_eq!(status, StatusCode::CONFLICT);

    // Tunnel réservé à un autre programme : les connexions de celui-ci sont fermées
    let opened = open(&a, json!({ "peer": b.id(), "name": "rpc-garde", "owner_pid": me + 1 })).await;
    let other = opened["port"].as_u64().unwrap() as u16;
    assert!(closed_without_answer(&mut connect(other).await).await);
    assert!(tunnel_status(&a.status().await, other)["refused"].as_u64().unwrap() >= 1);
    assert_eq!(b.status().await["tunnels"]["exposed"][0]["total"], 2, "rien n'a atteint B");

    // Un tunnel non gardé ne se fait pas réserver après coup ; un port inconnu non plus
    let plain = open(&a, json!({ "peer": b.id(), "name": "rpc-garde" })).await;
    assert_eq!(plain["guarded"], false);
    let plain_path = format!("/net/v1/tunnels/{}", plain["port"]);
    let (status, _) = call(&a, Method::PUT, &plain_path, json!({ "owner_pid": me })).await;
    assert_eq!(status, StatusCode::CONFLICT);
    let (status, _) = call(&a, Method::PUT, "/net/v1/tunnels/1", json!({ "owner_pid": me })).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn a_tunnel_relays_a_bounded_number_of_connections() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    expose(&b, "rpc-borne", service, &a).await;
    for bad in [0, tunnel::MAX_CONNECTIONS + 1] {
        let wanted = json!({ "peer": b.id(), "name": "rpc-borne", "max_connections": bad });
        let (status, _) = call(&a, Method::POST, "/net/v1/tunnels", wanted).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
    let opened = open(&a, json!({ "peer": b.id(), "name": "rpc-borne", "max_connections": 1 })).await;
    let port = opened["port"].as_u64().unwrap() as u16;
    let mut first = connect(port).await;
    assert!(echoed(&mut first, b"un").await);
    assert!(closed_without_answer(&mut connect(port).await).await, "une seule à la fois");
    drop(first);
    // La place libérée, une nouvelle connexion passe
    let mut passed = false;
    for _ in 0..50 {
        if echoed(&mut connect(port).await, b"deux").await {
            passed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(passed);
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ côté service

#[tokio::test]
async fn the_lending_side_sees_when_the_tunnel_drops() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    expose(&b, "rpc-suivi", service, &a).await;
    let exposed = |status: Value| status["tunnels"]["exposed"][0].clone();
    assert_eq!(exposed(b.status().await)["total"], 0);
    let port = open(&a, json!({ "peer": b.id(), "name": "rpc-suivi" })).await["port"].as_u64().unwrap() as u16;
    let mut tcp = connect(port).await;
    assert!(echoed(&mut tcp, b"llama").await);
    let seen = exposed(b.status().await);
    assert_eq!((seen["active"].clone(), seen["total"].clone()), (json!(1), json!(1)));
    assert_eq!(seen["idle_s"], Value::Null, "un flux est en cours");
    drop(tcp);
    let mut dropped = Value::Null;
    for _ in 0..50 {
        dropped = exposed(b.status().await);
        if dropped["active"] == 0 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(dropped["active"], 0);
    assert!(dropped["idle_s"].as_f64().is_some(), "{dropped}");
    // La liste des services renvoyée telle quelle (un autre prêt commence) : compteurs gardés
    expose(&b, "rpc-suivi", service, &a).await;
    assert_eq!(exposed(b.status().await)["total"], 1);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn a_service_accepts_a_bounded_number_of_streams() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    expose(&b, "rpc-plein", service, &a).await;
    let ports: Vec<u16> = {
        let mut ports = Vec::new();
        for _ in 0..2 {
            let opened = open(&a, json!({ "peer": b.id(), "name": "rpc-plein" })).await;
            ports.push(opened["port"].as_u64().unwrap() as u16);
        }
        ports
    };
    let mut held = Vec::new();
    for i in 0..tunnel::MAX_SERVICE_STREAMS {
        let mut tcp = connect(ports[i % 2]).await;
        assert!(echoed(&mut tcp, b"place").await, "connexion {i}");
        held.push(tcp);
    }
    assert!(closed_without_answer(&mut connect(ports[0]).await).await, "service plein");
    let message = a.peer_status(&b).await["last_error"]["message"].as_str().unwrap_or_default().to_string();
    assert!(message.contains("refusé par la machine"), "{message}");
    drop(held);
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ corps relayés

/// Corps envoyé en morceaux, sans longueur annoncée.
struct Chunked {
    left: usize,
}

impl Body for Chunked {
    type Data = Bytes;
    type Error = std::convert::Infallible;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        if self.left == 0 {
            return Poll::Ready(None);
        }
        let size = self.left.min(64 * 1024);
        self.left -= size;
        Poll::Ready(Some(Ok(Frame::data(Bytes::from(vec![b'x'; size])))))
    }
}

#[tokio::test]
async fn a_machine_cannot_send_an_oversized_body() {
    let release = Arc::new(Notify::new());
    let a = Node::start(fake_app(release.clone()).await).await;
    let b_app = fake_app(release).await;
    let b = Node::start_with(tempfile::tempdir().unwrap(), |dir| {
        let mut config = Config::local(dir.to_path_buf(), b_app, TOKEN);
        config.max_body = 1 << 20;
        config
    })
    .await;
    a.allow(vec![b.entry(true)]).await;
    b.allow(vec![a.entry(true)]).await;
    let path = format!("/peer/{}/p2p/v1/echo", b.id());
    let small = send(a.running.listen, Method::POST, &path, Some(TOKEN), "x".repeat(1000)).await;
    assert_eq!(small.status(), StatusCode::OK);
    // Longueur annoncée trop grande : refusée tout de suite
    let big = send(a.running.listen, Method::POST, &path, Some(TOKEN), "x".repeat(2 << 20)).await;
    assert_eq!(big.status(), StatusCode::PAYLOAD_TOO_LARGE);
    // Sans longueur annoncée : coupée en route
    let tcp = TcpStream::connect(a.running.listen).await.unwrap();
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(tcp)).await.unwrap();
    tokio::spawn(conn);
    let request = Request::builder()
        .method(Method::POST)
        .uri(&path)
        .header("host", "127.0.0.1")
        .header("x-aiodos-net-token", TOKEN)
        .body(Chunked { left: 3 << 20 })
        .unwrap();
    // Le refus peut se perdre : la connexion est fermée pendant que le reste du corps arrive
    // encore, et Windows la réinitialise alors (la réponse lue ou non selon le moment)
    match timeout(LIMIT, sender.send_request(request)).await.expect("réponse") {
        Ok(response) => {
            assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
            let text = response.into_body().collect().await.unwrap().to_bytes();
            assert!(String::from_utf8_lossy(&text).contains("trop grosse"));
        }
        Err(err) => assert!(!err.is_parse() && !err.is_user(), "{err}"),
    }
    // Un résultat de calcul peut porter une vidéo : sa borne à lui (celle d'AIODOS)
    let result = format!("/peer/{}/p2p/v1/work/result", b.id());
    let video = send(a.running.listen, Method::POST, &result, Some(TOKEN), "x".repeat(2 << 20)).await;
    assert_eq!(video.status(), StatusCode::OK);
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ API locale

/// Lit jusqu'à la fermeture de la connexion (au plus `within`) : vrai si elle se ferme.
async fn closes_within(tcp: &mut TcpStream, within: Duration) -> bool {
    let mut buf = vec![0u8; 4096];
    let read_all = async {
        loop {
            match tcp.read(&mut buf).await {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
        }
    };
    timeout(within, read_all).await.is_ok()
}

#[tokio::test]
async fn the_local_api_drops_refused_and_silent_connections() {
    let node = Node::start(fake_app(Arc::new(Notify::new())).await).await;
    let listen = node.running.listen;
    // Refus sans jeton : la réponse part, puis la connexion se ferme
    let mut refused = TcpStream::connect(listen).await.unwrap();
    refused.write_all(b"GET /net/v1/status HTTP/1.1\r\nhost: x\r\n\r\n").await.unwrap();
    assert!(closes_within(&mut refused, Duration::from_secs(5)).await, "fermée après le refus");
    // Connexion muette : fermée une fois le délai des en-têtes passé
    let mut silent = TcpStream::connect(listen).await.unwrap();
    assert!(closes_within(&mut silent, control::HEADER_TIMEOUT + Duration::from_secs(5)).await);
    // Trop de connexions sans jeton : la plus ancienne laisse sa place, AIODOS passe toujours
    let mut crowd = Vec::new();
    for _ in 0..=admission::MAX_WAITING {
        crowd.push(TcpStream::connect(listen).await.unwrap());
    }
    assert!(closes_within(&mut crowd[0], Duration::from_secs(5)).await, "la plus ancienne sort");
    let (status, _) = call(&node, Method::GET, "/net/v1/status", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    drop(crowd);
    node.running.stop().await;
}

// ------------------------------------------------------------ requêtes relayées bornées par pair

/// Un corps de requête qui ne se termine jamais : la requête relayée reste en cours, et tient
/// sa place.
struct NeverEnds;

impl Body for NeverEnds {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(
        self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
        Poll::Pending
    }

    fn is_end_stream(&self) -> bool {
        false
    }
}

/// Ouvre vers `path` une requête dont le corps ne finit pas (elle tient une place), sans revenir.
async fn hold_request(listen: SocketAddr, path: String) {
    let Ok(tcp) = TcpStream::connect(listen).await else {
        return;
    };
    let Ok((mut sender, conn)) = hyper::client::conn::http1::handshake(TokioIo::new(tcp)).await else {
        return;
    };
    tokio::spawn(conn);
    let request = Request::builder()
        .method(Method::POST)
        .uri(&path)
        .header("host", "127.0.0.1")
        .header("x-aiodos-net-token", TOKEN)
        .body(NeverEnds)
        .unwrap();
    let _ = sender.send_request(request).await; // ne revient pas (corps sans fin)
}

/// B, dont une requête attend sa place au plus `slot_wait`.
async fn waiting_node(app: SocketAddr, slot_wait: Duration) -> Node {
    Node::start_with(tempfile::tempdir().unwrap(), |dir| {
        let mut config = Config::local(dir.to_path_buf(), app, TOKEN);
        config.slot_wait = slot_wait;
        config
    })
    .await
}

/// Attend que B ait reçu (compté) au moins `count` requêtes de A.
async fn received(b: &Node, a: &Node, count: usize) {
    let mut held = 0;
    for _ in 0..200 {
        held = b.peer_status(a).await["requests_in"].as_u64().unwrap_or(0);
        if held >= count as u64 {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("B n'a reçu que {held} requêtes de A sur {count}");
}

#[tokio::test]
async fn one_machine_cannot_take_every_relay_slot() {
    let b = waiting_node(fake_app(Arc::new(Notify::new())).await, Duration::from_millis(300)).await;
    let a = Node::start(fake_app(Arc::new(Notify::new())).await).await;
    let c = Node::start(fake_app(Arc::new(Notify::new())).await).await;
    b.allow(vec![a.entry(true), c.entry(true)]).await;
    a.allow(vec![b.entry(true)]).await;
    c.allow(vec![b.entry(true)]).await;
    let path = format!("/peer/{}/p2p/v1/echo", b.id());
    // A ouvre autant de requêtes courtes « sans fin » qu'il a de places
    for _ in 0..pool::MAX_REQUESTS_PER_PEER {
        tokio::spawn(hold_request(a.running.listen, path.clone()));
    }
    received(&b, &a, pool::MAX_REQUESTS_PER_PEER).await;
    // La suivante attend sa place, puis est refusée (503)…
    let busy = send(a.running.listen, Method::POST, &path, Some(TOKEN), "x".into()).await;
    assert_eq!(busy.status(), StatusCode::SERVICE_UNAVAILABLE, "A a pris toutes ses places");
    // … mais une autre machine (C) reste servie : les places sont par machine
    let served = send(c.running.listen, Method::POST, &path, Some(TOKEN), "bonjour".into()).await;
    assert_eq!(served.status(), StatusCode::OK, "une autre machine est toujours servie");
    a.running.stop().await;
    b.running.stop().await;
    c.running.stop().await;
}

#[tokio::test]
async fn a_burst_of_conversations_waits_its_turn_and_short_requests_still_pass() {
    // Relecture finale du 21/10/2026 : 40 conversations envoyées ensemble vers un modèle
    // prêté, et 21 échouaient aussitôt (« Trop de requêtes… ») ; pendant ce temps, plus rien
    // d'autre ne passait (ping, baux de travail). La conversation en trop attend sa place,
    // et les requêtes courtes ont la leur.
    let release = Arc::new(Notify::new());
    let b = waiting_node(fake_app(release.clone()).await, Duration::from_secs(10)).await;
    let a = Node::start(fake_app(Arc::new(Notify::new())).await).await;
    b.allow(vec![a.entry(true)]).await;
    a.allow(vec![b.entry(true)]).await;
    let slow = format!("/peer/{}/p2p/v1/stream/slow", b.id());
    // Autant de conversations qu'il y a de places : chacune répond « un », puis attend
    let (started, mut answered) = tokio::sync::mpsc::unbounded_channel();
    let mut first = Vec::new();
    for _ in 0..pool::MAX_STREAMS_PER_PEER {
        let (listen, slow, started) = (a.running.listen, slow.clone(), started.clone());
        first.push(tokio::spawn(async move {
            let response = send(listen, Method::POST, &slow, Some(TOKEN), String::new()).await;
            assert_eq!(response.status(), StatusCode::OK);
            started.send(()).unwrap();
            response.into_body().collect().await.unwrap().to_bytes()
        }));
    }
    for _ in 0..pool::MAX_STREAMS_PER_PEER {
        timeout(LIMIT, answered.recv()).await.expect("toutes ont leur place").unwrap();
    }
    // Une de plus : elle attend son tour, sans être refusée
    let (listen, extra_path) = (a.running.listen, slow.clone());
    let extra = tokio::spawn(async move {
        send(listen, Method::POST, &extra_path, Some(TOKEN), String::new()).await.status()
    });
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(!extra.is_finished(), "la conversation en trop attend sa place");
    // Les requêtes courtes passent pendant ce temps
    let (status, echoed) = common::via(&a, &b, "/p2p/v1/echo", "ping").await;
    assert_eq!(status, StatusCode::OK, "{echoed}");
    // Les conversations finissent : la place se libère, celle qui attendait est servie
    release.notify_waiters();
    for done in first {
        assert_eq!(&timeout(LIMIT, done).await.unwrap().unwrap()[..], b"un\ndeux\n");
    }
    assert_eq!(timeout(LIMIT, extra).await.unwrap().unwrap(), StatusCode::OK);
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ places d'attente d'un tunnel gardé

#[tokio::test]
async fn the_oldest_pending_tunnel_connection_is_evicted_not_the_newest() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    expose(&b, "rpc-attente", service, &a).await;
    // Tunnel gardé dont le programme n'est pas encore nommé : les connexions attendent le contrôle
    let opened = open(&a, json!({ "peer": b.id(), "name": "rpc-attente", "await_owner": true })).await;
    let port = opened["port"].as_u64().unwrap() as u16;
    let mut pending = Vec::new();
    for _ in 0..tunnel::MAX_CHECKING {
        pending.push(connect(port).await);
        tokio::time::sleep(Duration::from_millis(2)).await; // ordre d'arrivée net
    }
    tokio::time::sleep(Duration::from_millis(500)).await; // le compagnon les prend toutes
    // Une connexion de plus : la plus ancienne en attente est écartée, pas la nouvelle
    let mut late = connect(port).await;
    assert!(
        closes_within(&mut pending[0], Duration::from_secs(3)).await,
        "la plus ancienne en attente est écartée"
    );
    assert!(
        !closes_within(&mut late, Duration::from_millis(800)).await,
        "la nouvelle attend le programme nommé, elle n'est pas refusée d'emblée"
    );
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ inondation d'un tunnel gardé

/// Variable qui fait de `flood_helper` un intrus : le port du tunnel à inonder.
const FLOOD_PORT: &str = "AIODOS_ESSAI_INONDATION";

/// Intrus lancé à part (ce même exécutable) : beaucoup de connexions en même temps vers le
/// port donné, chacune rouverte dès que le compagnon l'a refusée, quelques secondes. Sans
/// la variable, ne fait rien.
#[test]
fn flood_helper() {
    let Ok(port) = std::env::var(FLOOD_PORT) else {
        return;
    };
    let port: u16 = port.parse().unwrap();
    let end = Instant::now() + Duration::from_secs(8);
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let threads: Vec<_> = (0..48)
        .map(|_| {
            std::thread::spawn(move || {
                use std::io::Read;
                while Instant::now() < end {
                    let Ok(mut tcp) = std::net::TcpStream::connect_timeout(&addr, Duration::from_secs(1)) else {
                        std::thread::sleep(Duration::from_millis(5));
                        continue;
                    };
                    let _ = tcp.set_read_timeout(Some(Duration::from_secs(2)));
                    let _ = tcp.read(&mut [0u8; 16]); // jusqu'au refus du compagnon
                }
            })
        })
        .collect();
    for thread in threads {
        let _ = thread.join();
    }
}

#[tokio::test]
async fn a_flood_of_local_connections_does_not_shut_out_the_named_program() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    expose(&b, "rpc-inonde", service, &a).await;
    let me = std::process::id();
    let opened = open(&a, json!({ "peer": b.id(), "name": "rpc-inonde", "owner_pid": me })).await;
    let port = opened["port"].as_u64().unwrap() as u16;
    let mut intruder = Command::new(std::env::current_exe().unwrap())
        .args(["flood_helper", "--exact", "--nocapture"])
        .env(FLOOD_PORT, port.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    // L'inondation bat son plein : ses connexions sont refusées par dizaines à la seconde
    let mut flooded = false;
    for _ in 0..100 {
        if tunnel_status(&a.status().await, port)["refused"].as_u64().unwrap_or(0) >= 200 {
            flooded = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    // Pendant ce temps, les connexions du programme nommé passent toutes
    let mut passed = 0;
    for i in 0..20 {
        if let Ok(mut tcp) = TcpStream::connect(("127.0.0.1", port)).await
            && echoed(&mut tcp, format!("llama {i}").as_bytes()).await
        {
            passed += 1;
        }
    }
    let refused = tunnel_status(&a.status().await, port)["refused"].as_u64().unwrap_or(0);
    let _ = intruder.kill();
    let _ = intruder.wait();
    assert!(flooded, "l'intrus a bien inondé le tunnel ({refused} refus)");
    assert_eq!(passed, 20, "connexions du programme nommé relayées sous l'inondation ({refused} refus)");
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ service tenu (machine qui prête)

/// Service d'essai qui sert un client à la fois, dans l'ordre d'arrivée, jusqu'à ce qu'il
/// ferme, et lui renvoie ce qu'il reçoit : comme le serveur RPC de llama.cpp.
async fn serial_service() -> u16 {
    let socket = TcpSocket::new_v4().unwrap();
    socket.bind(SocketAddr::from(([127, 0, 0, 1], 0))).unwrap();
    let port = socket.local_addr().unwrap().port();
    let listener = socket.listen(16).unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((mut tcp, _)) = listener.accept().await else { return };
            let (mut read, mut write) = tcp.split();
            let _ = tokio::io::copy(&mut read, &mut write).await;
        }
    });
    port
}

fn exposed_status(status: &Value, name: &str) -> Value {
    status["tunnels"]["exposed"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == name)
        .cloned()
        .unwrap_or(Value::Null)
}

#[tokio::test]
async fn a_held_service_never_serves_another_program() {
    let (a, b, _) = pair_of_nodes().await;
    assert!(b.status().await["features"].as_array().unwrap().contains(&json!("service-hold")));
    let service = serial_service().await;
    let services = json!([{ "name": "rpc-tenu", "port": service, "peers": [a.id()], "hold": true }]);
    let (status, body) = call(&b, Method::PUT, "/net/v1/exposed", json!({ "services": services })).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let port = open(&a, json!({ "peer": b.id(), "name": "rpc-tenu" })).await["port"].as_u64().unwrap() as u16;

    // Les connexions brèves du démarrage de llama-server, l'une après l'autre : servies
    for word in ["un", "deux", "trois"] {
        let mut tcp = connect(port).await;
        assert!(echoed(&mut tcp, word.as_bytes()).await, "{word}");
    }
    let seen = exposed_status(&b.status().await, "rpc-tenu");
    assert_eq!(seen["held"], true);
    assert!(seen["holding"].as_u64().unwrap() >= 1, "{seen}");
    assert_eq!(seen["blocked"], Value::Null, "{seen}");

    // La connexion durable de llama-server, en cours…
    let mut llama = connect(port).await;
    assert!(echoed(&mut llama, b"modele").await);
    // … quand un autre programme de la machine se connecte au serveur, sans passer par le tunnel
    let mut intruder = connect(service).await;
    intruder.write_all(b"intrus").await.unwrap();
    let mut blocked = Value::Null;
    for _ in 0..100 {
        blocked = exposed_status(&b.status().await, "rpc-tenu");
        if blocked["blocked"].is_string() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(blocked["blocked"].as_str().unwrap_or_default().contains("autre programme"), "{blocked}");
    assert!(blocked["foreign"].as_u64().unwrap() >= 1, "{blocked}");
    // llama-server a fini : le compagnon ne rend pas sa connexion, l'intrus n'est jamais servi
    drop(llama);
    let mut got = [0u8; 6];
    assert!(
        timeout(Duration::from_secs(3), intruder.read_exact(&mut got)).await.is_err(),
        "l'intrus n'est jamais servi"
    );
    // Plus aucun flux vers ce service (AIODOS arrête alors le serveur)
    assert!(closed_without_answer(&mut connect(port).await).await);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn a_held_service_lets_go_when_it_is_withdrawn() {
    let (a, b, _) = pair_of_nodes().await;
    let service = serial_service().await;
    let held = json!([{ "name": "rpc-rendu", "port": service, "peers": [a.id()], "hold": true }]);
    let (status, _) = call(&b, Method::PUT, "/net/v1/exposed", json!({ "services": held })).await;
    assert_eq!(status, StatusCode::OK);
    // Le compagnon tient le serveur : une connexion directe attend (sans être servie)
    let mut waiting = connect(service).await;
    waiting.write_all(b"attente").await.unwrap();
    let mut got = [0u8; 7];
    assert!(timeout(Duration::from_millis(500), waiting.read_exact(&mut got)).await.is_err());
    // Service retiré (prêt fini, serveur arrêté par AIODOS) : ses connexions sont lâchées
    let (status, _) = call(&b, Method::PUT, "/net/v1/exposed", json!({ "services": [] })).await;
    assert_eq!(status, StatusCode::OK);
    timeout(LIMIT, waiting.read_exact(&mut got)).await.expect("servie une fois lâché").unwrap();
    a.running.stop().await;
    b.running.stop().await;
}

// ------------------------------------------------------------ réponse jamais lue

#[tokio::test]
async fn an_unread_response_gives_its_slot_back() {
    let release = Arc::new(Notify::new());
    let a = Node::start(fake_app(release.clone()).await).await;
    let b_app = fake_app(release).await;
    let stall = Duration::from_secs(3);
    let b = Node::start_with(tempfile::tempdir().unwrap(), |dir| {
        let mut config = Config::local(dir.to_path_buf(), b_app, TOKEN);
        config.response_stall = stall;
        config.slot_wait = Duration::from_millis(200);
        config
    })
    .await;
    a.allow(vec![b.entry(true)]).await;
    b.allow(vec![a.entry(true)]).await;
    // A demande d'un coup autant de grosses réponses qu'il a de places chez B, et n'en lit aucune
    let big = format!("/peer/{}/p2p/v1/stream/big", b.id());
    let began = Instant::now();
    let asked: Vec<_> = (0..pool::MAX_STREAMS_PER_PEER)
        .map(|_| {
            let (listen, big) = (a.running.listen, big.clone());
            tokio::spawn(async move { send(listen, Method::POST, &big, Some(TOKEN), String::new()).await })
        })
        .collect();
    let mut unread = Vec::new();
    for response in asked {
        let response = response.await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        unread.push(response);
    }
    let busy = send(a.running.listen, Method::POST, &big, Some(TOKEN), String::new()).await;
    if began.elapsed() < stall {
        // (sinon, les premières sont peut-être déjà coupées : rien à conclure ici)
        assert_eq!(busy.status(), StatusCode::SERVICE_UNAVAILABLE, "toutes ses places sont prises");
    }
    drop(busy);
    // Ses requêtes courtes ont leur propre part : elles passent toujours
    let echo = format!("/peer/{}/p2p/v1/echo", b.id());
    let short = send(a.running.listen, Method::POST, &echo, Some(TOKEN), "x".into()).await;
    assert_eq!(short.status(), StatusCode::OK, "les requêtes courtes ne sont pas arrêtées");
    // B coupe les réponses qui ne sont plus lues : les places reviennent
    let mut served = StatusCode::SERVICE_UNAVAILABLE;
    let end = Instant::now() + stall * 10;
    while Instant::now() < end {
        let response = send(a.running.listen, Method::POST, &big, Some(TOKEN), String::new()).await;
        served = response.status();
        if served == StatusCode::OK {
            break;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    assert_eq!(served, StatusCode::OK, "les réponses non lues ont rendu leurs places");
    drop(unread);
    a.running.stop().await;
    b.running.stop().await;
}
