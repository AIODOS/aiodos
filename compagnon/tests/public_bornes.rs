//! Ce qu'un inconnu pourrait tenir à peu de frais chez un prêteur du réseau public, avec des
//! clés jetables (une par connexion, gratuites) : relecture de sécurité du 28/10/2026
//! (src/public_serve.rs). Un prêteur (DHT locale, annuaire en mémoire, comme
//! tests/public_ask.rs) devant un faux AIODOS, et des inconnus qui lui parlent directement
//! en `aiodos/public/1`, flux par flux, sans passer par un compagnon.
//! - Corps au goutte-à-goutte : sa place de lecture prise par une requête qui arrive, ou
//!   coupé sous le débit minimal, la place rendue.
//! - Connexions tenues par des en-têtes jamais finis : elles cèdent leur place avant une
//!   connexion servie, et une nouvelle connexion entre toujours.
//! - Refus rapides d'AIODOS : ils n'épuisent pas les admissions des conversations servies.

mod common;

use std::{net::SocketAddr, sync::Arc, time::Duration};

use aiodos_net::{
    PUBLIC_ALPN,
    public_reading::PREEMPT_AFTER,
    public_serve::{BODY_PACE, BODY_TIMEOUT, GLOBAL_PER_MINUTE, MAX_CONNECTIONS, MAX_READING, PER_KEY_PER_HOUR},
};
use common::{LIMIT, Node, call, lend, public_node};
use hyper::Method;
use iroh::{
    Endpoint, EndpointId, RelayMode,
    address_lookup::MemoryLookup,
    endpoint::{Connection, presets},
};
use serde_json::Value;
use tokio::{
    sync::Notify,
    time::{Instant, sleep, timeout},
};

/// Un prêteur qui prête, et ce qu'il faut pour le joindre : → (prêteur, sa clé publique,
/// l'annuaire, la DHT locale à garder en vie).
async fn lender() -> (Node, EndpointId, MemoryLookup, n0_mainline::Testnet) {
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let lender = public_node(&testnet.bootstrap, &lookup, Arc::new(Notify::new()), None).await;
    let key = lend(&lender, true).await.parse().unwrap();
    (lender, key, lookup, testnet)
}

/// Un inconnu : sa propre clé, tirée au hasard, en direct sur 127.0.0.1.
async fn stranger(lookup: &MemoryLookup) -> Endpoint {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr("127.0.0.1:0".parse::<SocketAddr>().unwrap())
        .unwrap()
        .address_lookup(lookup.clone())
        .bind()
        .await
        .unwrap()
}

async fn connect(stranger: &Endpoint, lender: EndpointId) -> Connection {
    timeout(LIMIT, stranger.connect(lender, PUBLIC_ALPN)).await.expect("délai").unwrap()
}

/// Une requête entière, d'un coup, sur un nouveau flux : → la réponse brute (ligne d'état,
/// en-têtes, corps), vide si la connexion est fermée.
async fn exchange(conn: &Connection, path: &str) -> String {
    let Ok(Ok((mut send, mut recv))) = timeout(LIMIT, conn.open_bi()).await else {
        return String::new();
    };
    let request = format!("POST {path} HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n\r\nok");
    // (sans fermer l'envoi : hyper prendrait la demi-fermeture pour un appelant parti)
    if send.write_all(request.as_bytes()).await.is_err() {
        return String::new();
    }
    let answer = timeout(LIMIT, recv.read_to_end(64 << 10)).await.expect("réponse");
    String::from_utf8_lossy(&answer.unwrap_or_default()).into_owned()
}

/// Les conversations servies aux inconnus par ce nœud (`GET /net/v1/public`).
async fn served(node: &Node) -> Value {
    call(node, Method::GET, "/net/v1/public", Value::Null).await.1["served"].clone()
}

#[tokio::test(flavor = "multi_thread")]
async fn a_body_sent_drop_by_drop_is_cut_and_its_reading_place_returned() {
    let (lender, lender_key, lookup, _testnet) = lender().await;
    let stranger = stranger(&lookup).await;
    // Toutes les places de lecture, tenues par des corps au goutte-à-goutte : deux octets par
    // seconde, jamais assez muets pour être coupés par le silence (deux flux par connexion)
    let mut conns = Vec::new();
    let mut slow = Vec::new();
    for i in 0..MAX_READING {
        if i % 2 == 0 {
            conns.push(connect(&stranger, lender_key).await);
        }
        let (mut send, mut recv) = conns.last().unwrap().open_bi().await.unwrap();
        let started = Instant::now();
        slow.push(tokio::spawn(async move {
            let head = b"POST /public/v1/echo HTTP/1.1\r\nhost: x\r\ncontent-length: 100000\r\n\r\nx";
            send.write_all(head).await.unwrap();
            let drip = async {
                while send.write_all(b"x").await.is_ok() {
                    sleep(Duration::from_millis(500)).await;
                }
                std::future::pending::<()>().await;
            };
            tokio::select! {
                answer = recv.read_to_end(64 << 10) => (answer.unwrap_or_default(), started.elapsed()),
                () = drip => unreachable!(),
            }
        }));
    }
    // Toutes les places prises : une requête entière prend celle du corps le plus lent, une
    // fois qu'il a eu le temps d'avancer (avant : 503 « busy » tant qu'elles étaient tenues)
    sleep(PREEMPT_AFTER + Duration::from_millis(500)).await;
    let other = connect(&stranger, lender_key).await;
    let answer = exchange(&other, "/public/v1/echo").await;
    assert!(answer.starts_with("HTTP/1.1 200"), "place de lecture jamais prise au plus lent : {answer}");

    // Le corps évincé reçoit 503 « busy » ; sous le débit minimal passé la grâce, les autres
    // sont coupés (408), bien avant le délai total (30 s avant la relecture du 28/10)
    let cut_by = BODY_PACE.grace + Duration::from_secs(3);
    assert!(cut_by < BODY_TIMEOUT);
    let mut evicted = 0;
    for task in slow {
        let (answer, after) = timeout(LIMIT, task).await.expect("corps lent jamais coupé").unwrap();
        let answer = String::from_utf8_lossy(&answer);
        if answer.starts_with("HTTP/1.1 503") && answer.contains("busy") {
            evicted += 1;
        } else {
            assert!(answer.starts_with("HTTP/1.1 408") && answer.contains("too_slow"), "{answer}");
        }
        assert!(after < cut_by, "coupé après {after:?}");
    }
    assert_eq!(evicted, 1, "une seule place prise");
    // Les places de lecture sont rendues : une requête entière passe
    let answer = exchange(&other, "/public/v1/echo").await;
    assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
    assert_eq!(served(&lender).await["in_flight"], 0);

    stranger.close().await;
    lender.running.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn connections_that_do_not_move_forward_give_way_before_a_served_one() {
    let (lender, lender_key, lookup, _testnet) = lender().await;
    let stranger = stranger(&lookup).await;
    // Une conversation servie, puis au repos entre deux messages
    let resting = connect(&stranger, lender_key).await;
    assert!(exchange(&resting, "/public/v1/echo").await.starts_with("HTTP/1.1 200"));

    // Deux fois le plafond de connexions, chacune avec un flux aux en-têtes jamais finis
    // (renouvelable toutes les 10 s) : avant la relecture du 28/10, elles comptaient comme
    // servies, la connexion au repos était fermée, puis toute nouvelle connexion refusée
    let mut stalled = Vec::new();
    for _ in 0..2 * MAX_CONNECTIONS {
        let conn = connect(&stranger, lender_key).await;
        if let Ok((mut send, recv)) = conn.open_bi().await {
            let _ = send.write_all(b"POST /public/v1/echo HTTP/1.1\r\nhost: x\r\n").await;
            stalled.push((conn, send, recv));
        }
        sleep(Duration::from_millis(20)).await;
    }
    sleep(Duration::from_millis(300)).await;

    // La conversation au repos est toujours là, et sert de nouveau
    let answer = exchange(&resting, "/public/v1/echo").await;
    assert!(answer.starts_with("HTTP/1.1 200"), "connexion servie fermée : {answer:?}");
    // Une nouvelle connexion entre, et elle est servie
    let newcomer = connect(&stranger, lender_key).await;
    let answer = exchange(&newcomer, "/public/v1/echo").await;
    assert!(answer.starts_with("HTTP/1.1 200"), "nouvelle connexion refusée : {answer:?}");

    drop(stalled);
    stranger.close().await;
    lender.running.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn quick_refusals_do_not_use_up_the_admissions_of_served_conversations() {
    let (lender, lender_key, lookup, _testnet) = lender().await;
    // Autant de requêtes qu'en admet la minute, qu'AIODOS refuse aussitôt (mal formées),
    // chacune relayée ; une clé jetable pour `PER_KEY_PER_HOUR` d'entre elles
    let mut refused = 0;
    let mut strangers = Vec::new();
    while refused < GLOBAL_PER_MINUTE {
        let stranger = stranger(&lookup).await;
        let conn = connect(&stranger, lender_key).await;
        for _ in 0..PER_KEY_PER_HOUR.min(GLOBAL_PER_MINUTE - refused) {
            let answer = exchange(&conn, "/public/v1/refused").await;
            assert!(answer.starts_with("HTTP/1.1 400") && answer.contains("bad_request"), "{answer}");
            refused += 1;
        }
        strangers.push(stranger);
    }
    // Une conversation passe encore (avant la relecture du 28/10 : 503 « busy » pour une
    // minute)
    let stranger = stranger(&lookup).await;
    let conn = connect(&stranger, lender_key).await;
    let answer = exchange(&conn, "/public/v1/echo").await;
    assert!(answer.starts_with("HTTP/1.1 200"), "{answer}");
    let counts = served(&lender).await;
    assert_eq!(counts["relayed"], GLOBAL_PER_MINUTE + 1, "{counts}");
    assert_eq!(counts["accepted"], 1, "{counts}");

    for stranger in strangers.into_iter().chain([stranger]) {
        stranger.close().await;
    }
    lender.running.stop().await;
}
