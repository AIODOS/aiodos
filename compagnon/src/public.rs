//! Réseau public, phase 3.0 : un annuaire, sans rien confier (docs/phase3-reseau-public.md).
//!
//! Désactivé par défaut : AIODOS le démarre seulement quand l'utilisateur a choisi
//! « Rejoindre le réseau public » (`PUT /net/v1/public`). Sans ce choix, rien ne tourne et
//! rien n'est annoncé.
//!
//! Un nœud public a sa **propre clé**, distincte de celle de la machine : ni ses amis ni
//! les inconnus ne peuvent relier son identité publique à ses machines appairées. Son
//! **point de terminaison est à part** aussi : il n'accepte que le groupe de discussion
//! public (ALPN d'`iroh-gossip`), et le point de terminaison privé garde sa liste
//! blanche. Il passe **par les relais seulement** : son adresse IP n'est pas donnée aux
//! inconnus (le relais la voit). Limite connue (relecture du 20/10) : Iroh joint le relais
//! qu'un autre nœud annonce, et un inconnu qui fait tourner son propre relais verrait donc
//! l'adresse IP de ce nœud ; le crochet de connexion d'Iroh 1.3 passe avant la recherche
//! d'adresse, il ne permet pas de l'empêcher ici. Sans relais (essais en local), direct.
//!
//! Le nœud trouve quelques autres clés par la DHT (rendezvous.rs), rejoint le sujet
//! « aiodos/public/1 » du groupe de discussion, et y diffuse son profil signé chaque
//! minute (public_profile.rs) ; il garde en mémoire les profils reçus depuis moins de
//! 5 minutes.

use std::{
    collections::{BTreeSet, HashMap},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, bail};
use iroh::{
    Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey, TransportAddr,
    address_lookup::{DnsAddressLookup, MemoryLookup, PkarrPublisher, PkarrResolver},
    endpoint::{AfterHandshakeOutcome, BeforeConnectOutcome, Connection, EndpointHooks, PortmapperConfig, presets},
    protocol::Router,
};
use iroh_gossip::{
    ALPN as GOSSIP_ALPN, Gossip, TopicId,
    api::{Event, GossipReceiver, GossipSender},
};
use iroh_mainline_address_lookup::DhtAddressLookup;
use n0_future::StreamExt;
use n0_mainline::Dht;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

use crate::{
    close,
    config::{Config, Directory, Relays},
    dns, identity,
    public_profile::{self, MAX_MESSAGE, Profile},
    rendezvous,
};

/// Fichier de la clé publique du nœud, à côté de celle de la machine.
pub const KEY_FILE: &str = "aiodos-public.key";
/// Diffusion du profil.
const PROFILE_EVERY: Duration = Duration::from_secs(60);
/// Inscription dans la DHT, et recherche d'autres nœuds s'il en manque.
const RENDEZVOUS_EVERY: Duration = Duration::from_secs(5 * 60);
/// Sans aucun voisin : nouvelle lecture après 20 s, puis 40, 80… jusqu'à 5 minutes.
const ALONE_RETRY: Duration = Duration::from_secs(20);
/// La session du groupe de discussion repart de zéro au bout de ce délai : iroh-gossip 0.101
/// garde sans limite les adresses que ses voisins lui donnent (relecture du 20/10), et un
/// inconnu pourrait le gonfler ; AIODOS la relance aussitôt (même clé).
const RENEW_EVERY: Duration = Duration::from_secs(6 * 3600);
/// Un nœud dont aucun profil n'est arrivé depuis ce délai sort de l'annuaire.
const FORGET_AFTER: Duration = Duration::from_secs(5 * 60);
/// Au plus autant de nœuds dans l'annuaire (les plus anciens sortent).
const MAX_NODES: usize = 1000;
/// En dessous de ce nombre de voisins, on cherche d'autres nœuds dans la DHT.
const WANTED_NEIGHBORS: usize = 4;
/// Clés proposées au groupe de discussion à chaque recherche.
const JOIN_AT_MOST: usize = 8;

fn topic() -> TopicId {
    let bytes: [u8; 32] = Sha256::digest(rendezvous::NETWORK.as_bytes()).into();
    TopicId::from_bytes(bytes)
}

/// Ce qu'AIODOS demande (`PUT /net/v1/public`).
#[derive(Debug, Deserialize)]
pub struct Wanted {
    pub enabled: bool,
    #[serde(flatten)]
    pub profile: Profile,
}

struct Seen {
    ts: u64,
    profile: Profile,
    at: Instant,
}

#[derive(Default)]
struct State {
    profile: Profile,
    key: Option<EndpointId>,
    neighbors: BTreeSet<EndpointId>,
    nodes: HashMap<EndpointId, Seen>,
    announced_at: Option<u64>,
    found: usize,
    rendezvous_error: Option<String>,
    error: Option<String>,
}

struct Session {
    router: Router,
    cancel: CancellationToken,
    task: tokio::task::JoinHandle<()>,
}

/// Le réseau public du compagnon : arrêté tant qu'AIODOS ne le demande pas.
pub struct Public {
    data_dir: PathBuf,
    relays: Relays,
    directory: Directory,
    bind: Option<std::net::SocketAddr>,
    dht_bootstrap: Option<Vec<String>>,
    lookup: Option<MemoryLookup>,
    state: Arc<Mutex<State>>,
    session: tokio::sync::Mutex<Option<Session>>,
}

impl Public {
    pub fn new(config: &Config) -> Self {
        Public {
            data_dir: config.data_dir.clone(),
            relays: config.relays.clone(),
            directory: config.directory,
            bind: config.bind,
            dht_bootstrap: config.public_dht_bootstrap.clone(),
            lookup: config.public_lookup.clone(),
            state: Arc::new(Mutex::new(State::default())),
            session: tokio::sync::Mutex::new(None),
        }
    }

    /// Démarre, arrête, ou met à jour le profil. → l'état (cf. `snapshot`).
    pub async fn configure(&self, wanted: Wanted) -> Value {
        let profile = wanted.profile.clean();
        let changed = {
            let mut state = self.state.lock().expect("état du réseau public");
            let changed = state.profile != profile;
            state.profile = profile;
            changed
        };
        let mut session = self.session.lock().await;
        if session.as_ref().is_some_and(|running| running.task.is_finished()) {
            // Le groupe de discussion s'est arrêté seul : la session repart de zéro
            if let Some(dead) = session.take() {
                stop(dead).await;
            }
        }
        match (wanted.enabled, session.is_some()) {
            (true, false) => match self.start().await {
                Ok(started) => {
                    *session = Some(started);
                    self.state.lock().expect("état").error = None;
                }
                Err(err) => {
                    warn!("Réseau public : démarrage impossible ({err:#})");
                    self.state.lock().expect("état").error = Some(format!("{err:#}"));
                }
            },
            (false, true) => {
                if let Some(running) = session.take() {
                    stop(running).await;
                }
                let mut state = self.state.lock().expect("état");
                let profile = std::mem::take(&mut state.profile);
                *state = State { profile, ..State::default() };
                info!("Réseau public quitté");
            }
            (true, true) if changed => debug!("Réseau public : profil changé"),
            _ => {}
        }
        drop(session);
        self.snapshot()
    }

    /// État pour AIODOS : la clé publique du nœud, ses voisins, les nœuds vus.
    pub fn snapshot(&self) -> Value {
        let state = self.state.lock().expect("état du réseau public");
        let mut nodes: Vec<(&EndpointId, &Seen)> =
            state.nodes.iter().filter(|(_, seen)| seen.at.elapsed() < FORGET_AFTER).collect();
        nodes.sort_by_key(|(_, seen)| seen.at.elapsed());
        json!({
            "enabled": state.key.is_some(),
            "key": state.key.map(|k| k.to_string()),
            "neighbors": state.neighbors.len(),
            "nodes": nodes.iter().map(|(id, seen)| json!({
                "key": id.to_string(),
                "version": seen.profile.version,
                "models": seen.profile.models,
                "lends": seen.profile.lends,
                "seen_s": seen.at.elapsed().as_secs(),
            })).collect::<Vec<_>>(),
            "rendezvous": {
                "announced_at": state.announced_at,
                "found": state.found,
                "error": state.rendezvous_error,
            },
            "error": state.error,
        })
    }

    /// Arrêt du compagnon : le réseau public est quitté proprement.
    pub async fn shutdown(&self) {
        if let Some(running) = self.session.lock().await.take() {
            stop(running).await;
        }
    }

    async fn start(&self) -> Result<Session> {
        if self.relays == Relays::Disabled && self.lookup.is_none() {
            bail!("le réseau public passe par les relais, et aucun relais n'est choisi");
        }
        let (secret, created) = identity::load_or_create_file(&self.data_dir, KEY_FILE)?;
        let endpoint = self.bind_endpoint(secret.clone()).await?;
        let id = endpoint.id();
        if let Some(lookup) = &self.lookup {
            // Essais en local : chaque nœud dit où le joindre directement
            lookup.add_endpoint_info(EndpointAddr {
                id,
                addrs: endpoint.bound_sockets().into_iter().map(TransportAddr::Ip).collect(),
            });
        }
        let gossip = Gossip::builder().max_message_size(MAX_MESSAGE).spawn(endpoint.clone());
        let router = Router::builder(endpoint).accept(GOSSIP_ALPN, gossip.clone()).spawn();
        let mut dht = Dht::builder();
        if let Some(bootstrap) = &self.dht_bootstrap {
            dht.bootstrap(bootstrap);
        }
        let dht = dht.build().context("DHT Mainline")?;
        let (sender, receiver) = gossip.subscribe(topic(), Vec::new()).await?.split();
        let cancel = CancellationToken::new();
        {
            let mut state = self.state.lock().expect("état");
            state.key = Some(id);
        }
        info!(
            "Réseau public rejoint : clé {} ({}){}",
            id.fmt_short(),
            if created { "nouvelle" } else { "existante" },
            if self.relays == Relays::Disabled { ", en direct (essais)" } else { ", par les relais seulement" },
        );
        let task = tokio::spawn(run(
            Node { id, secret, dht, sender, state: self.state.clone() },
            receiver,
            cancel.clone(),
        ));
        Ok(Session { router, cancel, task })
    }

    async fn bind_endpoint(&self, secret: SecretKey) -> Result<Endpoint> {
        let relay_mode = match &self.relays {
            Relays::N0 => RelayMode::Default,
            Relays::Custom(urls) => RelayMode::custom(urls.iter().cloned()),
            Relays::Disabled => RelayMode::Disabled,
        };
        let mut builder = Endpoint::builder(presets::Minimal)
            .secret_key(secret)
            .alpns(vec![GOSSIP_ALPN.to_vec()])
            .relay_mode(relay_mode)
            .portmapper_config(PortmapperConfig::Disabled)
            .hooks(PublicGate);
        if self.directory.n0_dns {
            builder = builder
                .address_lookup(PkarrPublisher::n0_dns())
                .address_lookup(PkarrResolver::n0_dns())
                .address_lookup(DnsAddressLookup::n0_dns());
        }
        if self.directory.dht {
            builder = builder.address_lookup(DhtAddressLookup::builder());
        }
        if let Some(lookup) = &self.lookup {
            builder = builder.address_lookup(lookup.clone());
        }
        if self.relays != Relays::Disabled {
            builder = builder.clear_ip_transports(); // les inconnus ne voient pas mon adresse IP
        } else if let Some(bind) = self.bind {
            builder = builder
                .clear_ip_transports()
                .bind_addr(std::net::SocketAddr::new(bind.ip(), 0))
                .with_context(|| format!("adresse d'écoute UDP refusée : {bind}"))?;
        }
        if let Some(resolver) = dns::resolver() {
            builder = builder.dns_resolver(resolver);
        }
        builder.bind().await.context("point de terminaison du réseau public")
    }
}

async fn stop(session: Session) {
    session.cancel.cancel();
    let _ = tokio::time::timeout(Duration::from_secs(3), session.router.shutdown()).await;
}

/// Le point de terminaison public n'accepte que le groupe de discussion public.
#[derive(Debug)]
struct PublicGate;

impl EndpointHooks for PublicGate {
    async fn before_connect<'a>(
        &'a self,
        _remote_addr: &'a iroh::EndpointAddr,
        alpn: &'a [u8],
    ) -> BeforeConnectOutcome {
        if alpn == GOSSIP_ALPN {
            BeforeConnectOutcome::Accept
        } else {
            BeforeConnectOutcome::Reject
        }
    }

    async fn after_handshake<'a>(&'a self, conn: &'a Connection) -> AfterHandshakeOutcome {
        if conn.alpn() == GOSSIP_ALPN {
            AfterHandshakeOutcome::Accept
        } else {
            AfterHandshakeOutcome::Reject { error_code: close::REFUSED.into(), reason: b"refused".to_vec() }
        }
    }
}

struct Node {
    id: EndpointId,
    secret: SecretKey,
    dht: Dht,
    sender: GossipSender,
    state: Arc<Mutex<State>>,
}

impl Node {
    async fn broadcast_profile(&self, neighbors_only: bool) {
        let profile = self.state.lock().expect("état").profile.clone();
        let message = match public_profile::seal(&self.secret, &profile, SystemTime::now()) {
            Ok(message) => message,
            Err(err) => return warn!("Réseau public : profil non diffusé ({err:#})"),
        };
        let sent = if neighbors_only {
            self.sender.broadcast_neighbors(message).await
        } else {
            self.sender.broadcast(message).await
        };
        if let Err(err) = sent {
            debug!("Réseau public : diffusion impossible ({err})");
        }
    }

    /// Inscription dans la DHT ; s'il manque des voisins, d'autres clés à rejoindre.
    async fn rendezvous(&self) {
        let now = SystemTime::now();
        let announced = rendezvous::announce(&self.dht, self.id, now).await;
        let neighbors = self.state.lock().expect("état").neighbors.len();
        let found = if neighbors < WANTED_NEIGHBORS {
            rendezvous::discover(&self.dht, self.id, now).await
        } else {
            Vec::new()
        };
        {
            let mut state = self.state.lock().expect("état");
            match &announced {
                Ok(_) => {
                    state.announced_at = Some(now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs());
                    state.rendezvous_error = None;
                }
                Err(err) => state.rendezvous_error = Some(format!("{err:#}")),
            }
            if neighbors < WANTED_NEIGHBORS {
                state.found = found.len();
            }
        }
        if let Err(err) = &announced {
            debug!("Réseau public : {err:#}");
        }
        if !found.is_empty() {
            let wanted: Vec<EndpointId> = found.into_iter().take(JOIN_AT_MOST).collect();
            if let Err(err) = self.sender.join_peers(wanted).await {
                debug!("Réseau public : nœuds trouvés non joints ({err})");
            }
        }
    }

    fn received(&self, content: &[u8]) {
        let (author, ts, profile) = match public_profile::open(content, SystemTime::now()) {
            Ok(opened) => opened,
            Err(err) => return debug!("Réseau public : message écarté ({err:#})"),
        };
        if author == self.id {
            return;
        }
        let mut state = self.state.lock().expect("état");
        if state.nodes.get(&author).is_some_and(|seen| seen.ts >= ts) {
            return; // pas plus récent que celui qu'on a
        }
        state.nodes.insert(author, Seen { ts, profile, at: Instant::now() });
        state.nodes.retain(|_, seen| seen.at.elapsed() < FORGET_AFTER);
        if state.nodes.len() > MAX_NODES {
            let oldest = state.nodes.iter().max_by_key(|(_, seen)| seen.at.elapsed()).map(|(id, _)| *id);
            if let Some(oldest) = oldest {
                state.nodes.remove(&oldest);
            }
        }
    }
}

async fn run(node: Node, mut receiver: GossipReceiver, cancel: CancellationToken) {
    let mut profile_tick = tokio::time::interval(PROFILE_EVERY);
    // Seul (aucun voisin), le nœud relit la DHT bien plus tôt : deux nœuds arrivés au même
    // instant peuvent l'avoir lue chacun avant l'inscription de l'autre
    let rendezvous_at = tokio::time::sleep(Duration::ZERO);
    tokio::pin!(rendezvous_at);
    let mut alone_retry = ALONE_RETRY;
    let renew_at = tokio::time::sleep(RENEW_EVERY);
    tokio::pin!(renew_at);
    let mut renewed = false;
    // Le rendez-vous dure quelques secondes (DHT) : à part, pour ne pas retarder les messages
    let node = Arc::new(node);
    let mut pending: Option<tokio::task::JoinHandle<()>> = None;
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            _ = &mut renew_at => {
                renewed = true;
                break;
            }
            _ = profile_tick.tick() => node.broadcast_profile(false).await,
            _ = &mut rendezvous_at => {
                if pending.as_ref().is_none_or(|task| task.is_finished()) {
                    let node = node.clone();
                    pending = Some(tokio::spawn(async move { node.rendezvous().await }));
                }
                let alone = node.state.lock().expect("état").neighbors.is_empty();
                let delay = if alone {
                    let delay = alone_retry;
                    alone_retry = (alone_retry * 2).min(RENDEZVOUS_EVERY);
                    delay
                } else {
                    alone_retry = ALONE_RETRY;
                    RENDEZVOUS_EVERY
                };
                rendezvous_at.as_mut().reset(tokio::time::Instant::now() + delay);
            }
            event = receiver.next() => match event {
                Some(Ok(Event::NeighborUp(id))) => {
                    node.state.lock().expect("état").neighbors.insert(id);
                    node.broadcast_profile(true).await; // le nouveau voisin nous connaît tout de suite
                }
                Some(Ok(Event::NeighborDown(id))) => {
                    node.state.lock().expect("état").neighbors.remove(&id);
                }
                Some(Ok(Event::Received(message))) => node.received(&message.content),
                Some(Ok(Event::Lagged)) => debug!("Réseau public : messages manqués"),
                Some(Err(err)) => {
                    warn!("Réseau public : groupe de discussion interrompu ({err})");
                    break;
                }
                None => break,
            },
        }
    }
    if let Some(task) = pending {
        task.abort();
    }
    if !cancel.is_cancelled() {
        // Renouvelée, ou arrêt imprévu : AIODOS le voit (plus de clé affichée) et redemande
        // le réseau public au tour suivant (network/public.py)
        let mut state = node.state.lock().expect("état");
        state.key = None;
        state.neighbors.clear();
        state.error = if renewed {
            None
        } else {
            Some("groupe de discussion public interrompu : nouvel essai".into())
        };
        if renewed {
            info!("Réseau public : session renouvelée");
        }
    }
}
