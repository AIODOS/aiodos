//! Relais seulement (`--relay-only`, étape 2.7) : des compagnons sans socket UDP, qui ne
//! peuvent se joindre que par un relais iroh-relay 1.3.0 lancé dans l'essai, en HTTP
//! comme `iroh-relay --dev`, et réservé aux clés de sa liste blanche.

mod common;

use std::{net::SocketAddr, sync::Arc, time::Duration};

use aiodos_net::{Config, config::Relays, identity};
use common::{LIMIT, Node, TOKEN, call, fake_app, via};
use hyper::{Method, StatusCode};
use iroh::{EndpointId, RelayUrl};
use iroh_relay::server::{Access, AccessControl, ClientRequest, RelayConfig, Server, ServerConfig};
use serde_json::{Value, json};
use tokio::{sync::Notify, time::Instant};

/// Liste blanche du relais, comme `access.allowlist` dans relais.toml (docs/relais.md).
#[derive(Debug)]
struct RelayAllowlist(Vec<EndpointId>);

impl AccessControl for RelayAllowlist {
    async fn on_connect(&self, request: &ClientRequest) -> Access {
        if self.0.contains(&request.endpoint_id()) {
            Access::Allow
        } else {
            Access::Deny { reason: None }
        }
    }
}

/// Relais en HTTP sur 127.0.0.1 (port libre) qui ne sert que ces clés. Il s'arrête quand
/// le `Server` rendu est lâché.
async fn relay(allowed: Vec<EndpointId>) -> (Server, RelayUrl) {
    let mut relay = RelayConfig::new(SocketAddr::from(([127, 0, 0, 1], 0)));
    relay.access = Arc::new(RelayAllowlist(allowed));
    let mut config = ServerConfig::default();
    config.relay = Some(relay);
    let server = Server::spawn(config).await.unwrap();
    let url = format!("http://{}", server.http_addr().unwrap()).parse().unwrap();
    (server, url)
}

/// Dossier d'un compagnon dont la clé existe déjà : on l'inscrit au relais avant de lancer
/// le compagnon, comme l'hébergeur qui reçoit la clé copiée dans la vue Réseau.
fn machine() -> (tempfile::TempDir, EndpointId) {
    let dir = tempfile::tempdir().unwrap();
    let (secret, _) = identity::load_or_create(dir.path()).unwrap();
    (dir, secret.public())
}

/// Compagnon qui passe par ce relais ; avec `relay_only`, jamais en direct.
async fn start(dir: tempfile::TempDir, url: &RelayUrl, relay_only: bool) -> Node {
    let forward = fake_app(Arc::new(Notify::new())).await;
    let url = url.clone();
    Node::start_with(dir, move |path| {
        let mut config = Config::local(path.to_path_buf(), forward, TOKEN);
        config.relays = Relays::Custom(vec![url]);
        config.relay_only = relay_only;
        if relay_only {
            config.bind = None;
        }
        config.dial_timeout = Duration::from_secs(5);
        config
    })
    .await
}

/// Entrée de liste blanche du compagnon : la clé et le relais, sans adresse directe.
fn by_relay(node: &Node, url: &RelayUrl) -> Value {
    json!({ "id": node.id(), "relays": [url.to_string()] })
}

/// Premier relais de la machine, tel que son compagnon le décrit.
async fn home_relay(node: &Node) -> Value {
    node.status().await["home_relays"][0].clone()
}

/// Attend qu'une condition soit vraie (au plus `LIMIT`).
async fn wait_until<F, Fut>(what: &str, mut check: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = bool>,
{
    let deadline = Instant::now() + LIMIT;
    while !check().await {
        assert!(Instant::now() < deadline, "délai dépassé en attendant : {what}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

#[tokio::test]
async fn relay_only_machines_talk_through_the_relay_and_never_directly() {
    let ((dir_a, key_a), (dir_b, key_b)) = (machine(), machine());
    let (_relay, url) = relay(vec![key_a, key_b]).await;
    let a = start(dir_a, &url, true).await;
    let b = start(dir_b, &url, true).await;
    // Demandé dès le démarrage (réponse à une invitation), le ticket attend le relais :
    // sans annuaire, la clé seule ne mènerait nulle part
    let (_, ticket) = call(&b, Method::GET, "/net/v1/ticket", Value::Null).await;
    assert_eq!(ticket["relays"], json!([url.to_string()]), "{ticket}");
    assert_eq!(ticket["addrs"], json!([]), "{ticket}");
    for node in [&a, &b] {
        assert!(node.running.bound.is_empty(), "aucune socket UDP");
        let status = node.status().await;
        assert_eq!(status["relay_only"], true);
        assert_eq!(status["portmap"], false);
        assert_eq!(status["addrs"], json!([]), "aucune adresse directe annoncée");
        wait_until("relais connecté", || async move { home_relay(node).await["connected"] == true })
            .await;
    }
    a.allow(vec![by_relay(&b, &url)]).await;
    b.allow(vec![by_relay(&a, &url)]).await;
    // Des requêtes dans les deux sens pendant 2 s : de quoi passer en direct si c'était
    // possible (cf. le contrôle plus bas, où ces mêmes machines y passent).
    for i in 0..10 {
        let (status, reply) = via(&a, &b, "/p2p/v1/echo", &format!("aller {i}")).await;
        assert_eq!(status, StatusCode::OK, "{reply}");
        assert_eq!(reply["peer"], a.id());
        let (status, reply) = via(&b, &a, "/p2p/v1/echo", &format!("retour {i}")).await;
        assert_eq!(status, StatusCode::OK, "{reply}");
        assert_eq!(reply["peer"], b.id());
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    for (node, other) in [(&a, &b), (&b, &a)] {
        let seen = node.peer_status(other).await;
        assert_eq!(seen["connected"], true);
        assert_eq!(seen["path"], "relay", "{seen}");
        assert_eq!(seen["remote"], url.to_string());
        assert_eq!(seen["connections"], 1);
    }
    a.running.stop().await;
    b.running.stop().await;
}

#[tokio::test]
async fn the_relay_only_serves_the_keys_of_its_allowlist() {
    let ((dir_a, key_a), (dir_s, _)) = (machine(), machine());
    let (_relay, url) = relay(vec![key_a]).await;
    let a = start(dir_a, &url, true).await;
    let stranger = start(dir_s, &url, true).await;
    let (ra, rs) = (&a, &stranger);
    wait_until("refus du relais", || async move { home_relay(rs).await["denied"].is_string() }).await;
    assert_eq!(home_relay(&stranger).await["connected"], false);
    wait_until("relais connecté", || async move { home_relay(ra).await["connected"] == true }).await;
    assert_eq!(home_relay(&a).await["denied"], Value::Null);
    // Les deux compagnons s'admettent l'un l'autre : seul le relais leur fait obstacle
    a.allow(vec![by_relay(&stranger, &url)]).await;
    stranger.allow(vec![by_relay(&a, &url)]).await;
    let (status, reply) = via(&a, &stranger, "/p2p/v1/echo", "ping").await;
    assert_eq!(status, StatusCode::GATEWAY_TIMEOUT, "{reply}");
    let (status, reply) = via(&stranger, &a, "/p2p/v1/echo", "ping").await;
    assert_eq!(status, StatusCode::GATEWAY_TIMEOUT, "{reply}");
    // Rien n'est arrivé jusqu'à la poignée de main de A : le relais a tout arrêté avant
    assert_eq!(a.status().await["refused"]["count"], 0);
    a.running.stop().await;
    stranger.running.stop().await;
}

#[tokio::test]
async fn without_the_option_the_same_machines_go_direct() {
    // Contrôle : mêmes machines, même relais, mêmes listes blanches, mais avec leurs
    // sockets UDP (sur 127.0.0.1). Iroh perce et passe en direct : l'essai plus haut
    // verrait donc un chemin direct s'il y en avait un.
    let ((dir_a, key_a), (dir_b, key_b)) = (machine(), machine());
    let (_relay, url) = relay(vec![key_a, key_b]).await;
    let a = start(dir_a, &url, false).await;
    let b = start(dir_b, &url, false).await;
    for node in [&a, &b] {
        wait_until("relais connecté", || async move { home_relay(node).await["connected"] == true })
            .await;
    }
    a.allow(vec![by_relay(&b, &url)]).await;
    b.allow(vec![by_relay(&a, &url)]).await;
    let (ra, rb) = (&a, &b);
    wait_until("chemin direct", || async move {
        via(ra, rb, "/p2p/v1/echo", "ping").await.0 == StatusCode::OK
            && ra.peer_status(rb).await["path"] == "direct"
    })
    .await;
    a.running.stop().await;
    b.running.stop().await;
}
