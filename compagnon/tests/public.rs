//! Réseau public, phase 3.0 (src/public.rs) : trois compagnons dans le même processus,
//! une DHT locale, et un annuaire en mémoire à la place des relais. Deux rejoignent le
//! réseau public : ils se trouvent par la DHT, sans invitation ni liste blanche, et
//! chacun voit le profil signé de l'autre ; le troisième n'a rien demandé : il n'a pas
//! de clé publique, n'annonce rien et ne voit personne.

mod common;

use std::{sync::Arc, time::Duration};

use aiodos_net::Config;
use common::{Node, TOKEN, call, fake_app};
use hyper::{Method, StatusCode};
use iroh::address_lookup::MemoryLookup;
use serde_json::{Value, json};
use tokio::sync::Notify;

async fn node(bootstrap: &[String], lookup: &MemoryLookup) -> Node {
    let app = fake_app(Arc::new(Notify::new())).await;
    let (bootstrap, lookup) = (bootstrap.to_vec(), lookup.clone());
    Node::start_with(tempfile::tempdir().unwrap(), move |dir| {
        let mut config = Config::local(dir.to_path_buf(), app, TOKEN);
        config.public_dht_bootstrap = Some(bootstrap);
        config.public_lookup = Some(lookup);
        config
    })
    .await
}

async fn public(node: &Node) -> Value {
    let (status, body) = call(node, Method::GET, "/net/v1/public", Value::Null).await;
    assert_eq!(status, StatusCode::OK);
    body
}

async fn join(node: &Node, models: &[&str]) -> String {
    let wanted = json!({
        "enabled": true, "version": "2.1.0", "models": models, "lends": true, "free": 2, "tps": 41.4,
    });
    let (status, body) = call(node, Method::PUT, "/net/v1/public", wanted).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["error"].is_null(), "{body}");
    assert_eq!(body["enabled"], true);
    body["key"].as_str().unwrap().to_string()
}

/// Attend que `node` voie la clé `key` dans son annuaire public.
async fn sees(node: &Node, key: &str) -> Value {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
    loop {
        let state = public(node).await;
        let found = state["nodes"].as_array().and_then(|nodes| nodes.iter().find(|n| n["key"] == key));
        if let Some(found) = found {
            return found.clone();
        }
        assert!(tokio::time::Instant::now() < deadline, "{key} jamais vue : {state}");
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}

/// Attend que `node` voie la clé `key` avec un profil qui satisfait `wanted`, dans le délai.
async fn sees_within(node: &Node, key: &str, within: Duration, wanted: impl Fn(&Value) -> bool) -> Value {
    let deadline = tokio::time::Instant::now() + within;
    loop {
        let state = public(node).await;
        let found = state["nodes"].as_array().and_then(|nodes| nodes.iter().find(|n| n["key"] == key));
        if let Some(found) = found.filter(|n| wanted(n)) {
            return found.clone();
        }
        assert!(tokio::time::Instant::now() < deadline, "profil de {key} pas vu à temps : {state}");
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn two_public_nodes_meet_through_the_dht_and_a_third_stays_silent() {
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let a = node(&testnet.bootstrap, &lookup).await;
    let b = node(&testnet.bootstrap, &lookup).await;
    let c = node(&testnet.bootstrap, &lookup).await;

    let quiet = public(&c).await;
    assert_eq!(quiet["enabled"], false);
    assert!(quiet["key"].is_null());

    let ka = join(&a, &["embeddinggemma"]).await;
    let kb = join(&b, &["qwen3.5:9b"]).await;
    // La clé publique n'est pas celle de la machine : rien ne relie les deux
    assert_ne!(ka, a.id());
    assert!(a.dir.path().join("aiodos-public.key").exists());

    let seen = sees(&a, &kb).await;
    assert_eq!(seen["models"], json!(["qwen3.5:9b"]));
    assert_eq!(seen["version"], "2.1.0");
    assert_eq!(seen["lends"], true);
    // Places libres et vitesse : signées avec le reste, et lues par les autres
    assert_eq!((seen["free"].clone(), seen["tps"].clone()), (json!(2), json!(41)));
    assert_eq!(sees(&b, &ka).await["models"], json!(["embeddinggemma"]));
    assert!(public(&a).await["neighbors"].as_u64().unwrap() >= 1);

    // C n'a rien demandé : il ne voit personne, et n'a pas de clé à montrer
    let quiet = public(&c).await;
    assert!(quiet["nodes"].as_array().unwrap().is_empty());
    assert!(!c.dir.path().join("aiodos-public.key").exists());

    // Le profil change : les autres le voient, sans attendre la diffusion de chaque minute
    // (places libres vues avec jusqu'à 90 s de retard, essai réel du 28/10/2026)
    let wanted = json!({ "enabled": true, "version": "2.1.0", "models": ["mistral:7b"], "lends": false });
    call(&a, Method::PUT, "/net/v1/public", wanted).await;
    let changed = sees_within(&b, &ka, Duration::from_secs(15), |n| n["lends"] == false).await;
    assert_eq!(changed["models"], json!(["mistral:7b"]));
    // Et de nouveau : au plus tôt CHANGE_GAP (5 s) après la précédente, pas une minute
    let wanted = json!({ "enabled": true, "version": "2.1.0", "models": ["mistral:7b"], "lends": true, "free": 1 });
    call(&a, Method::PUT, "/net/v1/public", wanted).await;
    let changed = sees_within(&b, &ka, Duration::from_secs(15), |n| n["lends"] == true).await;
    assert_eq!(changed["free"], json!(1));

    // Quitter le réseau public : plus de clé affichée, plus d'annuaire
    let (_, left) = call(&a, Method::PUT, "/net/v1/public", json!({ "enabled": false })).await;
    assert_eq!(left["enabled"], false);
    assert!(left["nodes"].as_array().unwrap().is_empty());
    // Revenir : la même clé publique (gardée dans son fichier)
    assert_eq!(join(&a, &[]).await, ka);

    a.running.stop().await;
    b.running.stop().await;
    c.running.stop().await;
}

/// Deux nœuds arrivés au même instant peuvent lire la DHT chacun avant l'inscription de
/// l'autre : seuls, ils la relisent bientôt (20 s), au lieu d'attendre 5 minutes.
#[tokio::test(flavor = "multi_thread")]
async fn two_nodes_joining_at_the_same_instant_still_meet() {
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let a = node(&testnet.bootstrap, &lookup).await;
    let b = node(&testnet.bootstrap, &lookup).await;
    let (ka, kb) = tokio::join!(join(&a, &[]), join(&b, &[]));
    sees(&a, &kb).await;
    sees(&b, &ka).await;
    a.running.stop().await;
    b.running.stop().await;
}
