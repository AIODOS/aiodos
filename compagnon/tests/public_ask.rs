//! Réseau public qui calcule (src/public_serve.rs, src/public_ask.rs) : deux compagnons
//! dans le même processus, une DHT locale et un annuaire en mémoire (comme tests/public.rs).
//! Le prêteur rejoint le réseau public ; le demandeur, sans l'avoir rejoint ni être appairé,
//! lui confie des requêtes par `POST /net/v1/public/ask/…`, chacune par la clé jetable de sa
//! conversation, et lit la réponse en flux. D'abord en direct sur 127.0.0.1 (bornes, refus,
//! en-têtes), puis par un relais lancé dans l'essai, les deux bouts publics en relais
//! seulement, comme sur Internet.

mod common;

use std::{net::SocketAddr, sync::Arc, time::Duration};

use aiodos_net::{Config, config::Relays};
use bytes::Bytes;
use common::{LIMIT, Node, TOKEN, call, fake_app, send};
use http_body_util::{BodyExt, Full};
use hyper::{Method, Request, Response, StatusCode, body::Incoming};
use hyper_util::rt::TokioIo;
use iroh::{RelayUrl, SecretKey, address_lookup::MemoryLookup};
use iroh_relay::server::{RelayConfig, Server, ServerConfig};
use serde_json::{Value, json};
use tokio::{net::TcpStream, sync::Notify, time::timeout};

const C1: &str = "conversation-0000000001";
const C2: &str = "conversation-0000000002";
const C3: &str = "conversation-0000000003";

/// Compagnon d'essai du réseau public (DHT locale, annuaire en mémoire), devant un faux
/// AIODOS ; par ce relais s'il est donné (le nœud public est alors en relais seulement).
async fn node(
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
async fn lend(node: &Node, lends: bool) -> String {
    let wanted = json!({
        "enabled": true, "version": "2.1.0", "models": ["qwen3.5:9b"], "lends": lends, "free": 1, "tps": 30,
    });
    let (status, body) = call(node, Method::PUT, "/net/v1/public", wanted).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["error"].is_null(), "{body}");
    body["key"].as_str().unwrap().to_string()
}

/// `POST /net/v1/public/ask/<lender>/<path>` sur l'API locale du demandeur.
async fn ask(
    asker: &Node,
    lender: &str,
    path: &str,
    conversation: Option<&str>,
    headers: &[(&str, &str)],
    body: impl Into<Bytes>,
) -> Response<Incoming> {
    let tcp = TcpStream::connect(asker.running.listen).await.unwrap();
    let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(tcp)).await.unwrap();
    tokio::spawn(conn);
    let mut request = Request::builder()
        .method(Method::POST)
        .uri(format!("/net/v1/public/ask/{lender}/{path}"))
        .header("host", "127.0.0.1")
        .header("x-aiodos-net-token", TOKEN);
    if let Some(conversation) = conversation {
        request = request.header("x-aiodos-conversation", conversation);
    }
    for (name, value) in headers {
        request = request.header(*name, *value);
    }
    let request = request.body(Full::new(body.into())).unwrap();
    timeout(LIMIT, sender.send_request(request)).await.expect("délai").unwrap()
}

async fn read_json(response: Response<Incoming>) -> (StatusCode, Value) {
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

/// Ce que le faux AIODOS du prêteur a vu d'une requête de cette conversation.
async fn echo(asker: &Node, lender: &str, conversation: &str) -> (StatusCode, Value) {
    read_json(ask(asker, lender, "public/v1/echo", Some(conversation), &[], "").await).await
}

/// Prochain morceau d'une réponse en flux (`None` à la fin).
async fn next_chunk(body: &mut Incoming) -> Option<Bytes> {
    loop {
        let frame = timeout(LIMIT, body.frame()).await.expect("morceau attendu")?.ok()?;
        if let Ok(data) = frame.into_data() {
            return Some(data);
        }
    }
}

/// Les conversations servies aux inconnus par ce nœud (`GET /net/v1/public`).
async fn served(node: &Node) -> Value {
    call(node, Method::GET, "/net/v1/public", Value::Null).await.1["served"].clone()
}

/// Le refus attendu : son code HTTP et son code d'erreur.
fn refused(got: (StatusCode, Value), status: StatusCode, error: &str) {
    assert_eq!(got.0, status, "{}", got.1);
    assert_eq!(got.1["error"], error, "{}", got.1);
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stranger_answers_a_conversation_without_knowing_who_asks() {
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let release = Arc::new(Notify::new());
    let lender = node(&testnet.bootstrap, &lookup, release.clone(), None).await;
    let asker = node(&testnet.bootstrap, &lookup, Arc::new(Notify::new()), None).await;
    let lender_key = lend(&lender, true).await;

    // Un aller-retour. Le prêteur voit une clé jetable, et rien de ce qui désignerait le
    // demandeur, même déguisé en machine appairée ou en une autre clé
    let asker_id = asker.id();
    let disguise = [
        ("content-type", "application/json"),
        ("x-aiodos-peer", asker_id.as_str()),
        ("x-aiodos-public", lender_key.as_str()),
        ("user-agent", "AIODOS 2.1.0 (le PC de Paul)"),
        ("x-forwarded-for", "203.0.113.9"),
    ];
    let response = ask(&asker, &lender_key, "public/v1/echo", Some(C1), &disguise, "bonjour").await;
    let (status, seen) = read_json(response).await;
    assert_eq!(status, StatusCode::OK, "{seen}");
    assert_eq!(seen["path"], "/public/v1/echo");
    assert_eq!(seen["body"], "bonjour");
    assert_eq!(seen["content_type"], "application/json");
    let k1 = seen["public"].as_str().unwrap().to_string();
    assert_eq!(k1.len(), 64, "clé de l'appelant en hexadécimal");
    assert!(k1 != asker_id && k1 != lender_key, "ni la clé de la machine, ni une clé envoyée");
    for hidden in ["peer", "token", "conversation", "user_agent", "forwarded_for"] {
        assert!(seen[hidden].is_null(), "{hidden} : {seen}");
    }
    // Le demandeur n'a pas de clé publique sur disque : la clé jetable n'est écrite nulle part
    assert!(!asker.dir.path().join("aiodos-public.key").exists());

    // Même conversation, même clé ; une autre conversation, une autre clé
    assert_eq!(echo(&asker, &lender_key, C1).await.1["public"], k1.as_str());
    let k2 = echo(&asker, &lender_key, C2).await.1["public"].as_str().unwrap().to_string();
    assert_ne!(k2, k1);

    // La réponse revient en flux : le premier morceau avant la fin
    let mut first = ask(&asker, &lender_key, "public/v1/stream/slow", Some(C1), &[], "").await;
    assert_eq!(first.status(), StatusCode::OK);
    assert_eq!(next_chunk(first.body_mut()).await.unwrap(), "un\n");
    // Une requête à la fois par clé : la même conversation est refusée aussitôt
    refused(echo(&asker, &lender_key, C1).await, StatusCode::SERVICE_UNAVAILABLE, "busy");
    // Deux à la fois pour tout le public : une autre conversation passe, une troisième non
    let mut second = ask(&asker, &lender_key, "public/v1/stream/slow", Some(C2), &[], "").await;
    assert_eq!(second.status(), StatusCode::OK);
    assert_eq!(next_chunk(second.body_mut()).await.unwrap(), "un\n");
    refused(echo(&asker, &lender_key, C3).await, StatusCode::SERVICE_UNAVAILABLE, "busy");
    release.notify_waiters();
    for body in [first.body_mut(), second.body_mut()] {
        assert_eq!(next_chunk(body).await.unwrap(), "deux\n");
        assert!(next_chunk(body).await.is_none(), "fin du flux");
    }
    // Les places sont rendues avec la fin des réponses
    let deadline = tokio::time::Instant::now() + LIMIT;
    while echo(&asker, &lender_key, C3).await.0 != StatusCode::OK {
        assert!(tokio::time::Instant::now() < deadline, "places jamais rendues");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let counts = served(&lender).await;
    assert_eq!(counts["in_flight"], 0, "{counts}");
    assert!(counts["relayed"].as_u64().unwrap() >= 6, "{counts}");
    assert!(counts["refused"].as_u64().unwrap() >= 2, "{counts}");

    // Le profil ne prête plus : refus sans rien relayer ; puis de nouveau
    lend(&lender, false).await;
    refused(echo(&asker, &lender_key, C1).await, StatusCode::SERVICE_UNAVAILABLE, "not_lending");
    lend(&lender, true).await;
    assert_eq!(echo(&asker, &lender_key, C1).await.0, StatusCode::OK);

    // Refus du demandeur lui-même, avant de rien envoyer
    let no_conversation = ask(&asker, &lender_key, "public/v1/echo", None, &[], "").await;
    refused(read_json(no_conversation).await, StatusCode::BAD_REQUEST, "conversation");
    refused(echo(&asker, &lender_key, "trop-court").await, StatusCode::BAD_REQUEST, "conversation");
    let private = ask(&asker, &lender_key, "p2p/v1/echo", Some(C1), &[], "").await;
    refused(read_json(private).await, StatusCode::NOT_FOUND, "not_found");
    let around = ask(&asker, &lender_key, "public/v1/../../p2p/v1/echo", Some(C1), &[], "").await;
    refused(read_json(around).await, StatusCode::NOT_FOUND, "not_found");
    refused(echo(&asker, "pas-une-cle", C1).await, StatusCode::BAD_REQUEST, "bad_key");
    let big = ask(&asker, &lender_key, "public/v1/echo", Some(C1), &[], vec![b'x'; 300 << 10]).await;
    refused(read_json(big).await, StatusCode::PAYLOAD_TOO_LARGE, "too_large");
    let path = format!("/net/v1/public/ask/{lender_key}/public/v1/echo");
    let get = send(asker.running.listen, Method::GET, &path, Some(TOKEN), String::new()).await;
    assert_eq!(get.status(), StatusCode::METHOD_NOT_ALLOWED);
    let stranger = send(asker.running.listen, Method::POST, &path, None, String::new()).await;
    assert_eq!(stranger.status(), StatusCode::UNAUTHORIZED, "le jeton reste exigé");
    // Une clé que personne ne sert
    let nobody = SecretKey::generate().public().to_string();
    refused(echo(&asker, &nobody, C1).await, StatusCode::BAD_GATEWAY, "unreachable");

    // Quitter le réseau public : plus rien n'est servi aux inconnus (la connexion gardée par
    // la conversation est fermée : « unreachable », ou « interrupted » si sa fermeture n'est
    // pas encore arrivée)
    call(&lender, Method::PUT, "/net/v1/public", json!({ "enabled": false })).await;
    let (status, gone) = echo(&asker, &lender_key, C3).await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{gone}");
    assert!(gone["error"] == "unreachable" || gone["error"] == "interrupted", "{gone}");

    asker.running.stop().await;
    lender.running.stop().await;
}

/// Relais en HTTP sur 127.0.0.1 (port libre), ouvert à toutes les clés ; il s'arrête quand
/// le `Server` rendu est lâché.
async fn relay() -> (Server, RelayUrl) {
    let mut config = ServerConfig::default();
    config.relay = Some(RelayConfig::new(SocketAddr::from(([127, 0, 0, 1], 0))));
    let server = Server::spawn(config).await.unwrap();
    let url = format!("http://{}", server.http_addr().unwrap()).parse().unwrap();
    (server, url)
}

#[tokio::test(flavor = "multi_thread")]
async fn through_a_relay_with_both_public_ends_relay_only() {
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let (_relay, url) = relay().await;
    let release = Arc::new(Notify::new());
    let lender = node(&testnet.bootstrap, &lookup, release.clone(), Some(&url)).await;
    let asker = node(&testnet.bootstrap, &lookup, Arc::new(Notify::new()), Some(&url)).await;
    let lender_key = lend(&lender, true).await;
    // L'annuaire ne donne que le relais du prêteur (aucune adresse IP : son nœud public n'a
    // pas de socket UDP) ; le temps qu'il rejoigne son relais
    let deadline = tokio::time::Instant::now() + LIMIT;
    let seen = loop {
        let (status, seen) = echo(&asker, &lender_key, C1).await;
        if status == StatusCode::OK {
            break seen;
        }
        assert!(tokio::time::Instant::now() < deadline, "prêteur jamais joint par le relais : {seen}");
        tokio::time::sleep(Duration::from_millis(200)).await;
    };
    let key = seen["public"].as_str().unwrap();
    assert!(key != asker.id() && key != lender_key);
    let mut answer = ask(&asker, &lender_key, "public/v1/stream/slow", Some(C1), &[], "").await;
    assert_eq!(answer.status(), StatusCode::OK);
    assert_eq!(next_chunk(answer.body_mut()).await.unwrap(), "un\n");
    release.notify_waiters();
    assert_eq!(next_chunk(answer.body_mut()).await.unwrap(), "deux\n");
    assert!(next_chunk(answer.body_mut()).await.is_none());

    // Un demandeur qui passe par un autre relais ne joint pas ce prêteur : il devrait passer
    // par le relais que le prêteur annonce, qui verrait son adresse IP
    let (_other_relay, other_url) = relay().await;
    let elsewhere = node(&testnet.bootstrap, &lookup, Arc::new(Notify::new()), Some(&other_url)).await;
    let before = served(&lender).await;
    refused(echo(&elsewhere, &lender_key, C1).await, StatusCode::BAD_GATEWAY, "unreachable");
    assert_eq!(served(&lender).await, before, "rien n'est arrivé du second demandeur");

    elsewhere.running.stop().await;
    asker.running.stop().await;
    lender.running.stop().await;
}
