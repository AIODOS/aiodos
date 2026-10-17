//! Deux compagnons dans le même processus, relais et annuaires coupés : connexion
//! directe sur 127.0.0.1. Un faux serveur réseau d'AIODOS (`p2p_app`) répond derrière
//! le compagnon B (cf. common/mod.rs).

mod common;

use std::{sync::Arc, time::Duration};

use common::{LIMIT, Node, TOKEN, call, fake_app, pair_of_nodes, send, via};
use http_body_util::BodyExt;
use hyper::{Method, StatusCode};
use serde_json::Value;
use tokio::{sync::Notify, time::timeout};

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
