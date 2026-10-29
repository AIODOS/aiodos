//! Cadences bornées (seau à jetons) du réseau public : nouvelles connexions et nouveaux flux
//! d'inconnus chez un prêteur (public_serve.rs), nouveaux venus dans l'annuaire
//! (public_nodes.rs). Un seau se remplit de `per_second` jetons par seconde, `burst` au
//! plus ; chaque admission en prend un, et sans jeton c'est un refus, aussitôt. Rien n'est
//! gardé de qui demande : un seau par usage, ou par connexion.

use std::time::{Duration, Instant};

/// Une cadence : `per_second` en moyenne, `burst` d'un coup au plus (un seau plein).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Cadence {
    pub per_second: f64,
    pub burst: f64,
}

impl Cadence {
    /// `count` par `period` en moyenne, `burst` d'un coup au plus.
    pub const fn per(count: f64, period: Duration, burst: f64) -> Cadence {
        Cadence { per_second: count / period.as_secs_f64(), burst }
    }
}

/// Un seau à jetons, plein au départ.
#[derive(Debug, Clone)]
pub struct Bucket {
    cadence: Cadence,
    tokens: f64,
    at: Instant,
}

impl Bucket {
    pub fn new(cadence: Cadence, now: Instant) -> Bucket {
        Bucket { cadence, tokens: cadence.burst, at: now }
    }

    fn refill(&mut self, now: Instant) {
        let elapsed = now.saturating_duration_since(self.at).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.cadence.per_second).min(self.cadence.burst);
        // Une horloge qui recule (essais) ne rend rien, et ne retire rien
        self.at = self.at.max(now);
    }

    /// Prend un jeton s'il y en a un : → admis ou non.
    pub fn take(&mut self, now: Instant) -> bool {
        self.refill(now);
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    /// Jetons entiers disponibles maintenant (état, essais).
    pub fn available(&mut self, now: Instant) -> u32 {
        self.refill(now);
        self.tokens as u32
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_burst_then_the_average_pace() {
        let start = Instant::now();
        let mut bucket = Bucket::new(Cadence { per_second: 10.0, burst: 20.0 }, start);
        let admitted = (0..100).filter(|_| bucket.take(start)).count();
        assert_eq!(admitted, 20, "la rafale, pas plus");
        // Une seconde plus tard : dix de plus
        let later = start + Duration::from_secs(1);
        assert_eq!((0..100).filter(|_| bucket.take(later)).count(), 10);
        // Longtemps après : la rafale seulement, rien d'accumulé au-delà
        let much_later = later + Duration::from_secs(3600);
        assert_eq!(bucket.available(much_later), 20);
        // Une horloge qui recule ne donne rien
        assert!(!Bucket::new(Cadence { per_second: 1.0, burst: 0.0 }, much_later).take(start));
    }

    #[test]
    fn a_pace_per_minute() {
        let cadence = Cadence::per(60.0, Duration::from_secs(60), 5.0);
        assert!((cadence.per_second - 1.0).abs() < 1e-9);
    }
}
