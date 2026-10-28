//! Réseau public qui calcule, côté demandeur : confier une conversation à la machine d'un
//! inconnu sans dire qui demande (docs/conception/reseau-public-calcul.md, § 3 et 4).
//!
//! `POST /net/v1/public/ask/<clé du prêteur>/public/v1/…` (API locale, jeton exigé) part
//! vers le nœud public du prêteur par `PUBLIC_ALPN` (cf. public_serve.rs), et sa réponse
//! revient **en flux**, morceau par morceau, sans être lue d'abord : une conversation
//! s'écrit mot à mot. Un Stop d'AIODOS (client parti) coupe le flux, ce qui arrête le
//! calcul chez le prêteur (cf. stream_io.rs).
//!
//! **Une clé jetable par conversation.** La requête nomme sa conversation
//! (`X-Aiodos-Conversation`, identifiant opaque choisi par AIODOS) ; pour chacune, le
//! compagnon crée un point de terminaison Iroh **neuf**, à clé tirée au hasard et jamais
//! écrite sur disque, **en relais seulement** (aucune socket UDP : ni adresse IP directe à
//! donner, ni perçage), qui ne sert qu'à demander : aucun ALPN accepté, toute connexion
//! entrante refusée, rien publié dans les annuaires. Le prêteur voit cette clé jetable et
//! l'adresse d'un relais ; jamais l'adresse IP de la machine, ni sa clé, ni celle de son
//! nœud public. Le point de terminaison est gardé tant que la conversation sert, pour que
//! ses messages suivants trouvent la connexion ouverte (quelques secondes de moins par un
//! relais), puis fermé et oublié avec sa clé `CONVERSATION_IDLE` après sa dernière requête ;
//! au plus `MAX_CONVERSATIONS` à la fois (au-delà, la plus anciennement servie est fermée).
//! Deux conversations ont deux clés : le prêteur ne peut pas les relier entre elles.
//!
//! **Joindre le prêteur par sa seule clé.** Le compagnon cherche l'adresse du relais du
//! prêteur dans ses annuaires (DNS de n0 et pkarr, puis DHT Mainline, selon
//! `--directory`), en lecture seulement : le nœud public du prêteur l'y publie (cf.
//! public.rs). Les annuaires sont lus l'un après l'autre, celui de n0 d'abord : n0 sait déjà
//! qui joint qui par ses relais, alors que la DHT est ouverte à tous, et un nœud de la DHT
//! proche de la clé du prêteur verrait quelle adresse IP la cherche. Puis la connexion passe
//! par ce relais, **seulement s'il est l'un des relais de ce compagnon** (ceux de n0 par
//! défaut, ou ceux choisis) : un prêteur qui annoncerait son propre relais y verrait
//! arriver l'adresse IP du demandeur (Iroh joint le relais que l'autre annonce, cf.
//! public.rs). D'où un point de terminaison jetable **sans annuaire** : Iroh lancerait
//! sinon sa propre recherche à la connexion, et en ajouterait les relais. Délai borné
//! (`Config::dial_timeout`, recherche comprise) ; échec : 502 `{"error": "unreachable"}`.
//!
//! Ce qui part chez le prêteur : le chemin, le corps (`MAX_BODY` au plus, lu en entier
//! d'abord), et les seuls `PASSED_HEADERS` ; jamais le jeton, l'identifiant de
//! conversation, ni les autres en-têtes d'AIODOS (`User-Agent`…). Ce qui revient : le code,
//! les seuls `RETURNED_HEADERS`, et le corps en flux. Refus du compagnon lui-même :
//! `{"error": "<code>", "detail": "…"}` (cf. `public_serve::refusal`).

use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow};
use http_body_util::{BodyExt, Full, Limited};
use hyper::{
    HeaderMap, Method, Request, Response, StatusCode,
    body::Incoming,
    header::{CONTENT_LENGTH, HOST, HeaderValue},
};
use hyper_util::rt::TokioIo;
use iroh::{
    Endpoint, EndpointAddr, EndpointId, RelayMap, RelayMode, SecretKey, TransportAddr,
    address_lookup::{AddressLookup, AddressLookupBuilder, DnsAddressLookup, MemoryLookup, PkarrResolver},
    endpoint::{
        AfterHandshakeOutcome, BeforeConnectOutcome, Connection, EndpointHooks, PortmapperConfig, Side,
        presets,
    },
};
use iroh_mainline_address_lookup::DhtAddressLookup;
use n0_future::StreamExt;
use tokio::task::JoinSet;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};

use crate::{
    CONVERSATION_HEADER, PUBLIC_ALPN, close,
    config::{Config, Directory, Relays},
    dns,
    public_serve::{self, MAX_BODY, PASSED_HEADERS, refusal, returned_headers},
    relay_body::{self, Progress},
    reply::{self, Body},
    stream_io::QuicIo,
};

/// Route de l'API locale (suivie de `<clé du prêteur>/public/v1/…`).
pub const ROUTE: &str = "/net/v1/public/ask/";
/// Identifiant de conversation : de 16 à 64 caractères `[A-Za-z0-9_-]`.
pub const CONVERSATION_MIN: usize = 16;
pub const CONVERSATION_MAX: usize = 64;
/// Une conversation sans requête depuis ce délai est oubliée, sa clé jetable avec elle.
pub const CONVERSATION_IDLE: Duration = Duration::from_secs(15 * 60);
/// Conversations (clés jetables) gardées à la fois.
pub const MAX_CONVERSATIONS: usize = 32;
/// Prêteurs joints par une même conversation, connexions gardées (la plus ancienne est
/// fermée au-delà) : un essai, puis une autre machine si la première est occupée.
const MAX_LENDERS_PER_CONVERSATION: usize = 4;
/// Passage du ménage des conversations oubliées.
const SWEEP_EVERY: Duration = Duration::from_secs(60);
/// Le corps d'AIODOS arrive dans ce délai (il l'envoie aussitôt).
const BODY_TIMEOUT: Duration = Duration::from_secs(30);
/// Début de la réponse du prêteur (code et en-têtes) attendu au plus pendant ce temps ; la
/// suite, en flux, n'est pas bornée ici (AIODOS attend le premier mot, puis coupe ou non).
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(120);
/// Fermeture d'un point de terminaison jetable (prévenir le prêteur), au plus.
const CLOSE_TIMEOUT: Duration = Duration::from_secs(3);

/// L'identifiant de conversation d'une requête, s'il est là (une seule fois) et valide.
pub fn conversation_id(headers: &HeaderMap) -> Option<String> {
    let mut values = headers.get_all(CONVERSATION_HEADER).iter();
    let (value, None) = (values.next()?, values.next()) else {
        return None;
    };
    let id = value.to_str().ok()?;
    valid_conversation(id).then(|| id.to_string())
}

/// 16 à 64 caractères, lettres ASCII, chiffres, `-` et `_`.
pub fn valid_conversation(id: &str) -> bool {
    (CONVERSATION_MIN..=CONVERSATION_MAX).contains(&id.len())
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Les conversations publiques de cette machine, chacune avec sa clé jetable.
pub struct Asker {
    relays: Relays,
    /// Les relais de ce compagnon : les seuls par lesquels joindre un prêteur.
    allowed: RelayMap,
    directory: Directory,
    bind: Option<SocketAddr>,
    dht_bootstrap: Option<Vec<String>>,
    lookup: Option<MemoryLookup>,
    dial_timeout: Duration,
    /// Annuaire de la DHT Mainline, en lecture seulement, partagé par toutes les clés
    /// jetables (un seul client de la DHT, créé au premier besoin).
    dht: OnceLock<Option<DhtAddressLookup>>,
    conversations: tokio::sync::Mutex<HashMap<String, Arc<Conversation>>>,
}

/// Une conversation : son point de terminaison jetable, ses annuaires (en lecture, dans
/// l'ordre où les lire), et les prêteurs qu'elle a joints.
struct Conversation {
    endpoint: Endpoint,
    directories: Vec<Box<dyn AddressLookup>>,
    lenders: tokio::sync::Mutex<HashMap<EndpointId, (Connection, Instant)>>,
    /// Dernière requête (début ou fin).
    used: Mutex<Instant>,
    in_flight: AtomicUsize,
}

/// Ce qu'on garde d'une adresse trouvée dans un annuaire : les relais permis, et les
/// adresses IP seulement sans relais (essais en local). `None` s'il ne reste rien.
fn usable(found: EndpointAddr, allowed: &RelayMap, direct: bool) -> Option<EndpointAddr> {
    let addrs: Vec<TransportAddr> = found
        .addrs
        .into_iter()
        .filter(|addr| match addr {
            TransportAddr::Relay(url) => allowed.contains(url),
            TransportAddr::Ip(_) => direct,
            _ => false,
        })
        .collect();
    (!addrs.is_empty()).then(|| EndpointAddr::from_parts(found.id, addrs))
}

impl Conversation {
    fn touch(&self) {
        *self.used.lock().expect("conversation") = Instant::now();
    }

    fn used(&self) -> Instant {
        *self.used.lock().expect("conversation")
    }

    fn busy(&self) -> bool {
        self.in_flight.load(Ordering::SeqCst) > 0
    }

    /// Une requête commence ; la conversation sert jusqu'à ce que le garde soit lâché (fin
    /// de la réponse, ou requête abandonnée).
    fn using(self: &Arc<Self>) -> Using {
        self.in_flight.fetch_add(1, Ordering::SeqCst);
        self.touch();
        Using(self.clone())
    }

    /// Un flux vers ce prêteur, sur la connexion gardée si elle vit encore, sinon sur une
    /// nouvelle, par un relais permis (une seule ouverture à la fois par conversation).
    async fn stream(&self, lender: EndpointId, dial: &Dial<'_>) -> Result<QuicIo, String> {
        let mut lenders = self.lenders.lock().await;
        // Un flux attend sa place si le prêteur en a déjà assez d'ouverts (cf.
        // public_serve::MAX_STREAMS_PER_CONNECTION) : pas au-delà du délai de connexion
        if let Some((conn, _)) = lenders.get(&lender) {
            if conn.close_reason().is_none()
                && let Ok(Ok((send, recv))) = tokio::time::timeout(dial.timeout, conn.open_bi()).await
            {
                return Ok(QuicIo::bare(send, recv));
            }
            lenders.remove(&lender);
        }
        let deadline = tokio::time::Instant::now() + dial.timeout;
        let Some(addr) = self.resolve(lender, dial, deadline).await else {
            return Err("adresse introuvable dans les annuaires, ou par un relais qui n'est pas des miens".into());
        };
        let conn = match tokio::time::timeout_at(deadline, self.endpoint.connect(addr, PUBLIC_ALPN)).await {
            Ok(Ok(conn)) => conn,
            Ok(Err(err)) => return Err(err.to_string()),
            Err(_) => return Err(format!("aucune réponse en {} s", dial.timeout.as_secs())),
        };
        let (send, recv) = match tokio::time::timeout_at(deadline, conn.open_bi()).await {
            Ok(Ok(pair)) => pair,
            Ok(Err(err)) => return Err(err.to_string()),
            Err(_) => return Err("aucun flux ouvert à temps".into()),
        };
        if lenders.len() >= MAX_LENDERS_PER_CONVERSATION {
            let oldest = lenders.iter().min_by_key(|(_, (_, at))| *at).map(|(id, _)| *id);
            if let Some((old, _)) = oldest.and_then(|id| lenders.remove(&id)) {
                old.close(close::STOPPING.into(), b"done");
            }
        }
        lenders.insert(lender, (conn, Instant::now()));
        Ok(QuicIo::bare(send, recv))
    }

    /// L'adresse du prêteur par un relais permis : annuaires lus l'un après l'autre (chacun
    /// `LOOKUP_EACH` au plus), jusqu'à une réponse utilisable ou `deadline`.
    async fn resolve(
        &self,
        lender: EndpointId,
        dial: &Dial<'_>,
        deadline: tokio::time::Instant,
    ) -> Option<EndpointAddr> {
        for directory in &self.directories {
            let Some(mut found) = directory.resolve(lender) else {
                continue;
            };
            let until = deadline.min(tokio::time::Instant::now() + LOOKUP_EACH);
            loop {
                match tokio::time::timeout_at(until, found.next()).await {
                    Ok(Some(Ok(item))) => {
                        let addr = item.into_endpoint_addr();
                        let relays: Vec<String> = addr.relay_urls().map(|url| url.to_string()).collect();
                        match usable(addr, dial.allowed, dial.direct) {
                            Some(addr) => return Some(addr),
                            None => {
                                debug!("Réseau public : {} annonce d'autres relais ({relays:?})", lender.fmt_short());
                            }
                        }
                    }
                    Ok(Some(Err(_))) => {}
                    Ok(None) | Err(_) => break, // annuaire suivant
                }
            }
            if tokio::time::Instant::now() >= deadline {
                break;
            }
        }
        None
    }
}

/// Comment joindre un prêteur : délai (recherche comprise), relais permis, et adresses IP
/// permises ou non (seulement sans relais, en essai local).
struct Dial<'a> {
    timeout: Duration,
    allowed: &'a RelayMap,
    direct: bool,
}

/// Lecture d'un annuaire, au plus (le suivant est lu ensuite).
const LOOKUP_EACH: Duration = Duration::from_secs(8);

fn relay_mode(relays: &Relays) -> RelayMode {
    match relays {
        Relays::N0 => RelayMode::Default,
        Relays::Custom(urls) => RelayMode::custom(urls.iter().cloned()),
        Relays::Disabled => RelayMode::Disabled,
    }
}

/// Une requête en cours dans une conversation (elle n'est pas oubliée pendant ce temps).
struct Using(Arc<Conversation>);

impl Drop for Using {
    fn drop(&mut self) {
        self.0.touch();
        self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
    }
}

impl Asker {
    pub fn new(config: &Config) -> Self {
        Asker {
            relays: config.relays.clone(),
            allowed: relay_mode(&config.relays).relay_map(),
            directory: config.directory,
            bind: config.bind,
            dht_bootstrap: config.public_dht_bootstrap.clone(),
            lookup: config.public_lookup.clone(),
            dial_timeout: config.dial_timeout,
            dht: OnceLock::new(),
            conversations: tokio::sync::Mutex::default(),
        }
    }

    /// `POST /net/v1/public/ask/<rest>` : relaie la requête au prêteur nommé, par la clé
    /// jetable de sa conversation, et rend sa réponse en flux.
    pub async fn ask(&self, req: Request<Incoming>, rest: &str) -> Response<Body> {
        if req.method() != Method::POST {
            return refusal(StatusCode::METHOD_NOT_ALLOWED, "method", "POST seulement.");
        }
        let (key, tail) = rest.split_once('/').unwrap_or((rest, ""));
        let Ok(lender) = key.parse::<EndpointId>() else {
            return refusal(StatusCode::BAD_REQUEST, "bad_key", "Clé du prêteur illisible.");
        };
        let path = format!("/{tail}");
        let query = req.uri().query().map(str::to_owned);
        if !public_serve::allowed_target(&path, query.as_deref()) {
            return refusal(StatusCode::NOT_FOUND, "not_found", "Seule l'API /public/v1/… d'un prêteur passe.");
        }
        let Some(conversation_id) = conversation_id(req.headers()) else {
            return refusal(
                StatusCode::BAD_REQUEST,
                "conversation",
                "En-tête X-Aiodos-Conversation manquant ou invalide (16 à 64 caractères A-Z a-z 0-9 _ -).",
            );
        };
        if self.relays == Relays::Disabled && self.lookup.is_none() {
            return refusal(
                StatusCode::BAD_GATEWAY,
                "unreachable",
                "Aucun relais : le réseau public passe par les relais.",
            );
        }
        if relay_body::announced_too_large(req.headers(), MAX_BODY) {
            return too_large();
        }
        let (parts, body) = req.into_parts();
        let body = match tokio::time::timeout(BODY_TIMEOUT, Limited::new(body, MAX_BODY).collect()).await {
            Ok(Ok(collected)) => collected.to_bytes(),
            Ok(Err(err)) if relay_body::is_too_large(err.as_ref()) => return too_large(),
            Ok(Err(_)) | Err(_) => {
                return refusal(StatusCode::BAD_REQUEST, "bad_request", "Requête interrompue.");
            }
        };
        let conversation = match self.conversation(&conversation_id).await {
            Ok(conversation) => conversation,
            Err(err) => {
                warn!("Réseau public : clé jetable impossible ({err:#})");
                let detail = format!("Clé jetable impossible : {err:#}.");
                return refusal(StatusCode::BAD_GATEWAY, "unreachable", detail);
            }
        };
        let using = conversation.using();
        let target = match &query {
            Some(query) => format!("{path}?{query}"),
            None => path,
        };
        let mut out = Request::new(reply::boxed(Full::new(body.clone())));
        *out.method_mut() = Method::POST;
        *out.uri_mut() = match target.parse() {
            Ok(uri) => uri,
            Err(_) => return refusal(StatusCode::NOT_FOUND, "not_found", "Chemin illisible."),
        };
        *out.headers_mut() = sent_headers(&parts.headers, body.len());
        let dial = Dial {
            timeout: self.dial_timeout,
            allowed: &self.allowed,
            direct: self.relays == Relays::Disabled,
        };
        let io = match conversation.stream(lender, &dial).await {
            Ok(io) => io,
            Err(err) => {
                debug!("Réseau public : {} injoignable ({err})", lender.fmt_short());
                return refusal(
                    StatusCode::BAD_GATEWAY,
                    "unreachable",
                    format!("Machine du réseau public injoignable : {err}."),
                );
            }
        };
        let exchange = async move {
            let (mut sender, conn) = hyper::client::conn::http1::handshake(TokioIo::new(io)).await?;
            tokio::spawn(async move {
                if let Err(err) = conn.await {
                    debug!("Réseau public : flux interrompu ({err})");
                }
            });
            sender.send_request(out).await
        };
        match tokio::time::timeout(RESPONSE_TIMEOUT, exchange).await {
            Ok(Ok(response)) => {
                let (mut head, body) = response.into_parts();
                head.headers = returned_headers(&head.headers);
                // La conversation sert tant que la réponse coule
                let body = relay_body::guarded(using, Arc::new(Progress::default()), reply::boxed(body));
                Response::from_parts(head, body)
            }
            Ok(Err(err)) => refusal(
                StatusCode::BAD_GATEWAY,
                "interrupted",
                format!("Échange interrompu avec la machine du réseau public : {err}."),
            ),
            Err(_) => refusal(
                StatusCode::GATEWAY_TIMEOUT,
                "timeout",
                format!("Pas de réponse de la machine du réseau public en {} s.", RESPONSE_TIMEOUT.as_secs()),
            ),
        }
    }

    /// La conversation de cet identifiant, avec sa clé jetable ; créée s'il le faut, après
    /// avoir oublié les conversations finies et fait de la place.
    async fn conversation(&self, id: &str) -> Result<Arc<Conversation>> {
        let mut conversations = self.conversations.lock().await;
        if let Some(existing) = conversations.get(id) {
            existing.touch();
            return Ok(existing.clone());
        }
        forget(&mut conversations, Instant::now(), MAX_CONVERSATIONS - 1);
        let conversation = Arc::new(self.open().await?);
        debug!("Réseau public : nouvelle clé jetable {}", conversation.endpoint.id().fmt_short());
        conversations.insert(id.to_string(), conversation.clone());
        Ok(conversation)
    }

    /// Une conversation neuve : un point de terminaison à clé tirée au hasard, relais
    /// seulement, sans annuaire, qui n'accepte rien ; et ses annuaires, en lecture seulement
    /// (aucun `PkarrPublisher` : la clé jetable n'est publiée nulle part), n0 d'abord.
    async fn open(&self) -> Result<Conversation> {
        let mut builder = Endpoint::builder(presets::Minimal)
            .secret_key(SecretKey::generate())
            .alpns(Vec::new())
            .relay_mode(relay_mode(&self.relays))
            .portmapper_config(PortmapperConfig::Disabled)
            .hooks(AskGate);
        if self.relays != Relays::Disabled {
            builder = builder.clear_ip_transports(); // le prêteur ne voit pas mon adresse IP
        } else if let Some(bind) = self.bind {
            // Essais en local, sans relais : en direct sur la boucle locale
            builder = builder
                .clear_ip_transports()
                .bind_addr(SocketAddr::new(bind.ip(), 0))
                .with_context(|| format!("adresse d'écoute UDP refusée : {bind}"))?;
        }
        if let Some(resolver) = dns::resolver() {
            builder = builder.dns_resolver(resolver);
        }
        let endpoint = builder.bind().await.context("point de terminaison jetable")?;
        let directories = match self.directories(&endpoint) {
            Ok(directories) => directories,
            Err(err) => {
                endpoint.close().await;
                return Err(err);
            }
        };
        // Personne n'a de raison de joindre une clé jetable : tout ce qui arrive est refusé
        // (la tâche s'arrête quand le point de terminaison est fermé)
        let refusing = endpoint.clone();
        tokio::spawn(async move {
            while let Some(incoming) = refusing.accept().await {
                incoming.refuse();
            }
        });
        Ok(Conversation {
            endpoint,
            directories,
            lenders: tokio::sync::Mutex::default(),
            used: Mutex::new(Instant::now()),
            in_flight: AtomicUsize::new(0),
        })
    }

    /// Les annuaires où chercher un prêteur, dans l'ordre : ceux de n0, puis la DHT (cf.
    /// en tête), puis celui des essais. Ils ne sont pas donnés au point de terminaison.
    fn directories(&self, endpoint: &Endpoint) -> Result<Vec<Box<dyn AddressLookup>>> {
        let mut directories: Vec<Box<dyn AddressLookup>> = Vec::new();
        if self.directory.n0_dns {
            let pkarr = PkarrResolver::n0_dns().into_address_lookup(endpoint);
            directories.push(Box::new(pkarr.map_err(|err| anyhow!("annuaire de n0 (pkarr) : {err}"))?));
            let dns = DnsAddressLookup::n0_dns().into_address_lookup(endpoint);
            directories.push(Box::new(dns.map_err(|err| anyhow!("annuaire de n0 (DNS) : {err}"))?));
        }
        if let Some(dht) = self.dht() {
            directories.push(Box::new(dht));
        }
        if let Some(lookup) = &self.lookup {
            directories.push(Box::new(lookup.clone()));
        }
        Ok(directories)
    }

    /// Annuaire de la DHT, en lecture seulement, s'il est choisi (`--directory`).
    fn dht(&self) -> Option<DhtAddressLookup> {
        if !self.directory.dht {
            return None;
        }
        self.dht
            .get_or_init(|| {
                let mut dht = n0_mainline::Dht::builder();
                if let Some(bootstrap) = &self.dht_bootstrap {
                    dht.bootstrap(bootstrap);
                }
                match DhtAddressLookup::builder().no_publish().dht_builder(dht).build() {
                    Ok(lookup) => Some(lookup),
                    Err(err) => {
                        warn!("Réseau public : annuaire de la DHT indisponible pour demander ({err})");
                        None
                    }
                }
            })
            .clone()
    }

    /// Oublie régulièrement les conversations finies, jusqu'à l'arrêt du compagnon.
    pub async fn sweep_loop(self: Arc<Self>, shutdown: CancellationToken) {
        loop {
            tokio::select! {
                _ = shutdown.cancelled() => break,
                _ = tokio::time::sleep(SWEEP_EVERY) => self.sweep(Instant::now()).await,
            }
        }
    }

    /// Oublie les conversations sans requête depuis `CONVERSATION_IDLE` à l'instant `now`.
    pub async fn sweep(&self, now: Instant) {
        forget(&mut *self.conversations.lock().await, now, MAX_CONVERSATIONS);
    }

    /// Nombre de conversations (clés jetables) gardées.
    pub async fn len(&self) -> usize {
        self.conversations.lock().await.len()
    }

    pub async fn is_empty(&self) -> bool {
        self.len().await == 0
    }

    /// Clé jetable de cette conversation (essais) : la crée si elle n'existe pas.
    #[doc(hidden)]
    pub async fn key_of(&self, conversation: &str) -> Result<EndpointId> {
        Ok(self.conversation(conversation).await?.endpoint.id())
    }

    /// Arrêt du compagnon : toutes les clés jetables sont fermées et oubliées.
    pub async fn shutdown(&self) {
        let drained: Vec<Arc<Conversation>> =
            self.conversations.lock().await.drain().map(|(_, conversation)| conversation).collect();
        let mut closing = JoinSet::new();
        for conversation in drained {
            closing.spawn(async move { conversation.endpoint.close().await });
        }
        let _ = tokio::time::timeout(CLOSE_TIMEOUT, closing.join_all()).await;
    }
}

/// Oublie les conversations sans requête depuis `CONVERSATION_IDLE` (aucune requête en
/// cours), puis les plus anciennement servies tant qu'il y en a plus de `keep` (celles sans
/// requête en cours d'abord). Leurs points de terminaison sont fermés à part.
fn forget(conversations: &mut HashMap<String, Arc<Conversation>>, now: Instant, keep: usize) {
    let mut gone: Vec<Arc<Conversation>> = Vec::new();
    conversations.retain(|_, conversation| {
        let expired =
            !conversation.busy() && now.saturating_duration_since(conversation.used()) >= CONVERSATION_IDLE;
        if expired {
            gone.push(conversation.clone());
        }
        !expired
    });
    while conversations.len() > keep {
        let oldest = conversations
            .iter()
            .min_by_key(|(_, conversation)| (conversation.busy(), conversation.used()))
            .map(|(id, _)| id.clone());
        match oldest.and_then(|id| conversations.remove(&id)) {
            Some(conversation) => gone.push(conversation),
            None => break,
        }
    }
    if !gone.is_empty() {
        info!("Réseau public : {} clé(s) jetable(s) oubliée(s)", gone.len());
    }
    for conversation in gone {
        tokio::spawn(async move {
            let _ = tokio::time::timeout(CLOSE_TIMEOUT, conversation.endpoint.close()).await;
        });
    }
}

/// En-têtes envoyés au prêteur : `PASSED_HEADERS` d'AIODOS, `Host` neutre, `Content-Length`.
fn sent_headers(from: &HeaderMap, length: usize) -> HeaderMap {
    let mut headers = HeaderMap::new();
    for name in PASSED_HEADERS {
        for value in from.get_all(*name) {
            headers.append(*name, value.clone());
        }
    }
    headers.insert(HOST, HeaderValue::from_static("aiodos"));
    headers.insert(CONTENT_LENGTH, HeaderValue::from(length));
    headers
}

fn too_large() -> Response<Body> {
    refusal(
        StatusCode::PAYLOAD_TOO_LARGE,
        "too_large",
        format!("Requête trop grosse pour le réseau public (au plus {} Kio).", MAX_BODY >> 10),
    )
}

/// Le point de terminaison jetable ne fait que demander : `PUBLIC_ALPN` en sortie
/// seulement, aucune connexion entrante.
#[derive(Debug)]
struct AskGate;

impl EndpointHooks for AskGate {
    async fn before_connect<'a>(
        &'a self,
        _remote_addr: &'a iroh::EndpointAddr,
        alpn: &'a [u8],
    ) -> BeforeConnectOutcome {
        if alpn == PUBLIC_ALPN {
            BeforeConnectOutcome::Accept
        } else {
            BeforeConnectOutcome::Reject
        }
    }

    async fn after_handshake<'a>(&'a self, conn: &'a Connection) -> AfterHandshakeOutcome {
        if conn.side() == Side::Client && conn.alpn() == PUBLIC_ALPN {
            AfterHandshakeOutcome::Accept
        } else {
            AfterHandshakeOutcome::Reject { error_code: close::REFUSED.into(), reason: b"refused".to_vec() }
        }
    }
}

#[cfg(test)]
mod tests {
    use iroh::{EndpointAddr, TransportAddr};

    use super::*;

    /// Demandeur d'essai : sans relais, en direct sur 127.0.0.1 (annuaire en mémoire).
    fn local_asker() -> Asker {
        let dir = std::env::temp_dir();
        let mut config = Config::local(dir, "127.0.0.1:9".parse().unwrap(), "jeton");
        config.public_lookup = Some(MemoryLookup::new());
        Asker::new(&config)
    }

    const A: &str = "conversation-aaaaaaaa";
    const B: &str = "conversation-bbbbbbbb";

    #[test]
    fn a_conversation_id_is_opaque_but_bounded() {
        for good in ["abcdefghijklmnop", "Ab-_09xyzXYZ-___", &"z".repeat(64), A] {
            assert!(valid_conversation(good), "{good}");
        }
        let too_long = "z".repeat(65);
        let bad = ["abcdefghijklmno", &too_long, "conversation avec espace", "conversation/../x0", "conversationééééé", ""];
        for bad in bad {
            assert!(!valid_conversation(bad), "{bad}");
        }
        let mut headers = HeaderMap::new();
        assert_eq!(conversation_id(&headers), None);
        headers.insert(CONVERSATION_HEADER, HeaderValue::from_static(A));
        assert_eq!(conversation_id(&headers).as_deref(), Some(A));
        headers.append(CONVERSATION_HEADER, HeaderValue::from_static(B));
        assert_eq!(conversation_id(&headers), None, "deux conversations à la fois : refusé");
    }

    #[test]
    fn only_content_type_and_accept_leave_for_the_lender() {
        let mut from = HeaderMap::new();
        from.insert("content-type", HeaderValue::from_static("application/json"));
        from.insert("accept", HeaderValue::from_static("application/x-ndjson"));
        from.insert("x-aiodos-net-token", HeaderValue::from_static("jeton"));
        from.insert(CONVERSATION_HEADER, HeaderValue::from_static(A));
        from.insert("user-agent", HeaderValue::from_static("python-httpx/0.28"));
        from.insert("host", HeaderValue::from_static("127.0.0.1:47800"));
        let headers = sent_headers(&from, 7);
        assert_eq!(headers.len(), 4);
        assert_eq!(headers["host"], "aiodos");
        assert_eq!(headers["content-length"], "7");
        assert!(headers.get(CONVERSATION_HEADER).is_none() && headers.get("user-agent").is_none());
    }

    #[test]
    fn a_lender_is_joined_only_through_one_of_my_relays() {
        let lender = SecretKey::generate().public();
        let n0 = RelayMode::Default.relay_map();
        let ours = n0.urls::<Vec<iroh::RelayUrl>>()[0].clone();
        let theirs: iroh::RelayUrl = "https://relais.du-preteur.example".parse().unwrap();
        let ip = TransportAddr::Ip("203.0.113.9:4242".parse().unwrap());
        let found = |addrs: Vec<TransportAddr>| EndpointAddr::from_parts(lender, addrs);
        // Son propre relais seulement : il y verrait mon adresse IP, pas joint
        assert!(usable(found(vec![TransportAddr::Relay(theirs.clone())]), &n0, false).is_none());
        // Le sien et l'un des miens : seul le mien reste
        let both = vec![TransportAddr::Relay(theirs), TransportAddr::Relay(ours.clone()), ip.clone()];
        let kept = usable(found(both), &n0, false).unwrap();
        assert_eq!(kept.addrs.into_iter().collect::<Vec<_>>(), vec![TransportAddr::Relay(ours)]);
        // Des adresses IP : seulement sans relais (essais en local)
        assert!(usable(found(vec![ip.clone()]), &n0, false).is_none());
        assert!(usable(found(vec![ip]), &RelayMode::Disabled.relay_map(), true).is_some());
    }

    #[tokio::test]
    async fn one_disposable_key_per_conversation_then_forgotten() {
        let asker = local_asker();
        let a = asker.key_of(A).await.unwrap();
        assert_eq!(asker.key_of(A).await.unwrap(), a, "même conversation, même clé");
        let b = asker.key_of(B).await.unwrap();
        assert_ne!(a, b, "autre conversation, autre clé");
        // Une requête en cours : la conversation n'est pas oubliée
        let using = asker.conversations.lock().await[A].using();
        asker.sweep(Instant::now() + CONVERSATION_IDLE + Duration::from_secs(1)).await;
        assert_eq!(asker.len().await, 1, "seule B, finie depuis longtemps, est oubliée");
        drop(using);
        assert_eq!(asker.key_of(A).await.unwrap(), a);
        // Finie depuis 15 minutes : oubliée avec sa clé ; revenue, elle en a une autre
        asker.sweep(Instant::now() + CONVERSATION_IDLE + Duration::from_secs(1)).await;
        assert!(asker.is_empty().await);
        let again = asker.key_of(A).await.unwrap();
        assert!(again != a && again != b);
        asker.shutdown().await;
        assert!(asker.is_empty().await);
    }

    #[tokio::test]
    async fn at_most_thirty_two_conversations_the_oldest_is_closed() {
        let asker = local_asker();
        let first = asker.key_of("conversation-premiere").await.unwrap();
        let endpoint = asker.conversations.lock().await["conversation-premiere"].endpoint.clone();
        for i in 1..=MAX_CONVERSATIONS {
            asker.key_of(&format!("conversation-{i:08}")).await.unwrap();
        }
        assert_eq!(asker.len().await, MAX_CONVERSATIONS);
        assert!(!asker.conversations.lock().await.contains_key("conversation-premiere"));
        tokio::time::timeout(Duration::from_secs(10), endpoint.closed()).await.expect("fermée");
        assert_ne!(asker.key_of("conversation-premiere").await.unwrap(), first);
        asker.shutdown().await;
    }

    #[tokio::test]
    async fn a_disposable_key_has_no_udp_socket_behind_a_relay() {
        let mut config = Config::local(std::env::temp_dir(), "127.0.0.1:9".parse().unwrap(), "jeton");
        config.relays = Relays::Custom(vec!["http://127.0.0.1:9".parse().unwrap()]);
        let asker = Asker::new(&config);
        asker.key_of(A).await.unwrap();
        let endpoint = asker.conversations.lock().await[A].endpoint.clone();
        assert!(endpoint.bound_sockets().is_empty(), "relais seulement : aucune adresse IP");
        asker.shutdown().await;
    }

    #[tokio::test]
    async fn nobody_can_connect_to_a_disposable_key() {
        let asker = local_asker();
        asker.key_of(A).await.unwrap();
        let target = asker.conversations.lock().await[A].endpoint.clone();
        let addr = EndpointAddr::from_parts(target.id(), target.bound_sockets().into_iter().map(TransportAddr::Ip));
        let caller = Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled)
            .clear_ip_transports()
            .bind_addr("127.0.0.1:0".parse::<SocketAddr>().unwrap())
            .unwrap()
            .bind()
            .await
            .unwrap();
        for alpn in [PUBLIC_ALPN, crate::ALPN] {
            let tried = tokio::time::timeout(Duration::from_secs(10), caller.connect(addr.clone(), alpn)).await;
            assert!(matches!(tried, Ok(Err(_))), "connexion entrante refusée");
        }
        caller.close().await;
        asker.shutdown().await;
    }
}
