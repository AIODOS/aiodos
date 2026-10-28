//! Réseau public qui calcule, côté prêteur : les conversations que des inconnus confient à
//! cette machine (docs/conception/reseau-public-calcul.md, § 3 à 5).
//!
//! Le nœud public (public.rs) accepte `PUBLIC_ALPN` de **toute clé**, sans appairage ni
//! liste blanche : un demandeur s'y présente avec une clé jetable, tirée pour sa
//! conversation (cf. public_ask.rs). Chaque flux est un échange HTTP/1.1 relayé vers le
//! même serveur réseau local d'AIODOS que le chemin privé (inbound.rs), qui vérifie à son
//! tour (prêt aux inconnus permis, places, modèle, bornes de la demande). Seules passent
//! les requêtes `POST /public/v1/…`, aux chemins sans détour (ni `..`, ni `//`, ni `%`) :
//! le reste reçoit un 404 sans que rien ne soit relayé.
//!
//! Ce qu'AIODOS reçoit : le chemin, le corps (lu en entier avant tout, `MAX_BODY` au plus),
//! deux en-têtes de l'appelant (`PASSED_HEADERS`), et `X-Aiodos-Public` : la clé de
//! l'appelant, authentifiée par la poignée de main TLS. Tout le reste est laissé de côté,
//! plutôt que retiré au cas par cas : un `X-Aiodos-Peer` (un inconnu ne passe jamais pour
//! une machine appairée), un faux `X-Aiodos-Public`, le jeton, un `X-Forwarded-For` (que le
//! serveur local croirait peut-être, venu de 127.0.0.1), les en-têtes propres à un tronçon,
//! et ce qui en dirait trop sur le demandeur (`User-Agent`…). La réponse revient en flux,
//! avec ses seuls `RETURNED_HEADERS`.
//!
//! Une clé jetable par conversation rend une borne par clé contournable : il y a donc aussi
//! des plafonds pour tout le public (§ 5). Par clé : `PER_KEY_IN_FLIGHT` requête à la fois,
//! `PER_KEY_PER_HOUR` par heure ; pour tous : `GLOBAL_IN_FLIGHT` à la fois,
//! `GLOBAL_PER_MINUTE` servies par minute (acceptées par AIODOS : réponse 200) et
//! `GLOBAL_RELAYED_PER_MINUTE` relayées en tout, refus d'AIODOS compris : des requêtes
//! qu'AIODOS refuse vite (mal formées, machine occupée…) n'épuisent plus les admissions
//! des conversations servies (relecture du 28/10), sans qu'on relaie sans borne pour
//! autant. Au-delà, 503 `{"error": "busy"}` aussitôt (le demandeur passe à une autre
//! machine), avant même de lire le corps quand c'est déjà joué. Profil qui ne prête pas :
//! 503 `{"error": "not_lending"}`, sans rien relayer. Les compteurs vivent en mémoire
//! seulement et oublient une clé inactive depuis `FORGET_KEY_AFTER` : rien n'est gardé de
//! l'appelant au-delà. Une place est tenue jusqu'à la fin de la réponse (cf.
//! relay_body::guarded).
//!
//! Et ce qu'un inconnu pourrait tenir à peu de frais, avec une clé jetable par connexion :
//! - connexions (`MAX_CONNECTIONS`) : une connexion ne garde sa place qu'en avançant.
//!   Au-delà du plafond, la nouvelle prend celle d'une connexion qui n'a jamais été servie
//!   (la plus ancienne d'abord : en-têtes jamais finis, refus seulement, rien demandé), puis
//!   de la plus anciennement servie ; jamais celle d'une connexion dont une requête avance
//!   (corps en lecture, AIODOS qui répond, réponse en cours), dont le nombre est borné par
//!   `MAX_READING` et `GLOBAL_IN_FLIGHT`. Des en-têtes partiels renouvelés ne protègent
//!   donc plus rien (relecture du 28/10 : 32 connexions ainsi tenues refusaient toutes les
//!   autres et faisaient fermer les connexions servies au repos). Fermées aussi après
//!   `CONNECTION_IDLE` sans flux ;
//! - flux par connexion (`MAX_STREAMS_PER_CONNECTION` : borne de QUIC sur le nœud public,
//!   cf. public.rs, et remise à zéro de ceux qui passeraient quand même), en-têtes
//!   (`HEADER_TIMEOUT`, `MAX_HEAD`, `MAX_HEADERS`) ;
//! - corps lus en même temps (`MAX_READING`), chacun coupé après `BODY_IDLE` de silence,
//!   sous le débit minimal `BODY_PACE` après sa grâce, et en `BODY_TIMEOUT` au plus ; toutes
//!   les places prises, une nouvelle requête prend celle du corps le plus lent (cf.
//!   public_reading.rs). Un goutte-à-goutte ne tient donc plus une place de lecture que
//!   quelques secondes, et pas contre une requête qui arrive (relecture du 28/10 : 30 s pour
//!   presque rien) ;
//! - réponse que l'appelant ne lit plus (coupée après `Config::response_stall`, cf.
//!   relay_body.rs), et ce que QUIC garde sans que rien ne le lise (flux à sens unique,
//!   données en attente, datagrammes : `public::MAX_UNI_STREAMS`,
//!   `public::CONNECTION_WINDOW` par connexion, aucun datagramme ; aussi sur les connexions
//!   du groupe de discussion).
//!
//! La mémoire que les conversations d'inconnus tiennent reste ainsi de quelques dizaines de
//! mégaoctets au plus, même sur un téléphone.

use std::{
    collections::{HashMap, VecDeque},
    convert::Infallible,
    fmt,
    net::SocketAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use http_body_util::Full;
use hyper::{
    HeaderMap, Method, Request, Response, StatusCode,
    body::Incoming,
    header::{CONNECTION, CONTENT_LENGTH, HOST, HeaderName, HeaderValue},
    server::conn::http1,
    service::service_fn,
};
use hyper_util::rt::{TokioIo, TokioTimer};
use iroh::{
    EndpointId,
    endpoint::Connection,
    protocol::{AcceptError, ProtocolHandler},
};
use serde_json::{Value, json};
use tokio::task::JoinSet;
use tracing::debug;

use crate::{
    PUBLIC_HEADER, close, inbound,
    public_reading::{Readers, Unread},
    relay_body::{self, Pace, Progress},
    reply::{self, Body},
    stream_io::QuicIo,
};

/// Seule API servie aux inconnus : `POST /public/v1/…` d'AIODOS.
pub const PATH_PREFIX: &str = "/public/v1/";
/// Taille maximale d'une requête : du texte seulement (une conversation et ses options).
pub const MAX_BODY: usize = 256 << 10;
/// Requêtes en cours d'une même clé (une conversation à la fois).
pub const PER_KEY_IN_FLIGHT: usize = 1;
/// Requêtes admises d'une même clé, sur une heure glissante.
pub const PER_KEY_PER_HOUR: usize = 30;
/// Requêtes du public en cours en même temps, toutes clés confondues : une clé jetable par
/// conversation ne donne pas plus de places à qui en tire beaucoup. AIODOS borne aussi de
/// son côté (places de la machine, cf. node/places.py).
pub const GLOBAL_IN_FLIGHT: usize = 2;
/// Requêtes du public servies sur une minute glissante, toutes clés confondues : acceptées
/// par AIODOS (réponse 200), ou relayées et pas encore répondues.
pub const GLOBAL_PER_MINUTE: usize = 120;
/// Requêtes du public relayées à AIODOS sur une minute glissante, toutes clés confondues,
/// qu'il les serve ou les refuse : une borne sur le total, plus large, pour que des refus
/// rapides n'épuisent pas `GLOBAL_PER_MINUTE` sans que le compagnon relaie sans fin.
pub const GLOBAL_RELAYED_PER_MINUTE: usize = 5 * GLOBAL_PER_MINUTE;
/// Une clé sans requête depuis ce délai est oubliée (ses compteurs avec elle). Les clés
/// gardées sont donc au plus `GLOBAL_RELAYED_PER_MINUTE` × 10 (6 000), une centaine
/// d'octets chacune pour une clé jetable (une requête) : pas besoin d'autre borne.
pub const FORGET_KEY_AFTER: Duration = Duration::from_secs(10 * 60);
const MINUTE: Duration = Duration::from_secs(60);
const HOUR: Duration = Duration::from_secs(3600);
/// Connexions d'inconnus ouvertes en même temps sur le nœud public (cf. `Conns::enter`).
pub const MAX_CONNECTIONS: usize = 32;
/// Flux servis en même temps sur une connexion : une requête, et la suivante qui attend.
pub const MAX_STREAMS_PER_CONNECTION: usize = 2;
/// Connexion sans aucun flux depuis ce délai : fermée (le demandeur en rouvre une).
pub const CONNECTION_IDLE: Duration = Duration::from_secs(120);
/// En-têtes d'une requête attendus au plus pendant ce temps après l'ouverture du flux.
pub const HEADER_TIMEOUT: Duration = Duration::from_secs(10);
/// Taille de la tête d'une requête (ligne et en-têtes ; 8 Kio au moins pour hyper).
const MAX_HEAD: usize = 16 << 10;
/// Nombre d'en-têtes d'une requête (au-delà, 431).
const MAX_HEADERS: usize = 32;
/// Corps lus en même temps, toutes connexions confondues (`MAX_BODY` chacun au plus) ; une
/// place ne se garde qu'en avançant (cf. public_reading.rs).
pub const MAX_READING: usize = 8;
/// Silence maximal pendant la lecture d'un corps, premier morceau compris.
pub const BODY_IDLE: Duration = Duration::from_secs(4);
/// Débit minimal d'un corps, en moyenne depuis son début, après une courte grâce (comme
/// `relay_body::PACE` entre machines appairées, en plus court : une requête d'inconnu est
/// petite, et public_ask.rs l'envoie d'un coup). Une conversation de 32 Kio a 6 s, une
/// requête de 256 Kio 20 s ; un goutte-à-goutte ne tient une place de lecture que 4 s au
/// plus.
pub const BODY_PACE: Pace = Pace { grace: Duration::from_secs(4), min_rate: 16 * 1024 };
/// Le corps entier arrive dans ce délai, quoi qu'il arrive (256 Kio au débit minimal).
pub const BODY_TIMEOUT: Duration = Duration::from_secs(20);
/// Longueur du chemin et de la requête (`?…`) relayés.
const MAX_TARGET: usize = 256;
/// Seuls en-têtes de l'appelant transmis à AIODOS (outre ceux que le compagnon pose).
pub const PASSED_HEADERS: &[&str] = &["content-type", "accept"];
/// Seuls en-têtes de la réponse renvoyés à l'appelant (et `Content-Length`, que hyper
/// recalcule).
pub const RETURNED_HEADERS: &[&str] = &["content-type", "retry-after"];
/// Code de remise à zéro d'un flux de trop sur une connexion.
const STREAM_REFUSED: u32 = 503;

// ------------------------------------------------------------ bornes

/// Compteurs des requêtes du public, par clé et pour tous, en mémoire seulement.
#[derive(Debug, Clone, Default)]
pub struct Limits(Arc<Mutex<Counters>>);

#[derive(Debug, Default)]
struct Counters {
    keys: HashMap<EndpointId, KeyUse>,
    in_flight: usize,
    /// Requêtes servies (acceptées par AIODOS) de la dernière minute, toutes clés confondues.
    served: VecDeque<Instant>,
    /// Requêtes relayées dont AIODOS n'a pas encore répondu : comptées comme servies en
    /// attendant (`in_flight` au plus).
    undecided: usize,
    /// Requêtes relayées de la dernière minute, servies ou refusées par AIODOS.
    recent: VecDeque<Instant>,
    relayed: u64,
    accepted: u64,
    refused: u64,
}

#[derive(Debug)]
struct KeyUse {
    in_flight: usize,
    /// Admissions de la dernière heure.
    recent: VecDeque<Instant>,
    /// Dernière requête (début ou fin).
    last: Instant,
}

impl Counters {
    /// Oublie les admissions sorties des fenêtres, et les clés inactives.
    fn tidy(&mut self, now: Instant) {
        for window in [&mut self.served, &mut self.recent] {
            while window.front().is_some_and(|at| now.saturating_duration_since(*at) >= MINUTE) {
                window.pop_front();
            }
        }
        self.keys.retain(|_, key| {
            while key.recent.front().is_some_and(|at| now.saturating_duration_since(*at) >= HOUR) {
                key.recent.pop_front();
            }
            key.in_flight > 0 || now.saturating_duration_since(key.last) < FORGET_KEY_AFTER
        });
    }

    fn allows(&self, key: &EndpointId) -> bool {
        if self.in_flight >= GLOBAL_IN_FLIGHT
            || self.served.len() + self.undecided >= GLOBAL_PER_MINUTE
            || self.recent.len() >= GLOBAL_RELAYED_PER_MINUTE
        {
            return false;
        }
        self.keys
            .get(key)
            .is_none_or(|key| key.in_flight < PER_KEY_IN_FLIGHT && key.recent.len() < PER_KEY_PER_HOUR)
    }
}

impl Limits {
    fn counters(&self) -> std::sync::MutexGuard<'_, Counters> {
        self.0.lock().expect("bornes du réseau public")
    }

    /// Une requête de cette clé serait-elle admise maintenant ? Rien n'est compté : sert à
    /// refuser avant de lire le corps.
    pub fn would_admit(&self, key: &EndpointId, now: Instant) -> bool {
        let mut counters = self.counters();
        counters.tidy(now);
        let allowed = counters.allows(key);
        if !allowed {
            counters.refused += 1;
        }
        allowed
    }

    /// Admet une requête de cette clé, ou `None` (503 `busy`). La place est rendue quand
    /// l'`Admitted` est lâché (fin de la réponse, ou flux abandonné) ; la requête ne compte
    /// parmi les servies que si AIODOS l'accepte (`Admitted::served`).
    pub fn admit(&self, key: EndpointId, now: Instant) -> Option<Admitted> {
        let mut counters = self.counters();
        counters.tidy(now);
        if !counters.allows(&key) {
            counters.refused += 1;
            return None;
        }
        counters.in_flight += 1;
        counters.undecided += 1;
        counters.relayed += 1;
        counters.recent.push_back(now);
        let used = counters
            .keys
            .entry(key)
            .or_insert_with(|| KeyUse { in_flight: 0, recent: VecDeque::new(), last: now });
        used.in_flight += 1;
        used.recent.push_back(now);
        used.last = now;
        Some(Admitted { limits: self.clone(), key, undecided: true })
    }

    /// Pour l'état (`GET /net/v1/public`, `"served"`) : requêtes en cours ; relayées,
    /// acceptées par AIODOS (200) et refusées par le compagnon depuis son démarrage. Jamais
    /// les clés.
    pub fn snapshot(&self) -> Value {
        let counters = self.counters();
        json!({
            "in_flight": counters.in_flight,
            "relayed": counters.relayed,
            "accepted": counters.accepted,
            "refused": counters.refused,
        })
    }
}

/// Une requête admise : sa place (par clé et pour tous) est rendue quand elle est lâchée.
#[derive(Debug)]
pub struct Admitted {
    limits: Limits,
    key: EndpointId,
    /// AIODOS n'a pas encore dit s'il la sert.
    undecided: bool,
}

impl Admitted {
    /// AIODOS sert la requête (réponse 200) : elle compte parmi les `GLOBAL_PER_MINUTE`.
    /// Sans cet appel, elle n'aura compté que parmi les relayées.
    pub fn served(&mut self, now: Instant) {
        if std::mem::take(&mut self.undecided) {
            let mut counters = self.limits.counters();
            counters.undecided = counters.undecided.saturating_sub(1);
            counters.served.push_back(now);
            counters.accepted += 1;
        }
    }
}

impl Drop for Admitted {
    fn drop(&mut self) {
        let mut counters = self.limits.counters();
        if self.undecided {
            counters.undecided = counters.undecided.saturating_sub(1);
        }
        counters.in_flight = counters.in_flight.saturating_sub(1);
        if let Some(used) = counters.keys.get_mut(&self.key) {
            used.in_flight = used.in_flight.saturating_sub(1);
            used.last = Instant::now();
        }
    }
}

// ------------------------------------------------------------ connexions

/// Connexions d'inconnus ouvertes, avec ce qu'elles ont obtenu.
#[derive(Debug, Default)]
struct Conns(Mutex<Table>);

#[derive(Debug, Default)]
struct Table {
    held: HashMap<u64, Held>,
    /// Numéro de la prochaine connexion (jamais réutilisé, contrairement à `stable_id`).
    next: u64,
}

#[derive(Debug)]
struct Held {
    conn: Connection,
    standing: Standing,
}

/// Ce qu'une connexion a obtenu, pour choisir celle qui cède sa place (cf. `victim`).
#[derive(Debug, Clone, Copy)]
struct Standing {
    opened: Instant,
    /// Requêtes de cette connexion qui avancent (cf. `Advancing`).
    advancing: usize,
    /// Dernière réponse acceptée par AIODOS (200) sur cette connexion, sa fin si elle est
    /// finie ; jamais pour une connexion qui n'a eu que des refus, ou rien demandé.
    served: Option<Instant>,
}

/// La connexion qui cède sa place à une nouvelle : jamais une dont une requête avance ;
/// d'abord celles qui n'ont jamais été servies (en-têtes jamais finis, refus seulement, rien
/// demandé), la plus anciennement ouverte en premier ; puis la plus anciennement servie.
/// Une connexion ne garde donc sa place qu'en avançant, et une connexion servie au repos
/// (une conversation entre deux messages) passe après toutes celles qui n'ont rien obtenu.
fn victim<'a>(standings: impl IntoIterator<Item = (u64, &'a Standing)>) -> Option<u64> {
    standings
        .into_iter()
        .filter(|(_, s)| s.advancing == 0)
        .min_by_key(|(_, s)| (s.served.is_some(), s.served.unwrap_or(s.opened)))
        .map(|(id, _)| id)
}

impl Conns {
    /// Une place pour cette connexion. Toutes prises : celle de `victim` est fermée ; si
    /// toutes avancent, `None` (la nouvelle est fermée). Les requêtes qui avancent sont
    /// bornées par `MAX_READING` et `GLOBAL_IN_FLIGHT`, bien en dessous de
    /// `MAX_CONNECTIONS` : une nouvelle connexion trouve toujours une place.
    fn enter(self: &Arc<Self>, conn: &Connection) -> Option<Entered> {
        let mut table = self.0.lock().expect("connexions publiques");
        if table.held.len() >= MAX_CONNECTIONS {
            let id = victim(table.held.iter().map(|(id, h)| (*id, &h.standing)))?;
            if let Some(old) = table.held.remove(&id) {
                old.conn.close(close::BUSY.into(), b"busy");
            }
        }
        let id = table.next;
        table.next += 1;
        let standing = Standing { opened: Instant::now(), advancing: 0, served: None };
        table.held.insert(id, Held { conn: conn.clone(), standing });
        Some(Entered(Place { conns: self.clone(), id }))
    }
}

/// La place d'une connexion, telle que ses flux la voient.
#[derive(Clone)]
struct Place {
    conns: Arc<Conns>,
    id: u64,
}

impl Place {
    fn update(&self, change: impl FnOnce(&mut Standing)) {
        if let Some(held) = self.conns.0.lock().expect("connexions publiques").held.get_mut(&self.id) {
            change(&mut held.standing);
        }
    }

    /// Une requête de cette connexion avance, tant que la valeur rendue est tenue.
    fn advancing(&self) -> Advancing {
        self.update(|s| s.advancing += 1);
        Advancing { place: self.clone(), served: false }
    }
}

/// Une requête qui avance : son corps est lu (une place de lecture, au débit minimal), puis
/// AIODOS l'a admise et répond (une place du public, réponse lue par l'appelant). Sa
/// connexion n'est pas évincée tant qu'elle est tenue (cf. `victim`).
struct Advancing {
    place: Place,
    served: bool,
}

impl Advancing {
    /// AIODOS sert la requête (réponse 200).
    fn served(&mut self) {
        self.served = true;
        self.place.update(|s| s.served = Some(Instant::now()));
    }
}

impl Drop for Advancing {
    fn drop(&mut self) {
        let served = self.served;
        self.place.update(|s| {
            s.advancing = s.advancing.saturating_sub(1);
            if served {
                s.served = Some(Instant::now()); // fin de la réponse servie
            }
        });
    }
}

/// La place d'une connexion, rendue quand elle est lâchée.
struct Entered(Place);

impl Drop for Entered {
    fn drop(&mut self) {
        self.0.conns.0.lock().expect("connexions publiques").held.remove(&self.0.id);
    }
}

// ------------------------------------------------------------ service

/// Le service de `PUBLIC_ALPN` sur le nœud public (cf. public.rs, `Router`).
#[derive(Clone)]
pub struct Serve(Arc<Shared>);

struct Shared {
    forward: SocketAddr,
    response_stall: Duration,
    lends: Arc<AtomicBool>,
    limits: Limits,
    conns: Arc<Conns>,
    reading: Arc<Readers>,
}

impl fmt::Debug for Serve {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Serve").field("forward", &self.0.forward).finish_non_exhaustive()
    }
}

impl Serve {
    /// Relaie vers `forward` (serveur réseau local d'AIODOS) tant que `lends` est vrai, dans
    /// les bornes de `limits`.
    pub fn new(forward: SocketAddr, response_stall: Duration, lends: Arc<AtomicBool>, limits: Limits) -> Self {
        Serve(Arc::new(Shared {
            forward,
            response_stall,
            lends,
            limits,
            conns: Arc::default(),
            reading: Readers::new(MAX_READING),
        }))
    }
}

impl ProtocolHandler for Serve {
    async fn accept(&self, conn: Connection) -> Result<(), AcceptError> {
        serve_connection(self.0.clone(), conn).await;
        Ok(())
    }
}

/// Sert les flux d'une connexion d'inconnu jusqu'à sa fermeture. Les flux en cours sont
/// arrêtés avec elle (`JoinSet` lâché), ou à l'arrêt du nœud public (le `Router` abandonne
/// alors ce futur).
async fn serve_connection(shared: Arc<Shared>, conn: Connection) {
    let caller = conn.remote_id();
    let Some(entered) = shared.conns.enter(&conn) else {
        debug!("Réseau public : trop de connexions d'inconnus, une refusée");
        conn.close(close::BUSY.into(), b"busy");
        return;
    };
    let mut streams = JoinSet::new();
    loop {
        let idle = tokio::time::sleep(CONNECTION_IDLE);
        tokio::select! {
            accepted = conn.accept_bi() => match accepted {
                Ok((mut send, mut recv)) => {
                    // Les flux finis mais pas encore relevés ne comptent plus (QUIC a déjà
                    // rendu leur place à l'appelant)
                    while streams.try_join_next().is_some() {}
                    if streams.len() >= MAX_STREAMS_PER_CONNECTION {
                        let _ = send.reset(STREAM_REFUSED.into());
                        let _ = recv.stop(STREAM_REFUSED.into());
                        continue;
                    }
                    let place = entered.0.clone();
                    streams.spawn(serve_stream(shared.clone(), caller, place, QuicIo::bare(send, recv)));
                }
                Err(_) => break,
            },
            Some(_) = streams.join_next(), if !streams.is_empty() => {}
            _ = idle, if streams.is_empty() => {
                conn.close(close::BUSY.into(), b"idle");
                break;
            }
        }
    }
}

/// Un flux = un échange HTTP/1.1, relayé vers AIODOS. Si l'appelant ne lit plus la réponse
/// (rien depuis `response_stall`), le flux est coupé et sa place rendue.
async fn serve_stream(shared: Arc<Shared>, caller: EndpointId, place: Place, io: QuicIo) {
    let progress = Arc::new(Progress::default());
    let stall = shared.response_stall;
    let service = {
        let progress = progress.clone();
        service_fn(move |req| {
            let (shared, progress, place) = (shared.clone(), progress.clone(), place.clone());
            async move { Ok::<_, Infallible>(forward(&shared, caller, &place, req, progress).await) }
        })
    };
    let mut builder = http1::Builder::new();
    builder
        .keep_alive(false)
        .timer(TokioTimer::new())
        .header_read_timeout(HEADER_TIMEOUT)
        .max_buf_size(MAX_HEAD)
        .max_headers(MAX_HEADERS);
    tokio::select! {
        served = builder.serve_connection(TokioIo::new(io), service) => {
            if let Err(err) = served {
                debug!("Réseau public : échange interrompu ({err})");
            }
        }
        _ = progress.stalled(stall) => {
            debug!("Réseau public : réponse non lue depuis {} s, flux coupé", stall.as_secs());
        }
    }
}

/// Relaie une requête d'inconnu vers le serveur réseau local d'AIODOS, ou la refuse. Elle
/// avance (cf. `Advancing`, pour la place de sa connexion) de sa place de lecture à la fin
/// de la réponse d'AIODOS ; un refus n'est pas une avance.
async fn forward(
    shared: &Shared,
    caller: EndpointId,
    place: &Place,
    req: Request<Incoming>,
    progress: Arc<Progress>,
) -> Response<Body> {
    if req.method() != Method::POST || !allowed_target(req.uri().path(), req.uri().query()) {
        return refusal(StatusCode::NOT_FOUND, "not_found", "Seules les requêtes POST /public/v1/… passent.");
    }
    if !shared.lends.load(Ordering::SeqCst) {
        return refusal(
            StatusCode::SERVICE_UNAVAILABLE,
            "not_lending",
            "Cette machine ne prête pas aux inconnus en ce moment.",
        );
    }
    // Déjà joué : refus aussitôt, sans lire le corps
    if !shared.limits.would_admit(&caller, Instant::now()) {
        return busy();
    }
    if relay_body::announced_too_large(req.headers(), MAX_BODY) {
        return too_large();
    }
    // Une place de lecture, prise au besoin au corps le plus lent (cf. public_reading.rs)
    let Some(reading) = shared.reading.enter(Instant::now()) else {
        return busy();
    };
    let mut advancing = place.advancing();
    // Le corps entier d'abord (borné en taille, en silence, en débit et en temps) : un envoi
    // au goutte-à-goutte ne tient jamais l'une des places du public, et l'une des places de
    // lecture quelques secondes au plus.
    let (parts, body) = req.into_parts();
    let body = relay_body::bounded(body, MAX_BODY, BODY_IDLE, Some(BODY_PACE));
    let body = match tokio::time::timeout(BODY_TIMEOUT, reading.collect(body)).await {
        Ok(Ok(body)) => body,
        Ok(Err(Unread::Preempted)) => return busy(),
        Ok(Err(err)) if relay_body::is_too_large(&err) => return too_large(),
        Ok(Err(err)) if relay_body::is_too_slow(&err) => return too_slow(),
        Ok(Err(_)) => return refusal(StatusCode::BAD_REQUEST, "bad_request", "Requête interrompue."),
        Err(_) => return too_slow(),
    };
    drop(reading);
    let Some(mut admitted) = shared.limits.admit(caller, Instant::now()) else {
        return busy();
    };
    let target = parts.uri.path_and_query().map_or(PATH_PREFIX, |pq| pq.as_str()).to_owned();
    let mut local = Request::new(reply::boxed(Full::new(body.clone())));
    *local.method_mut() = Method::POST;
    *local.uri_mut() = match target.parse() {
        Ok(uri) => uri,
        Err(_) => return refusal(StatusCode::NOT_FOUND, "not_found", "Chemin illisible."),
    };
    *local.headers_mut() = relayed_headers(&parts.headers, caller, shared.forward, body.len());
    match inbound::send_local(shared.forward, local).await {
        Ok(response) => {
            // Seule une requête qu'AIODOS sert compte parmi les servies ; un refus (mal
            // formée, machine occupée…) ne compte que parmi les relayées
            if response.status() == StatusCode::OK {
                admitted.served(Instant::now());
                advancing.served();
            }
            let (mut head, body) = response.into_parts();
            head.headers = returned_headers(&head.headers);
            let held = (admitted, advancing);
            Response::from_parts(head, relay_body::guarded(held, progress, reply::boxed(body)))
        }
        Err(err) => {
            debug!("Réseau public : serveur réseau local injoignable ({}) : {err}", shared.forward);
            refusal(StatusCode::BAD_GATEWAY, "unavailable", "AIODOS ne répond pas sur la machine jointe.")
        }
    }
}

/// Chemin (et requête `?…`) qu'un inconnu peut demander : sous `PATH_PREFIX`, en lettres,
/// chiffres, `-`, `_`, `.` et `/`, sans segment vide, `.` ni `..` (rien qui mène ailleurs
/// une fois décodé ou normalisé par le serveur local), et court.
pub fn allowed_target(path: &str, query: Option<&str>) -> bool {
    let Some(rest) = path.strip_prefix(PATH_PREFIX) else {
        return false;
    };
    let safe_path = |c: char| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/');
    let safe_query = |c: char| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '=' | '&');
    path.len() + query.map_or(0, str::len) <= MAX_TARGET
        && rest.chars().all(safe_path)
        && rest.split('/').all(|segment| !segment.is_empty() && segment != "." && segment != "..")
        && query.is_none_or(|query| query.chars().all(safe_query))
}

/// En-têtes de la requête relayée à AIODOS : `PASSED_HEADERS` de l'appelant, puis ceux que
/// le compagnon pose (`Host`, `Content-Length`, `X-Aiodos-Public` : la vraie clé de
/// l'appelant, une seule fois). Rien d'autre de l'appelant.
pub fn relayed_headers(from: &HeaderMap, caller: EndpointId, forward: SocketAddr, length: usize) -> HeaderMap {
    let mut headers = only(from, PASSED_HEADERS);
    headers.insert(HOST, HeaderValue::from_str(&forward.to_string()).expect("adresse locale"));
    headers.insert(CONTENT_LENGTH, HeaderValue::from(length));
    headers.insert(PUBLIC_HEADER, HeaderValue::from_str(&caller.to_string()).expect("clé en hexadécimal"));
    headers
}

/// En-têtes d'une réponse renvoyée à l'autre bout (`RETURNED_HEADERS`).
pub fn returned_headers(from: &HeaderMap) -> HeaderMap {
    only(from, RETURNED_HEADERS)
}

fn only(from: &HeaderMap, names: &[&'static str]) -> HeaderMap {
    let mut headers = HeaderMap::new();
    for name in names {
        for value in from.get_all(*name) {
            headers.append(HeaderName::from_static(name), value.clone());
        }
    }
    headers
}

/// Refus du réseau public : `{"error": "<code>", "detail": "<explication>"}`, flux fermé.
/// Le code est ce que le demandeur lit (`busy`, `not_lending`…), l'explication pour le
/// journal.
pub fn refusal(status: StatusCode, code: &str, detail: impl Into<String>) -> Response<Body> {
    let mut response = reply::json(status, json!({ "error": code, "detail": detail.into() }));
    response.headers_mut().insert(CONNECTION, HeaderValue::from_static("close"));
    response
}

/// Une borne est atteinte : le demandeur passe à une autre machine.
fn busy() -> Response<Body> {
    refusal(
        StatusCode::SERVICE_UNAVAILABLE,
        "busy",
        "Machine occupée pour le réseau public : demande à une autre.",
    )
}

/// Corps trop lent : muet `BODY_IDLE`, sous `BODY_PACE`, ou pas fini en `BODY_TIMEOUT`.
fn too_slow() -> Response<Body> {
    refusal(
        StatusCode::REQUEST_TIMEOUT,
        "too_slow",
        format!(
            "Requête trop lente (au moins {} Kio/s après {} s, entière en {} s).",
            BODY_PACE.min_rate >> 10,
            BODY_PACE.grace.as_secs(),
            BODY_TIMEOUT.as_secs()
        ),
    )
}

fn too_large() -> Response<Body> {
    refusal(
        StatusCode::PAYLOAD_TOO_LARGE,
        "too_large",
        format!("Requête trop grosse (au plus {} Kio).", MAX_BODY >> 10),
    )
}

#[cfg(test)]
mod tests {
    use iroh::SecretKey;

    use super::*;

    fn key() -> EndpointId {
        SecretKey::generate().public()
    }

    #[test]
    fn only_plain_public_paths_pass() {
        for (path, query) in [
            ("/public/v1/chat", None),
            ("/public/v1/chat/stream", None),
            ("/public/v1/chat", Some("stream=1&model=qwen3.5")),
            ("/public/v1/v2.chat_x-y", None),
        ] {
            assert!(allowed_target(path, query), "{path} {query:?}");
        }
        for (path, query) in [
            ("/p2p/v1/stream/chat", None),
            ("/public/v1/", None),
            ("/public/v1", None),
            ("/public/v2/chat", None),
            ("/public/v1/../p2p/v1/stream/chat", None),
            ("/public/v1/./chat", None),
            ("/public/v1//chat", None),
            ("/public/v1/chat/", None),
            ("/public/v1/%2e%2e/p2p", None),
            ("/public/v1/chat\\..\\x", None),
            ("/public/v1/chat", Some("x=%2F")),
            ("/public/v1/chat", Some("x=<script>")),
        ] {
            assert!(!allowed_target(path, query), "{path} {query:?}");
        }
        let long = format!("/public/v1/{}", "a".repeat(MAX_TARGET));
        assert!(!allowed_target(&long, None));
    }

    #[test]
    fn a_stranger_never_passes_for_a_paired_machine_nor_for_another_key() {
        let caller = key();
        let mut from = HeaderMap::new();
        from.insert("content-type", HeaderValue::from_static("application/json"));
        from.insert("accept", HeaderValue::from_static("application/x-ndjson"));
        from.insert("x-aiodos-peer", HeaderValue::from_str(&key().to_string()).unwrap());
        from.append("x-aiodos-public", HeaderValue::from_str(&key().to_string()).unwrap());
        from.append("x-aiodos-public", HeaderValue::from_static("deuxieme"));
        from.insert("x-aiodos-net-token", HeaderValue::from_static("jeton"));
        from.insert("x-aiodos-conversation", HeaderValue::from_static("conversation-0123456789"));
        from.insert("x-forwarded-for", HeaderValue::from_static("203.0.113.9"));
        from.insert("user-agent", HeaderValue::from_static("AIODOS 2.1.0 (le PC de Paul)"));
        from.insert("connection", HeaderValue::from_static("keep-alive"));
        from.insert("host", HeaderValue::from_static("ailleurs.example"));
        from.insert("content-length", HeaderValue::from_static("999999"));
        let forward: SocketAddr = "127.0.0.1:47701".parse().unwrap();
        let headers = relayed_headers(&from, caller, forward, 12);
        let mut names: Vec<&str> = headers.keys().map(HeaderName::as_str).collect();
        names.sort_unstable();
        assert_eq!(names, ["accept", "content-length", "content-type", "host", "x-aiodos-public"]);
        let publics: Vec<_> = headers.get_all(PUBLIC_HEADER).iter().collect();
        assert_eq!(publics, [caller.to_string().as_str()], "la vraie clé, une seule fois");
        assert_eq!(headers[CONTENT_LENGTH], "12");
        assert_eq!(headers[HOST], "127.0.0.1:47701");
        assert_eq!(caller.to_string().len(), 64, "en hexadécimal");
    }

    #[test]
    fn a_response_returns_only_its_content_type() {
        let mut from = HeaderMap::new();
        from.insert("content-type", HeaderValue::from_static("application/x-ndjson"));
        from.insert("retry-after", HeaderValue::from_static("5"));
        from.insert("server", HeaderValue::from_static("uvicorn"));
        from.insert("x-aiodos-peer", HeaderValue::from_static("x"));
        from.insert("transfer-encoding", HeaderValue::from_static("chunked"));
        let headers = returned_headers(&from);
        assert_eq!(headers.len(), 2);
        assert_eq!(headers["content-type"], "application/x-ndjson");
    }

    #[test]
    fn one_request_at_a_time_per_key_and_two_for_everyone() {
        let limits = Limits::default();
        let now = Instant::now();
        let (a, b, c) = (key(), key(), key());
        let first = limits.admit(a, now).expect("première");
        assert!(!limits.would_admit(&a, now), "une à la fois par clé");
        assert!(limits.admit(a, now).is_none());
        let second = limits.admit(b, now).expect("une autre clé");
        assert!(limits.admit(c, now).is_none(), "deux à la fois pour tout le public");
        drop(first);
        let third = limits.admit(c, now).expect("une place rendue");
        assert!(limits.admit(a, now).is_none(), "toujours deux à la fois");
        drop((second, third));
        assert!(limits.admit(a, now).is_some(), "toutes les places rendues");
        let served = limits.snapshot();
        assert_eq!(served["in_flight"], 0);
        assert_eq!(served["relayed"], 4);
        assert!(served["refused"].as_u64().unwrap() >= 4);
    }

    #[test]
    fn thirty_requests_an_hour_per_key_and_the_key_is_forgotten_when_idle() {
        let limits = Limits::default();
        let start = Instant::now();
        let a = key();
        for i in 0..PER_KEY_PER_HOUR {
            let at = start + Duration::from_secs(i as u64);
            drop(limits.admit(a, at).unwrap_or_else(|| panic!("requête {i}")));
        }
        let later = start + Duration::from_secs(60);
        assert!(limits.admit(a, later).is_none(), "31e dans l'heure");
        assert!(limits.admit(key(), later).is_some(), "les autres clés passent");
        // Inactive assez longtemps, la clé est oubliée, avec ses compteurs
        let idle = Instant::now() + FORGET_KEY_AFTER + Duration::from_secs(1);
        assert!(limits.admit(a, idle).is_some());
        assert_eq!(limits.counters().keys.len(), 1, "seule la clé revenue est gardée");
    }

    #[test]
    fn a_hundred_and_twenty_requests_served_a_minute_for_everyone() {
        let limits = Limits::default();
        let start = Instant::now();
        for i in 0..GLOBAL_PER_MINUTE {
            let mut admitted = limits.admit(key(), start).unwrap_or_else(|| panic!("requête {i}"));
            admitted.served(start);
        }
        assert!(limits.admit(key(), start + Duration::from_secs(30)).is_none(), "121e dans la minute");
        assert!(limits.admit(key(), start + Duration::from_secs(61)).is_some(), "la minute est passée");
        assert_eq!(limits.snapshot()["accepted"], GLOBAL_PER_MINUTE);
    }

    #[test]
    fn quick_refusals_do_not_use_up_the_requests_served_but_the_total_stays_bounded() {
        // Relecture du 28/10 : 120 requêtes qu'AIODOS refuse vite (mal formées, machine
        // occupée), avec des clés jetables, privaient de toute conversation pendant une minute
        let limits = Limits::default();
        let start = Instant::now();
        for i in 0..GLOBAL_PER_MINUTE {
            drop(limits.admit(key(), start).unwrap_or_else(|| panic!("refus {i}")));
        }
        let mut served = limits.admit(key(), start).expect("une conversation servie passe encore");
        served.served(start);
        drop(served);
        // Ni refusée, ni servie encore : comptée parmi les servies tant qu'AIODOS n'a rien dit
        let first = limits.admit(key(), start).expect("en attente de la réponse");
        let second = limits.admit(key(), start).expect("deux à la fois");
        assert_eq!(limits.counters().undecided, 2);
        drop((first, second));
        assert_eq!(limits.counters().undecided, 0, "sans 200, jamais servies");
        // Le total reste borné : refus compris, `GLOBAL_RELAYED_PER_MINUTE` par minute
        while limits.counters().recent.len() < GLOBAL_RELAYED_PER_MINUTE {
            drop(limits.admit(key(), start).expect("sous la borne du total"));
        }
        assert!(limits.admit(key(), start + Duration::from_secs(30)).is_none(), "total atteint");
        assert!(limits.admit(key(), start + Duration::from_secs(61)).is_some(), "la minute est passée");
        assert_eq!(limits.snapshot()["accepted"], 1);
    }

    #[test]
    fn a_connection_keeps_its_place_only_by_moving_forward() {
        let start = Instant::now();
        let at = |s: u64| start + Duration::from_secs(s);
        let standing = |opened: u64, advancing: usize, served: Option<u64>| Standing {
            opened: at(opened),
            advancing,
            served: served.map(at),
        };
        // 1 : servie il y a longtemps, au repos ; 2 : servie récemment, au repos ; 3 : jamais
        // servie, ouverte la première (en-têtes jamais finis) ; 4 : jamais servie, plus
        // récente ; 5 : la plus ancienne, mais une requête avance (corps lu, réponse)
        let table = [
            (1, standing(10, 0, Some(20))),
            (2, standing(11, 0, Some(90))),
            (3, standing(5, 0, None)),
            (4, standing(80, 0, None)),
            (5, standing(0, 1, None)),
        ];
        let pick = |ids: &[u64]| victim(table.iter().filter(|(id, _)| ids.contains(id)).map(|(id, s)| (*id, s)));
        assert_eq!(pick(&[1, 2, 3, 4, 5]), Some(3), "d'abord la plus ancienne jamais servie");
        assert_eq!(pick(&[1, 2, 4, 5]), Some(4), "jamais servie, même récente, avant une servie");
        assert_eq!(pick(&[1, 2, 5]), Some(1), "puis la plus anciennement servie");
        assert_eq!(pick(&[5]), None, "jamais une connexion qui avance");
    }
}
