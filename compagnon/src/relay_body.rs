//! Corps des requêtes reçues d'une autre machine, bornés avant d'être relayés à AIODOS :
//! une taille maximale, un silence maximal entre deux morceaux, et un débit minimal en
//! moyenne (`PACE`). Sans quoi une machine autorisée (un ami appairé) enverrait des
//! gigaoctets, ou tiendrait des places au goutte-à-goutte, au nœud AIODOS qui sert aussi
//! l'interface. Un débit minimal plutôt qu'une durée totale fixe : une grosse requête
//! légitime sur un lien lent (téléphone mal couvert) a le temps qu'il lui faut.
//!
//! Dans l'autre sens, une réponse que la machine ne lit jamais tiendrait sa place aussi :
//! le flux est coupé quand elle n'a rien lu depuis `RESPONSE_STALL` (cf. `Progress`).
//!
//! Les fichiers (morceaux de travaux, images, mises à jour) voyagent dans les réponses ;
//! les requêtes restent petites (`MAX_BODY`), sauf un résultat de calcul
//! (`/p2p/v1/work/result`), qui peut porter une vidéo : `MAX_RESULT_BODY`. AIODOS borne
//! lui-même chaque requête selon l'action et la machine qui l'envoie
//! (aiodos/network/body_limit.py) ; les bornes d'ici ne doivent jamais être plus basses
//! que les siennes.

use std::{
    error::Error,
    fmt,
    future::Future,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};

use bytes::Bytes;
use http_body_util::{LengthLimitError, Limited};
use hyper::{
    HeaderMap,
    body::{Body as HttpBody, Frame, SizeHint},
    header::CONTENT_LENGTH,
};
use tokio::{
    sync::OwnedSemaphorePermit,
    time::{Instant, Sleep},
};

use crate::reply::{self, Body, BoxError};

/// Taille maximale d'un corps de requête relayé (enveloppe scellée comprise).
pub const MAX_BODY: usize = 64 << 20;
/// Résultat de calcul : une vidéo de 512 Mio, deux fois en base64 (dans la sortie, puis
/// dans l'enveloppe scellée) ; la borne d'AIODOS (`HUGE_BODY`).
pub const MAX_RESULT_BODY: usize = 1 << 30;
/// Chemin des résultats de calcul.
pub const RESULT_PATH: &str = "/p2p/v1/work/result";
/// Silence maximal pendant la réception d'un corps.
pub const BODY_IDLE: Duration = Duration::from_secs(60);
/// Débit minimal d'un corps reçu, en moyenne depuis son début, après un délai de grâce :
/// un goutte-à-goutte sous `BODY_IDLE` ne tient pas une place indéfiniment, et une requête
/// de 16 Mio passe encore à 16 Kio/s (un lien mobile très lent).
pub const PACE: Pace = Pace { grace: Duration::from_secs(120), min_rate: 16 * 1024 };
/// Réponse que l'autre machine ne lit plus : flux coupé au-delà.
pub const RESPONSE_STALL: Duration = Duration::from_secs(60);

/// Débit minimal d'un corps : coupé dès que le temps écoulé dépasse `grace`, plus le temps
/// qu'il faut à `min_rate` octets par seconde pour ce qui est déjà arrivé.
#[derive(Debug, Clone, Copy)]
pub struct Pace {
    pub grace: Duration,
    pub min_rate: u64,
}

impl Pace {
    /// Échéance, depuis le début du corps, une fois `received` octets arrivés.
    fn allowed(&self, received: u64) -> Duration {
        self.grace + Duration::from_secs_f64(received as f64 / self.min_rate.max(1) as f64)
    }
}

/// Borne d'une requête vers `path` : `general`, sauf pour un résultat de calcul.
pub fn limit_for(path: &str, general: usize) -> usize {
    if path == RESULT_PATH { general.max(MAX_RESULT_BODY) } else { general }
}

/// Le corps reçu, coupé au-delà de `max` octets, de `idle` sans rien recevoir, ou sous le
/// débit minimal `pace` (si donné).
pub fn bounded<B>(body: B, max: usize, idle: Duration, pace: Option<Pace>) -> Body
where
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
{
    let started = Instant::now();
    reply::boxed(Watched {
        inner: reply::boxed(Limited::new(body, max)),
        idle,
        deadline: Box::pin(tokio::time::sleep(idle)),
        pace: pace.map(|pace| Paced {
            pace,
            started,
            received: 0,
            deadline: Box::pin(tokio::time::sleep_until(started + pace.allowed(0))),
        }),
    })
}

/// La réponse relayée, qui rend la place de la machine (cf. pool::relay_slot) quand elle
/// est entièrement renvoyée ou que le flux est abandonné, et note son avancement
/// (`progress`, cf. inbound.rs) : une machine ne garde pas une place en ne lisant jamais
/// la réponse.
pub fn guarded(permit: OwnedSemaphorePermit, progress: Arc<Progress>, body: Body) -> Body {
    reply::boxed(Guarded { inner: body, progress, _permit: permit })
}

/// Avancement de la réponse d'un flux : depuis quand un morceau attend que l'autre machine
/// le lise (que hyper demande le suivant). Rien quand c'est AIODOS qui n'a pas encore
/// répondu (une conversation qui réfléchit) : seul un autre bout qui ne lit pas est coupé.
#[derive(Debug, Default)]
pub struct Progress {
    handed: Mutex<Option<Instant>>,
}

impl Progress {
    fn asked(&self) {
        *self.handed.lock().expect("avancement") = None;
    }

    fn handed(&self) {
        *self.handed.lock().expect("avancement") = Some(Instant::now());
    }

    /// Revient quand un morceau attend d'être lu depuis plus de `stall`.
    pub async fn stalled(&self, stall: Duration) {
        let every = (stall / 4).clamp(Duration::from_millis(50), Duration::from_secs(1));
        loop {
            tokio::time::sleep(every).await;
            let handed = *self.handed.lock().expect("avancement");
            if handed.is_some_and(|at| at.elapsed() >= stall) {
                return;
            }
        }
    }
}

/// Longueur annoncée trop grande : refusée avant tout envoi.
pub fn announced_too_large(headers: &HeaderMap, max: usize) -> bool {
    headers
        .get(CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<u64>().ok())
        .is_some_and(|len| len > max as u64)
}

/// L'erreur (ou l'une de ses causes) vient-elle d'un corps trop long ?
pub fn is_too_large(err: &(dyn Error + 'static)) -> bool {
    caused_by(err, |e| e.is::<LengthLimitError>())
}

/// L'erreur (ou l'une de ses causes) vient-elle d'un corps trop lent (muet, ou sous le
/// débit minimal) ?
pub fn is_too_slow(err: &(dyn Error + 'static)) -> bool {
    caused_by(err, |e| e.is::<BodyIdle>() || e.is::<BodyTooSlow>())
}

fn caused_by(err: &(dyn Error + 'static), cause: impl Fn(&(dyn Error + 'static)) -> bool) -> bool {
    let mut current = Some(err);
    while let Some(err) = current {
        if cause(err) {
            return true;
        }
        current = err.source();
    }
    false
}

/// Corps resté muet trop longtemps.
#[derive(Debug)]
pub struct BodyIdle;

impl fmt::Display for BodyIdle {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "corps de requête interrompu : plus rien reçu")
    }
}

impl Error for BodyIdle {}

/// Corps reçu sous le débit minimal (cf. `PACE`).
#[derive(Debug)]
pub struct BodyTooSlow;

impl fmt::Display for BodyTooSlow {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "corps de requête reçu trop lentement")
    }
}

impl Error for BodyTooSlow {}

struct Watched {
    inner: Body,
    idle: Duration,
    deadline: Pin<Box<Sleep>>,
    /// Débit minimal, ou aucun.
    pace: Option<Paced>,
}

/// Débit minimal d'un corps en cours : l'échéance recule à chaque morceau reçu.
struct Paced {
    pace: Pace,
    started: Instant,
    received: u64,
    deadline: Pin<Box<Sleep>>,
}

impl HttpBody for Watched {
    type Data = Bytes;
    type Error = BoxError;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        let this = &mut *self;
        if let Some(paced) = this.pace.as_mut()
            && paced.deadline.as_mut().poll(cx).is_ready()
        {
            return Poll::Ready(Some(Err(Box::new(BodyTooSlow))));
        }
        match Pin::new(&mut this.inner).poll_frame(cx) {
            Poll::Ready(frame) => {
                let next = Instant::now() + this.idle;
                this.deadline.as_mut().reset(next);
                if let (Some(paced), Some(Ok(frame))) = (this.pace.as_mut(), frame.as_ref())
                    && let Some(data) = frame.data_ref()
                {
                    paced.received += data.len() as u64;
                    let until = paced.started + paced.pace.allowed(paced.received);
                    paced.deadline.as_mut().reset(until);
                }
                Poll::Ready(frame)
            }
            Poll::Pending => match this.deadline.as_mut().poll(cx) {
                Poll::Ready(()) => Poll::Ready(Some(Err(Box::new(BodyIdle)))),
                Poll::Pending => Poll::Pending,
            },
        }
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

/// Corps de réponse qui tient une place (pool::relay_slot) jusqu'à sa fin.
struct Guarded {
    inner: Body,
    progress: Arc<Progress>,
    _permit: OwnedSemaphorePermit,
}

impl HttpBody for Guarded {
    type Data = Bytes;
    type Error = BoxError;

    fn poll_frame(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Bytes>, BoxError>>> {
        self.progress.asked(); // hyper demande la suite : le morceau d'avant est parti
        let polled = Pin::new(&mut self.inner).poll_frame(cx);
        if matches!(polled, Poll::Ready(Some(Ok(_)))) {
            self.progress.handed();
        }
        polled
    }

    fn is_end_stream(&self) -> bool {
        self.inner.is_end_stream()
    }

    fn size_hint(&self) -> SizeHint {
        self.inner.size_hint()
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use http_body_util::{BodyExt, Full};
    use tokio::sync::Semaphore;

    use super::*;

    #[tokio::test]
    async fn a_body_is_cut_beyond_its_size() {
        let small = bounded(Full::new(Bytes::from_static(b"court")), 16, BODY_IDLE, None);
        assert_eq!(&small.collect().await.unwrap().to_bytes()[..], b"court");
        let big = bounded(Full::new(Bytes::from(vec![0u8; 17])), 16, BODY_IDLE, None);
        let err = big.collect().await.unwrap_err();
        assert!(is_too_large(err.as_ref()), "{err}");
    }

    /// Un premier morceau, puis plus rien.
    struct FirstThenSilence(bool);

    impl HttpBody for FirstThenSilence {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            if self.0 {
                return Poll::Pending;
            }
            self.0 = true;
            Poll::Ready(Some(Ok(Frame::data(Bytes::from_static(b"debut")))))
        }
    }

    #[tokio::test]
    async fn a_silent_body_is_cut() {
        let body = bounded(FirstThenSilence(false), 1 << 20, Duration::from_millis(200), None);
        let err = tokio::time::timeout(Duration::from_secs(5), body.collect())
            .await
            .expect("coupé à temps")
            .unwrap_err();
        assert!(err.is::<BodyIdle>(), "{err}");
        assert!(!is_too_large(err.as_ref()));
    }

    #[tokio::test]
    async fn a_body_that_never_ends_is_cut_below_the_minimal_rate() {
        // Jamais inactif assez longtemps (silence de 10 s), mais sous le débit minimal
        let pace = Pace { grace: Duration::from_millis(150), min_rate: 1 << 20 };
        let body = bounded(FirstThenSilence(false), 1 << 20, Duration::from_secs(10), Some(pace));
        let err = tokio::time::timeout(Duration::from_secs(5), body.collect())
            .await
            .expect("coupé à temps")
            .unwrap_err();
        assert!(err.is::<BodyTooSlow>(), "{err}");
        assert!(is_too_slow(err.as_ref()) && !is_too_large(err.as_ref()));
    }

    /// Un morceau de `size` octets toutes les `every`, `count` fois.
    struct Trickle {
        size: usize,
        count: usize,
        every: Duration,
        next: Pin<Box<Sleep>>,
    }

    impl HttpBody for Trickle {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(
            mut self: Pin<&mut Self>,
            cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            if self.count == 0 {
                return Poll::Ready(None);
            }
            if self.next.as_mut().poll(cx).is_pending() {
                return Poll::Pending;
            }
            self.count -= 1;
            let every = self.every;
            self.next.as_mut().reset(Instant::now() + every);
            Poll::Ready(Some(Ok(Frame::data(Bytes::from(vec![0u8; self.size])))))
        }
    }

    #[tokio::test]
    async fn a_slow_but_steady_body_gets_the_time_it_needs() {
        // 10 morceaux de 1 Kio toutes les 50 ms (20 Kio/s) : bien plus long en tout que le
        // délai de grâce, mais au-dessus du débit minimal (16 Kio/s) ; il passe en entier
        let every = Duration::from_millis(50);
        let body = Trickle { size: 1024, count: 10, every, next: Box::pin(tokio::time::sleep(every)) };
        let pace = Pace { grace: Duration::from_millis(100), min_rate: 16 * 1024 };
        let body = bounded(body, 1 << 20, Duration::from_secs(10), Some(pace));
        let got = tokio::time::timeout(Duration::from_secs(5), body.collect()).await.expect("fini");
        assert_eq!(got.unwrap().to_bytes().len(), 10 * 1024);
    }

    #[test]
    fn a_large_request_is_allowed_a_slow_link() {
        // 16 Mio à 16 Kio/s : 1024 s, plus le délai de grâce ; un octet, presque rien de plus
        assert_eq!(PACE.allowed(16 << 20), Duration::from_secs(120 + 1024));
        assert!(PACE.allowed(1) < Duration::from_secs(121));
    }

    #[tokio::test]
    async fn a_relay_slot_is_held_until_the_body_ends() {
        let semaphore = Arc::new(Semaphore::new(1));
        let permit = semaphore.clone().try_acquire_owned().unwrap();
        let progress = Arc::new(Progress::default());
        let body = guarded(permit, progress, reply::boxed(Full::new(Bytes::from_static(b"ok"))));
        assert_eq!(semaphore.available_permits(), 0, "la place est prise pendant la réponse");
        let _ = body.collect().await.unwrap();
        assert_eq!(semaphore.available_permits(), 1, "rendue à la fin de la réponse");
    }

    #[tokio::test]
    async fn an_unread_response_is_seen_stalled() {
        let semaphore = Arc::new(Semaphore::new(1));
        let progress = Arc::new(Progress::default());
        let permit = semaphore.clone().try_acquire_owned().unwrap();
        let stall = Duration::from_millis(100);
        let mut body =
            guarded(permit, progress.clone(), reply::boxed(Full::new(Bytes::from_static(b"ok"))));
        // Un morceau remis, jamais redemandé : l'autre bout ne lit plus
        let _ = body.frame().await.unwrap().unwrap();
        tokio::time::timeout(Duration::from_secs(5), progress.stalled(stall)).await.expect("vu bloqué");
        // Redemandé : plus bloqué
        let _ = body.frame().await;
        assert!(tokio::time::timeout(Duration::from_millis(400), progress.stalled(stall)).await.is_err());
    }

    #[test]
    fn a_work_result_may_carry_a_video() {
        assert_eq!(limit_for("/p2p/v1/ping", MAX_BODY), MAX_BODY);
        assert_eq!(limit_for(RESULT_PATH, MAX_BODY), MAX_RESULT_BODY);
        assert_eq!(limit_for(RESULT_PATH, 2 << 30), 2 << 30, "jamais plus bas que la borne générale");
        assert_eq!(limit_for("/p2p/v1/work/result/x", 1 << 20), 1 << 20);
    }

    #[test]
    fn announced_length_is_checked() {
        let mut headers = HeaderMap::new();
        assert!(!announced_too_large(&headers, 10));
        headers.insert(CONTENT_LENGTH, "11".parse().unwrap());
        assert!(announced_too_large(&headers, 10));
        headers.insert(CONTENT_LENGTH, "10".parse().unwrap());
        assert!(!announced_too_large(&headers, 10));
    }
}
