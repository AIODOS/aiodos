//! Réseau public, phase 3.0 : un annuaire, sans rien confier (docs/phase3-reseau-public.md).
//!
//! AIODOS le démarre quand « Rejoindre le réseau public » est activé (`PUT /net/v1/public`),
//! ce qui est le cas par défaut depuis le 27/10/2026 (décision de l'auteur). Réglage
//! coupé : rien ne tourne et rien n'est annoncé.
//!
//! Un nœud public a sa **propre clé**, distincte de celle de la machine : ni ses amis ni
//! les inconnus ne peuvent relier son identité publique à ses machines appairées. Son
//! **point de terminaison est à part** aussi : il n'accepte que le groupe de discussion
//! public (ALPN d'`iroh-gossip`) et, depuis le 28/10/2026, les conversations que des
//! inconnus lui confient (`PUBLIC_ALPN`, de toute clé, en entrée seulement : cf.
//! public_serve.rs) ; le point de terminaison privé garde sa liste blanche. Il passe **par
//! les relais seulement** : son adresse IP n'est pas donnée aux inconnus (le relais la
//! voit). Limite connue (relecture du 20/10) : Iroh joint le relais qu'un autre nœud
//! annonce, et un inconnu qui fait tourner son propre relais verrait donc l'adresse IP de
//! ce nœud ; le crochet de connexion d'Iroh 1.3 passe avant la recherche d'adresse, il ne
//! permet pas de l'empêcher ici. Le demandeur d'une conversation, lui, s'en garde : sa clé
//! jetable ne joint un prêteur que par l'un de ses propres relais, adresse cherchée par le
//! compagnon et non par Iroh (cf. public_ask.rs). Sans relais (essais en local), direct.
//!
//! Joint par sa seule clé : le nœud publie l'adresse de son relais (jamais ses adresses IP,
//! filtre par défaut d'Iroh) dans l'annuaire DNS de n0 (pkarr) et dans la DHT Mainline,
//! selon `--directory` ; c'est ainsi que le groupe de discussion rejoint les clés trouvées
//! par le rendez-vous, et que la clé jetable d'un demandeur le trouve (cf. public_ask.rs).
//!
//! Le nœud trouve quelques autres clés par la DHT (rendezvous.rs), rejoint le sujet
//! « aiodos/public/1 » du groupe de discussion, et y diffuse son profil signé chaque
//! minute (public_profile.rs), et dans les 5 s quand il change (`CHANGE_GAP` : places
//! libres, prêt) ; il garde en mémoire les profils reçus depuis moins de
//! 5 minutes. Les conversations ne sont relayées à AIODOS que tant que le nœud tourne
//! (réglage « Rejoindre le réseau public ») et que son profil dit qu'il prête (`lends`).

use std::{
    collections::{BTreeSet, HashMap},
    net::SocketAddr,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, bail};
use iroh::{
    Endpoint, EndpointAddr, EndpointId, RelayMode, SecretKey, TransportAddr,
    address_lookup::{DnsAddressLookup, MemoryLookup, PkarrPublisher, PkarrResolver},
    endpoint::{
        AfterHandshakeOutcome, BeforeConnectOutcome, Connection, EndpointHooks, PortmapperConfig,
        QuicTransportConfig, Side, VarInt, presets,
    },
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
    PUBLIC_ALPN, close,
    config::{Config, Directory, Relays},
    dns, identity,
    public_profile::{self, MAX_MESSAGE, Profile},
    public_serve::{self, Limits},
    rendezvous,
};

/// Fichier de la clé publique du nœud, à côté de celle de la machine.
pub const KEY_FILE: &str = "aiodos-public.key";
/// Diffusion du profil.
const PROFILE_EVERY: Duration = Duration::from_secs(60);
/// Un profil changé (places libres, prêt, modèles, vitesse) part sans attendre la minute,
/// mais au plus tôt ce délai après la diffusion précédente : chaque profil est daté à la
/// seconde, et les autres ignorent celui qui n'est pas plus récent que le leur (essai réel
/// du 28/10/2026 : « free » vu avec jusqu'à 90 s de retard, et un téléphone sans modèle
/// refusait de converser alors que la machine qui prête était libre).
const CHANGE_GAP: Duration = Duration::from_secs(5);
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
/// Flux à sens unique qu'un autre nœud peut ouvrir en même temps sur une connexion : le
/// groupe de discussion en ouvre un par sujet ; sur une conversation, personne ne les lit.
pub const MAX_UNI_STREAMS: u32 = 4;
/// Octets qu'un autre nœud peut envoyer sur un flux avant qu'ils soient lus : une requête
/// entière (`public_serve::MAX_BODY`, en-têtes compris) tient dans un flux.
pub const STREAM_WINDOW: u32 = 512 << 10;
/// Octets qu'un autre nœud peut envoyer sur une connexion, tous flux confondus, avant qu'ils
/// soient lus : ce que la connexion d'un inconnu tient au plus en mémoire.
pub const CONNECTION_WINDOW: u32 = 1 << 20;
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
    bind: Option<SocketAddr>,
    dht_bootstrap: Option<Vec<String>>,
    lookup: Option<MemoryLookup>,
    /// Serveur réseau local d'AIODOS, qui reçoit aussi les conversations des inconnus.
    forward: SocketAddr,
    /// Réponse qu'un inconnu ne lit plus depuis ce temps : flux coupé.
    response_stall: Duration,
    /// Le profil dit que cette machine prête aux inconnus (sinon 503 `not_lending`).
    lends: Arc<AtomicBool>,
    /// Bornes des conversations servies aux inconnus, gardées d'une session à l'autre (en
    /// mémoire seulement).
    limits: Limits,
    state: Arc<Mutex<State>>,
    session: tokio::sync::Mutex<Option<Session>>,
    /// Le profil a changé : la session en cours le diffuse bientôt (`CHANGE_GAP`).
    changed: Arc<tokio::sync::Notify>,
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
            forward: config.forward,
            response_stall: config.response_stall,
            lends: Arc::new(AtomicBool::new(false)),
            limits: Limits::default(),
            state: Arc::new(Mutex::new(State::default())),
            session: tokio::sync::Mutex::new(None),
            changed: Arc::new(tokio::sync::Notify::new()),
        }
    }

    /// Démarre, arrête, ou met à jour le profil. → l'état (cf. `snapshot`).
    pub async fn configure(&self, wanted: Wanted) -> Value {
        let profile = wanted.profile.clean();
        self.lends.store(profile.lends, Ordering::SeqCst);
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
            (true, true) if changed => {
                debug!("Réseau public : profil changé");
                self.changed.notify_one(); // diffusé sans attendre la minute (cf. run)
            }
            _ => {}
        }
        drop(session);
        self.snapshot()
    }

    /// État pour AIODOS : la clé publique du nœud, ses voisins, les nœuds vus (avec leurs
    /// places libres et leur vitesse, `null` si leur profil ne les dit pas), et les
    /// conversations servies aux inconnus.
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
                "free": seen.profile.free,
                "tps": seen.profile.tps,
                "seen_s": seen.at.elapsed().as_secs(),
            })).collect::<Vec<_>>(),
            "served": self.limits.snapshot(),
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
            // Essais en local : chaque nœud dit où le joindre, en direct ou par ses relais
            // (comme l'annuaire de n0 le dirait de son relais)
            let relays = match &self.relays {
                Relays::Custom(urls) => urls.clone(),
                _ => Vec::new(),
            };
            lookup.add_endpoint_info(EndpointAddr {
                id,
                addrs: endpoint
                    .bound_sockets()
                    .into_iter()
                    .map(TransportAddr::Ip)
                    .chain(relays.into_iter().map(TransportAddr::Relay))
                    .collect(),
            });
        }
        let gossip = Gossip::builder().max_message_size(MAX_MESSAGE).spawn(endpoint.clone());
        let serve = public_serve::Serve::new(
            self.forward,
            self.response_stall,
            self.lends.clone(),
            self.limits.clone(),
        );
        let router = Router::builder(endpoint)
            .accept(GOSSIP_ALPN, gossip.clone())
            .accept(PUBLIC_ALPN, serve)
            .spawn();
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
            self.changed.clone(),
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
        // Flux bidirectionnels qu'un inconnu peut ouvrir en même temps sur une connexion :
        // ceux d'une conversation (cf. public_serve.rs) ; le groupe de discussion n'en ouvre
        // aucun. Et ce qu'il peut faire garder sans que rien ne le lise (relecture du
        // 28/10/2026) : avec les valeurs d'Iroh, 100 flux à sens unique de 1,25 Mo chacun
        // (125 Mo par connexion, que personne ne lit sur une conversation) et des
        // datagrammes ; ici quelques flux, 1 Mio par connexion, aucun datagramme
        let transport = QuicTransportConfig::builder()
            .max_concurrent_bidi_streams(VarInt::from_u32(public_serve::MAX_STREAMS_PER_CONNECTION as u32))
            .max_concurrent_uni_streams(VarInt::from_u32(MAX_UNI_STREAMS))
            .stream_receive_window(VarInt::from_u32(STREAM_WINDOW))
            .receive_window(VarInt::from_u32(CONNECTION_WINDOW))
            .datagram_receive_buffer_size(None)
            .build();
        let mut builder = Endpoint::builder(presets::Minimal)
            .secret_key(secret)
            .alpns(vec![GOSSIP_ALPN.to_vec(), PUBLIC_ALPN.to_vec()])
            .relay_mode(relay_mode)
            .transport_config(transport)
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

/// Le point de terminaison public n'accepte que le groupe de discussion public, dans les
/// deux sens, et les conversations des inconnus (`PUBLIC_ALPN`), de toute clé, en entrée
/// seulement : ce nœud ne demande jamais rien lui-même (le demandeur a sa clé jetable, cf.
/// public_ask.rs), sa clé publique n'est jamais celle qui demande.
#[derive(Debug)]
struct PublicGate;

/// Ce que la porte du nœud public laisse passer : ALPN, et sens de la connexion.
fn gate_allows(alpn: &[u8], side: Side) -> bool {
    alpn == GOSSIP_ALPN || (alpn == PUBLIC_ALPN && side == Side::Server)
}

impl EndpointHooks for PublicGate {
    async fn before_connect<'a>(
        &'a self,
        _remote_addr: &'a iroh::EndpointAddr,
        alpn: &'a [u8],
    ) -> BeforeConnectOutcome {
        if gate_allows(alpn, Side::Client) {
            BeforeConnectOutcome::Accept
        } else {
            BeforeConnectOutcome::Reject
        }
    }

    async fn after_handshake<'a>(&'a self, conn: &'a Connection) -> AfterHandshakeOutcome {
        if gate_allows(conn.alpn(), conn.side()) {
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

async fn run(node: Node, mut receiver: GossipReceiver, changed: Arc<tokio::sync::Notify>, cancel: CancellationToken) {
    // Le profil part chaque minute, et sans attendre quand il change : la minute repart
    // alors de cette diffusion, au plus tôt CHANGE_GAP après la précédente (une seule
    // diffusion par seconde : cf. CHANGE_GAP)
    let mut profile_tick = tokio::time::interval(PROFILE_EVERY);
    let mut broadcast_at: Option<tokio::time::Instant> = None;
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
            _ = profile_tick.tick() => {
                node.broadcast_profile(false).await;
                broadcast_at = Some(tokio::time::Instant::now());
            }
            _ = changed.notified() => {
                let now = tokio::time::Instant::now();
                match broadcast_at.map(|at| at + CHANGE_GAP) {
                    Some(soonest) if soonest > now => profile_tick.reset_at(soonest),
                    _ => profile_tick.reset_immediately(),
                }
            }
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
                    broadcast_at = Some(tokio::time::Instant::now());
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
