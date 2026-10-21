//! Connexions de l'API locale qui n'ont pas encore montré le jeton : bornées. Au-delà, la
//! plus ancienne est fermée pour faire place à la nouvelle.
//!
//! Sur 127.0.0.1, n'importe quel programme de la machine (une autre appli du téléphone)
//! peut ouvrir des milliers de connexions muettes : le compagnon épuiserait ses
//! descripteurs, et ne pourrait plus ni répondre à AIODOS ni ouvrir de socket pour Iroh.
//! Bloquer `accept` au-delà d'un plafond rendrait l'attaque encore plus simple (quelques
//! connexions suffiraient à tout bloquer) ; écarter la plus ancienne laisse passer AIODOS,
//! qui envoie sa requête dans la milliseconde. Une connexion qui a montré le jeton ne compte
//! plus.

use std::{
    collections::VecDeque,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

use tokio_util::sync::CancellationToken;

/// Connexions sans jeton gardées ouvertes en même temps, au plus.
pub const MAX_WAITING: usize = 512;

pub struct Admission {
    max: usize,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    next: u64,
    waiting: VecDeque<(u64, CancellationToken)>,
}

/// La place d'une connexion : rendue quand elle se ferme.
pub struct Ticket {
    id: u64,
    evicted: CancellationToken,
    admitted: AtomicBool,
    admission: Arc<Admission>,
}

impl Admission {
    pub fn new(max: usize) -> Arc<Self> {
        Arc::new(Admission { max: max.max(1), state: Mutex::default() })
    }

    /// Une nouvelle connexion entre ; la plus ancienne sans jeton sort s'il le faut.
    pub fn enter(self: &Arc<Self>) -> Arc<Ticket> {
        let mut state = self.state.lock().expect("admission");
        let id = state.next;
        state.next += 1;
        let evicted = CancellationToken::new();
        state.waiting.push_back((id, evicted.clone()));
        while state.waiting.len() > self.max {
            if let Some((_, oldest)) = state.waiting.pop_front() {
                oldest.cancel();
            }
        }
        Arc::new(Ticket { id, evicted, admitted: AtomicBool::new(false), admission: self.clone() })
    }

    /// Connexions sans jeton en ce moment.
    pub fn waiting(&self) -> usize {
        self.state.lock().expect("admission").waiting.len()
    }

    fn leave(&self, id: u64) {
        let mut state = self.state.lock().expect("admission");
        if let Some(at) = state.waiting.iter().position(|(other, _)| *other == id) {
            state.waiting.remove(at);
        }
    }
}

impl Ticket {
    /// La connexion a montré le jeton : elle ne sera plus écartée.
    pub fn admit(&self) {
        if !self.admitted.swap(true, Ordering::SeqCst) {
            self.admission.leave(self.id);
        }
    }

    /// Annulé quand la connexion doit laisser sa place.
    pub fn evicted(&self) -> CancellationToken {
        self.evicted.clone()
    }
}

impl Drop for Ticket {
    fn drop(&mut self) {
        self.admission.leave(self.id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_oldest_waiting_connection_makes_room() {
        let admission = Admission::new(2);
        let (one, two) = (admission.enter(), admission.enter());
        two.admit(); // a montré le jeton : ne compte plus
        let three = admission.enter();
        assert_eq!(admission.waiting(), 2);
        let four = admission.enter();
        assert!(one.evicted().is_cancelled(), "la plus ancienne sans jeton sort");
        assert!(!two.evicted().is_cancelled() && !three.evicted().is_cancelled());
        assert!(!four.evicted().is_cancelled());
        drop(three);
        assert_eq!(admission.waiting(), 1);
        drop((one, four));
        assert_eq!(admission.waiting(), 0);
    }
}
