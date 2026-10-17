//! Services ouverts à une autre machine (tunnels bruts, cf. src/tunnel.rs) : B ouvre un
//! service TCP local à A ; A le joint par un port local, à travers la connexion QUIC.
//! Deux compagnons dans le même processus, en direct sur 127.0.0.1 (cf. common/mod.rs).

mod common;

use std::sync::Arc;

use common::{LIMIT, Node, call, fake_app, pair_of_nodes};
use hyper::{Method, StatusCode};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
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

async fn expose(node: &Node, services: Value) -> (StatusCode, Value) {
    call(node, Method::PUT, "/net/v1/exposed", json!({ "services": services })).await
}

async fn open_tunnel(node: &Node, peer: &Node, name: &str) -> u16 {
    let (status, body) =
        call(node, Method::POST, "/net/v1/tunnels", json!({ "peer": peer.id(), "name": name })).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    body["port"].as_u64().unwrap() as u16
}

/// Envoie `data` et relit autant d'octets, en même temps (gros envois compris).
async fn echo(port: u16, data: Vec<u8>) -> Vec<u8> {
    let tcp = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
    tcp.set_nodelay(true).unwrap();
    let (mut read, mut write) = tcp.into_split();
    let size = data.len();
    let writer = tokio::spawn(async move {
        write.write_all(&data).await.unwrap();
        write
    });
    let mut got = vec![0; size];
    timeout(LIMIT, read.read_exact(&mut got)).await.expect("écho dans les temps").unwrap();
    drop(writer.await.unwrap());
    got
}

/// La connexion locale est fermée sans rien recevoir (service refusé ou tunnel fermé).
async fn closed_without_answer(port: u16) -> bool {
    let Ok(mut tcp) = TcpStream::connect(("127.0.0.1", port)).await else {
        return true;
    };
    let _ = tcp.write_all(b"ping").await;
    let mut buf = [0u8; 16];
    matches!(timeout(LIMIT, tcp.read(&mut buf)).await, Ok(Ok(0)) | Ok(Err(_)))
}

// ------------------------------------------------------------ tests

#[tokio::test]
async fn a_service_is_reached_through_a_tunnel() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    let (status, body) =
        expose(&b, json!([{ "name": "rpc-essai", "port": service, "peers": [a.id()] }])).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["exposed"], 1);
    let port = open_tunnel(&a, &b, "rpc-essai").await;

    assert_eq!(echo(port, b"bonjour".to_vec()).await, b"bonjour");
    let big: Vec<u8> = (0..4_000_000u32).map(|i| (i % 251) as u8).collect();
    assert_eq!(echo(port, big.clone()).await, big, "4 Mo dans les deux sens");
    // Deux connexions en même temps dans le même tunnel
    let (one, two) = tokio::join!(echo(port, b"un".to_vec()), echo(port, b"deux".to_vec()));
    assert_eq!((one.as_slice(), two.as_slice()), (&b"un"[..], &b"deux"[..]));

    let tunnels = &a.status().await["tunnels"]["tunnels"];
    assert_eq!(tunnels[0]["port"], port);
    assert_eq!(tunnels[0]["peer"], b.id());
    assert_eq!(tunnels[0]["total"], 4);
    let exposed = &b.status().await["tunnels"]["exposed"];
    assert_eq!(exposed[0]["name"], "rpc-essai");
    assert_eq!(exposed[0]["port"], service);
    // Toujours une seule connexion QUIC entre les deux machines
    assert_eq!(a.peer_status(&b).await["dials"], 1);
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn a_service_is_only_open_to_the_named_machines() {
    let (a, b, _) = pair_of_nodes().await;
    let service = echo_service().await;
    let other = Node::start(fake_app(Arc::new(Notify::new())).await).await; // pas A
    let (status, _) =
        expose(&b, json!([{ "name": "rpc-autre", "port": service, "peers": [other.id()] }])).await;
    assert_eq!(status, StatusCode::OK);
    // A ouvre bien son port local, mais B refuse le service : connexion fermée
    let port = open_tunnel(&a, &b, "rpc-autre").await;
    assert!(closed_without_answer(port).await);
    let port = open_tunnel(&a, &b, "inconnu").await;
    assert!(closed_without_answer(port).await);
    let seen = a.peer_status(&b).await;
    let message = seen["last_error"]["message"].as_str().unwrap_or_default().to_string();
    assert!(message.contains("refusé par la machine"), "{seen}");
    // Noms et clés vérifiés à l'entrée
    let (status, _) = expose(&b, json!([{ "name": "../p2p", "port": service, "peers": [] }])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = expose(&b, json!([{ "name": "rpc", "port": service, "peers": ["pas-une-clé"] }])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    for node in [a, b, other] {
        node.running.stop().await;
    }
}

#[tokio::test]
async fn a_tunnel_needs_an_allowed_machine_and_closes_on_demand() {
    let (a, b, _) = pair_of_nodes().await;
    let stranger = Node::start(fake_app(Arc::new(Notify::new())).await).await;
    let service = echo_service().await;
    expose(&b, json!([{ "name": "rpc", "port": service, "peers": [a.id()] }])).await;
    let (status, _) =
        call(&a, Method::POST, "/net/v1/tunnels", json!({ "peer": stranger.id(), "name": "rpc" })).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "machine absente de la liste blanche de A");
    let (status, _) =
        call(&a, Method::POST, "/net/v1/tunnels", json!({ "peer": b.id(), "name": "RPC !" })).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let port = open_tunnel(&a, &b, "rpc").await;
    let tcp = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
    let (mut read, mut write) = tcp.into_split();
    write.write_all(b"avant").await.unwrap();
    let mut got = [0u8; 5];
    timeout(LIMIT, read.read_exact(&mut got)).await.unwrap().unwrap();
    assert_eq!(&got, b"avant");
    let path = format!("/net/v1/tunnels/{port}");
    assert_eq!(call(&a, Method::DELETE, &path, Value::Null).await.0, StatusCode::OK);
    // La connexion en cours est coupée, et le port ne répond plus
    let mut rest = [0u8; 16];
    assert!(matches!(timeout(LIMIT, read.read(&mut rest)).await, Ok(Ok(0)) | Ok(Err(_))));
    assert!(closed_without_answer(port).await);
    assert_eq!(call(&a, Method::DELETE, &path, Value::Null).await.0, StatusCode::NOT_FOUND);
    for node in [a, b, stranger] {
        node.running.stop().await;
    }
}
