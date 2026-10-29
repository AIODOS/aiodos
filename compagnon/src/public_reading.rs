//! Places de lecture du réseau public : les corps de requêtes d'inconnus lus en même temps
//! (`public_serve::MAX_READING`), avant tout relais à AIODOS (cf. public_serve.rs).
//!
//! Chaque corps est déjà borné en silence, en débit et en temps (`public_serve::BODY_IDLE`,
//! `BODY_PACE`, `BODY_TIMEOUT`). Reste qu'avec des clés jetables, un inconnu qui ouvre de
//! nouveaux flux sans rien envoyer tiendrait toutes les places pendant la grâce du débit
//! minimal, encore et encore. Une place ne se garde donc qu'en avançant : quand toutes sont
//! prises, une nouvelle requête prend celle du corps le plus lent (en moyenne depuis son
//! début), s'il est lu depuis `PREEMPT_AFTER` au moins et sous `KEEP_RATE`. Le corps évincé
//! reçoit 503 `busy`, comme s'il n'avait pas eu de place. Une requête d'AIODOS (public_ask.rs)
//! arrive d'un coup et n'est presque jamais évincée ; un corps qui file à `KEEP_RATE` ou plus
//! ne l'est jamais, et tenir les huit places à ce débit coûte à l'inconnu 512 Kio/s.
//!
//! Relecture du 28/10 (bilan, « rythme non borné ») : un flot de nouveaux flux pouvait ainsi
//! couper l'envoi lent d'une conversation déjà en cours. Le corps d'une connexion déjà
//! servie (public_serve.rs) est donc **gardé** : seule une requête d'une connexion servie
//! peut lui prendre sa place, jamais celle d'un nouveau venu. Les corps gardés tiennent la
//! moitié des places au plus (`PROTECTED_SHARE`) : au-delà, un corps de connexion servie est
//! lu comme un autre, et les nouveaux venus trouvent toujours des places à prendre.

use std::{
    error::Error,
    fmt,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use bytes::{Bytes, BytesMut};
use http_body_util::BodyExt;
use tokio_util::sync::CancellationToken;

use crate::reply::{Body, BoxError};

/// Un corps lu depuis moins longtemps garde sa place quoi qu'il arrive : le temps d'un
/// aller-retour par un relais lointain, pour une requête envoyée d'un coup.
pub const PREEMPT_AFTER: Duration = Duration::from_secs(1);
/// Un corps qui arrive au moins à ce débit moyen garde sa place (quatre fois le débit
/// minimal `public_serve::BODY_PACE`).
pub const KEEP_RATE: u64 = 64 * 1024;
/// Part des places que les corps gardés (connexions déjà servies) tiennent au plus.
pub const PROTECTED_SHARE: usize = 2;

/// Les places de lecture, partagées par toutes les connexions d'inconnus.
#[derive(Debug)]
pub struct Readers {
    places: usize,
    table: Mutex<Table>,
}

#[derive(Debug, Default)]
struct Table {
    entries: Vec<Entry>,
    next: u64,
}

#[derive(Debug)]
struct Entry {
    id: u64,
    /// Corps d'une connexion déjà servie : seule une requête d'une connexion servie peut
    /// prendre sa place.
    protected: bool,
    started: Instant,
    received: Arc<AtomicU64>,
    cut: CancellationToken,
}

/// Ce qu'on sait d'un corps en lecture, pour choisir celui qui cède sa place (cf. `victim`).
#[derive(Debug, Clone, Copy)]
pub struct Pace {
    pub started: Instant,
    pub received: u64,
}

/// Le corps qui cède sa place à une nouvelle requête : lu depuis `PREEMPT_AFTER` au moins,
/// sous `KEEP_RATE` en moyenne, le plus lent d'abord (à débit égal, le plus ancien). Aucun :
/// la nouvelle requête est refusée.
pub fn victim(paces: impl IntoIterator<Item = Pace>, now: Instant) -> Option<usize> {
    paces
        .into_iter()
        .enumerate()
        .filter_map(|(index, pace)| {
            let elapsed = now.saturating_duration_since(pace.started);
            let rate = pace.received as f64 / elapsed.as_secs_f64().max(1e-3);
            (elapsed >= PREEMPT_AFTER && rate < KEEP_RATE as f64).then_some((index, rate, pace.started))
        })
        .min_by(|a, b| a.1.total_cmp(&b.1).then(a.2.cmp(&b.2)))
        .map(|(index, _, _)| index)
}

impl Readers {
    pub fn new(places: usize) -> Arc<Self> {
        Arc::new(Readers { places, table: Mutex::default() })
    }

    /// Une place de lecture, prise au besoin au corps le plus lent (cf. `victim`) ; `None`
    /// si toutes avancent (503 `busy`). `served` : la requête vient d'une connexion déjà
    /// servie ; son corps est alors gardé (s'il reste de la part gardée), et elle peut prendre
    /// la place d'un corps gardé. Sinon, elle ne prend que celle d'un corps qui ne l'est pas.
    pub fn enter(self: &Arc<Self>, now: Instant, served: bool) -> Option<Reading> {
        let mut table = self.table.lock().expect("places de lecture");
        if table.entries.len() >= self.places {
            // Les corps qu'elle peut évincer, et leur rang dans la table
            let (ranks, paces): (Vec<usize>, Vec<Pace>) = table
                .entries
                .iter()
                .enumerate()
                .filter(|(_, e)| served || !e.protected)
                .map(|(rank, e)| (rank, Pace { started: e.started, received: e.received.load(Ordering::Relaxed) }))
                .unzip();
            let index = ranks[victim(paces, now)?];
            table.entries.swap_remove(index).cut.cancel();
        }
        let protected = served && table.entries.iter().filter(|e| e.protected).count() < self.places / PROTECTED_SHARE;
        let id = table.next;
        table.next += 1;
        let (received, cut) = (Arc::new(AtomicU64::new(0)), CancellationToken::new());
        table.entries.push(Entry { id, protected, started: now, received: received.clone(), cut: cut.clone() });
        Some(Reading { readers: self.clone(), id, received, cut })
    }

    /// Places prises.
    pub fn taken(&self) -> usize {
        self.table.lock().expect("places de lecture").entries.len()
    }
}

/// Une place de lecture, rendue quand elle est lâchée.
#[derive(Debug)]
pub struct Reading {
    readers: Arc<Readers>,
    id: u64,
    received: Arc<AtomicU64>,
    cut: CancellationToken,
}

/// Pourquoi un corps n'a pas été lu en entier.
#[derive(Debug)]
pub enum Unread {
    /// Sa place a été prise par une autre requête (cf. `victim`).
    Preempted,
    /// Le corps lui-même : trop gros, trop lent, interrompu.
    Body(BoxError),
}

impl fmt::Display for Unread {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Unread::Preempted => write!(f, "place de lecture prise par une autre requête"),
            Unread::Body(err) => write!(f, "{err}"),
        }
    }
}

impl Error for Unread {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Unread::Preempted => None,
            Unread::Body(err) => Some(err.as_ref()),
        }
    }
}

impl Reading {
    /// Le corps entier, en notant ce qui arrive pour la place de lecture.
    pub async fn collect(&self, mut body: Body) -> Result<Bytes, Unread> {
        let mut collected = BytesMut::new();
        loop {
            let frame = tokio::select! {
                frame = body.frame() => frame,
                () = self.cut.cancelled() => return Err(Unread::Preempted),
            };
            match frame {
                None => return Ok(collected.freeze()),
                Some(Err(err)) => return Err(Unread::Body(err)),
                Some(Ok(frame)) => {
                    if let Ok(data) = frame.into_data() {
                        self.received.fetch_add(data.len() as u64, Ordering::Relaxed);
                        collected.extend_from_slice(&data);
                    }
                }
            }
        }
    }
}

impl Drop for Reading {
    fn drop(&mut self) {
        let mut table = self.readers.table.lock().expect("places de lecture");
        if let Some(index) = table.entries.iter().position(|e| e.id == self.id) {
            table.entries.swap_remove(index);
        }
    }
}

#[cfg(test)]
mod tests {
    use http_body_util::Full;

    use super::*;
    use crate::reply;

    #[test]
    fn the_slowest_body_gives_way_and_only_after_a_while() {
        let start = Instant::now();
        let pace = |received: u64| Pace { started: start, received };
        let young = start + PREEMPT_AFTER / 2;
        assert_eq!(victim([pace(0), pace(0)], young), None, "trop récents");
        let now = start + Duration::from_secs(2);
        // 2 s : 1 Kio (0,5 Kio/s), rien, 100 Kio (50 Kio/s), 200 Kio (100 Kio/s : garde sa place)
        let paces = [pace(1 << 10), pace(0), pace(100 << 10), pace(200 << 10)];
        assert_eq!(victim(paces, now), Some(1), "le plus lent d'abord");
        assert_eq!(victim([pace(1 << 10), pace(100 << 10)], now), Some(0));
        assert_eq!(victim([pace(200 << 10)], now), None, "au débit qui garde sa place");
        let older = Pace { started: start - Duration::from_secs(1), received: 0 };
        assert_eq!(victim([pace(0), older], now), Some(1), "à débit égal, le plus ancien");
    }

    #[tokio::test]
    async fn a_new_request_takes_the_place_of_a_body_that_does_not_move_forward() {
        let readers = Readers::new(2);
        let start = Instant::now();
        let first = readers.enter(start, false).expect("place libre");
        let second = readers.enter(start, false).expect("place libre");
        assert!(readers.enter(start, false).is_none(), "toutes prises, et trop récentes");
        let later = start + PREEMPT_AFTER;
        let third = readers.enter(later, false).expect("prise à un corps qui n'avance pas");
        assert_eq!(readers.taken(), 2);
        // Le corps évincé l'apprend en route (ici : un corps qui n'arrive jamais)
        let evicted = if first.cut.is_cancelled() { &first } else { &second };
        assert!(matches!(evicted.collect(reply::boxed(Never)).await, Err(Unread::Preempted)));
        // Un corps entier passe, et la place est rendue avec la lecture
        let body = reply::boxed(Full::new(Bytes::from_static(b"bonjour")));
        assert_eq!(&third.collect(body).await.unwrap()[..], b"bonjour");
        drop((first, second, third));
        assert_eq!(readers.taken(), 0);
    }

    #[test]
    fn the_body_of_a_served_connection_keeps_its_place_against_newcomers() {
        // Relecture du 28/10 : un flot de nouveaux flux coupait l'envoi lent d'une
        // conversation en cours (le plus lent des corps cédait sa place au nouveau venu)
        let readers = Readers::new(4);
        let start = Instant::now();
        let at = |s: u64| start + Duration::from_secs(s);
        // Les nouveaux venus du flot filent (jamais évincés entre eux) : seuls comptent ici
        // les corps lents
        let flood = |now: Instant| {
            let reading = readers.enter(now, false);
            if let Some(reading) = &reading {
                reading.received.store(1 << 30, Ordering::Relaxed);
            }
            reading
        };
        // Deux corps de connexions servies (la part gardée : la moitié des places), un
        // troisième (lu comme un autre, la part est prise) et celui d'un nouveau venu ; lents
        let served: Vec<Reading> = (0..3).map(|_| readers.enter(start, true).unwrap()).collect();
        let stranger = readers.enter(start, false).unwrap();
        served[0].received.store(10 << 10, Ordering::Relaxed); // 5 Kio/s à 2 s
        served[1].received.store(10 << 10, Ordering::Relaxed);
        stranger.received.store(60 << 10, Ordering::Relaxed); // 30 Kio/s à 2 s
        // Le flot prend la place du troisième (non gardé), puis de l'inconnu ; jamais celle
        // des deux gardés, pourtant plus lents que l'inconnu
        let first = flood(at(2)).expect("place prise au corps non gardé");
        assert!(served[2].cut.is_cancelled());
        let _second = flood(at(3)).expect("puis à l'inconnu");
        assert!(stranger.cut.is_cancelled());
        assert!(!served[0].cut.is_cancelled() && !served[1].cut.is_cancelled());
        assert!(flood(at(4)).is_none(), "plus rien à prendre : les gardés restent");
        drop(first);
        let _third = flood(at(4)).expect("une place rendue est reprise");
        // Une requête d'une connexion servie, elle, peut prendre la place d'un corps gardé
        assert!(readers.enter(at(5), true).is_some());
        assert!(served[0].cut.is_cancelled() || served[1].cut.is_cancelled());
    }

    /// Un corps dont rien n'arrive jamais.
    struct Never;

    impl hyper::body::Body for Never {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            self: std::pin::Pin<&mut Self>,
            _cx: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>> {
            std::task::Poll::Pending
        }
    }
}
