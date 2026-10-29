//! Réseau public, phase 3.0 (src/public.rs) : trois compagnons dans le même processus,
//! une DHT locale, et un annuaire en mémoire à la place des relais. Deux rejoignent le
//! réseau public : ils se trouvent par la DHT, sans invitation ni liste blanche, et
//! chacun voit le profil signé de l'autre ; le troisième n'a rien demandé : il n'a pas
//! de clé publique, n'annonce rien et ne voit personne.
//!
//! Et l'identité publique coûteuse (29/10/2026, src/public_identity.rs) : un nœud dont la clé
//! ne porte pas la preuve de travail n'entre ni dans l'annuaire des autres ni dans leur
//! groupe de discussion ; une clé longue à trouver ne retient pas `PUT`, et le nœud rejoint
//! le réseau de lui-même dès qu'elle est trouvée.

mod common;

use std::{path::Path, sync::Arc, time::Duration};

use aiodos_net::{Config, config::TEST_DIFFICULTY, identity, public::KEY_FILE, public_identity};
use common::{Node, TOKEN, call, fake_app};
use hyper::{Method, StatusCode};
use iroh::{SecretKey, address_lookup::MemoryLookup};
use serde_json::{Value, json};
use tokio::sync::Notify;

async fn node(bootstrap: &[String], lookup: &MemoryLookup) -> Node {
    node_with(bootstrap, lookup, tempfile::tempdir().unwrap(), |_| {}).await
}

/// Un nœud sur ce dossier, aux réglages d'essai changés par `tune`.
async fn node_with(
    bootstrap: &[String],
    lookup: &MemoryLookup,
    dir: tempfile::TempDir,
    tune: impl FnOnce(&mut Config),
) -> Node {
    let app = fake_app(Arc::new(Notify::new())).await;
    let (bootstrap, lookup) = (bootstrap.to_vec(), lookup.clone());
    Node::start_with(dir, move |dir: &Path| {
        let mut config = Config::local(dir.to_path_buf(), app, TOKEN);
        config.public_dht_bootstrap = Some(bootstrap);
        config.public_lookup = Some(lookup);
        tune(&mut config);
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

/// Relecture du 28/10 (« annuaire noyé ») : une clé neuve ne coûtait rien. Un nœud dont la
/// clé ne porte pas la preuve de travail des autres n'entre ni dans leur annuaire, ni dans
/// leur groupe de discussion (ses profils ne passent donc pas par eux) ; deux nœuds qui la
/// portent se voient.
#[tokio::test(flavor = "multi_thread")]
async fn a_node_without_the_proof_of_work_is_neither_seen_nor_a_neighbor() {
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let a = node(&testnet.bootstrap, &lookup).await;
    let c = node(&testnet.bootstrap, &lookup).await;
    // B : une clé sans les bits exigés par A et C, gardée parce que B, lui, n'en exige aucun
    // (comme un nœud d'avant le 29/10, ou un menteur qui ne paie pas la preuve)
    let cheap = loop {
        let key = SecretKey::generate();
        if !public_identity::proves(&key.public(), TEST_DIFFICULTY) {
            break key;
        }
    };
    let dir = tempfile::tempdir().unwrap();
    identity::write_file(dir.path(), KEY_FILE, &cheap).unwrap();
    let b = node_with(&testnet.bootstrap, &lookup, dir, |config| config.public_difficulty = 0).await;

    let kb = join(&b, &["qwen3.5:999b"]).await;
    assert_eq!(kb, cheap.public().to_string(), "B garde sa clé sans preuve");
    let ka = join(&a, &["qwen3.5:9b"]).await;
    let kc = join(&c, &["qwen3.5:9b"]).await;
    // Les clés de A et C, créées au démarrage, portent la preuve
    for key in [&ka, &kc] {
        assert!(public_identity::proves(&key.parse().unwrap(), TEST_DIFFICULTY));
    }
    sees(&a, &kc).await;
    sees(&c, &ka).await;
    // Quelques diffusions plus tard (B dit son profil à chaque nouveau voisin, et dans les
    // 5 s quand il change), B n'est ni chez A ni chez C, et n'a aucun voisin
    let wanted = json!({ "enabled": true, "version": "2.1.0", "models": ["qwen3.5:9b"], "lends": true, "free": 1 });
    call(&b, Method::PUT, "/net/v1/public", wanted).await;
    tokio::time::sleep(Duration::from_secs(8)).await;
    for node in [&a, &c] {
        let state = public(node).await;
        let keys: Vec<&str> = state["nodes"].as_array().unwrap().iter().filter_map(|n| n["key"].as_str()).collect();
        assert!(!keys.contains(&kb.as_str()), "{state}");
    }
    let alone = public(&b).await;
    assert_eq!(alone["neighbors"], 0, "{alone}");
    assert!(alone["nodes"].as_array().unwrap().is_empty(), "{alone}");

    a.running.stop().await;
    b.running.stop().await;
    c.running.stop().await;
}

/// La clé du nœud public se cherche une fois (preuve de travail) : jusqu'à ~30 s sur un
/// téléphone, plus qu'AIODOS n'attend une réponse (5 s). `PUT` répond sans l'attendre
/// (`"preparing": true`), puis le nœud rejoint le réseau de lui-même ; la clé gardée porte la
/// preuve, et sert telle quelle ensuite.
#[tokio::test(flavor = "multi_thread")]
async fn a_slow_key_search_does_not_hold_put_and_the_node_joins_when_it_is_found() {
    // 2^18 essais en moyenne : bien plus long que l'attente nulle, une seconde sur ce PC
    const BITS: u8 = 18;
    let testnet = n0_mainline::Testnet::new(5).await.unwrap();
    let lookup = MemoryLookup::new();
    let a = node_with(&testnet.bootstrap, &lookup, tempfile::tempdir().unwrap(), |config| {
        config.public_difficulty = BITS;
        config.public_key_wait = Duration::ZERO; // la recherche dure toujours plus que l'attente
    })
    .await;
    let wanted = json!({ "enabled": true, "version": "2.1.0", "models": [], "lends": false });
    let (status, body) = call(&a, Method::PUT, "/net/v1/public", wanted).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!((body["enabled"].clone(), body["preparing"].clone()), (json!(false), json!(true)), "{body}");
    assert!(body["error"].is_null(), "{body}");
    // Trouvée : le nœud rejoint le réseau sans nouveau PUT
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    let key = loop {
        let state = public(&a).await;
        if let Some(key) = state["key"].as_str() {
            assert_eq!((state["enabled"].clone(), state["preparing"].clone()), (json!(true), json!(false)));
            break key.to_string();
        }
        assert!(tokio::time::Instant::now() < deadline, "jamais rejoint : {state}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    assert!(public_identity::proves(&key.parse().unwrap(), BITS));
    let kept = identity::read_file(a.dir.path(), KEY_FILE).unwrap().unwrap();
    assert_eq!(kept.public().to_string(), key, "gardée");
    // Quitter puis revenir : la même clé, aussitôt (rien à chercher)
    call(&a, Method::PUT, "/net/v1/public", json!({ "enabled": false })).await;
    let (_, again) = call(&a, Method::PUT, "/net/v1/public", json!({ "enabled": true })).await;
    assert_eq!((again["enabled"].clone(), again["key"].clone()), (json!(true), json!(key)), "{again}");
    a.running.stop().await;
}
