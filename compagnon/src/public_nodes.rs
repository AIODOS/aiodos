//! Annuaire du réseau public : les nœuds dont un profil signé est arrivé par le groupe de
//! discussion (public.rs), gardés en mémoire tant qu'ils se redisent (`FORGET_AFTER`).
//!
//! Relecture de sécurité du 28/10/2026 (bilan, « annuaire noyé ») : des profils signés par
//! des clés neuves remplissaient les `MAX_NODES` places et chassaient les vrais prêteurs
//! (la place du nœud vu depuis le plus longtemps allait au dernier venu), tout en attirant
//! les conversations ; et un profil rejoué de moins de 10 min était accepté pour un auteur
//! pas encore connu. Désormais, un profil n'est offert à l'annuaire que :
//! - si sa clé porte la preuve de travail du réseau public (public_identity.rs) ;
//! - pour un auteur déjà présent, s'il est **plus récent** que le dernier gardé (un profil
//!   rejoué, même signature ou date pas plus récente, est refusé) ;
//! - pour un auteur absent, s'il date de moins de `NEW_MAX_AGE` (au lieu des 10 min de
//!   `public_profile::MAX_AGE`), s'il reste une place, et dans la cadence des nouveaux venus
//!   (`NEWCOMERS`, toutes clés confondues).
//!
//! Annuaire plein : un nouveau venu **ne chasse personne**. Seuls sortent les nœuds qui ne se
//! redisent plus ; ceux qui se redisent gardent leur place, d'autant plus sûrement qu'ils
//! sont là depuis longtemps (`Seen::first`, en mémoire : l'ancienneté de la première vue,
//! que l'état donne à AIODOS, `known_s`). Un menteur qui veut remplir l'annuaire doit donc
//! créer des identités coûteuses, une à une, au rythme de `NEWCOMERS`, et les redire chaque
//! minute ; les nœuds installés avant lui ne cèdent rien.

use std::{
    collections::HashMap,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use iroh::EndpointId;

use crate::{
    cadence::{Bucket, Cadence},
    public_identity,
    public_profile::{self, Profile},
};

/// Un nœud dont aucun profil n'est arrivé depuis ce délai sort de l'annuaire.
pub const FORGET_AFTER: Duration = Duration::from_secs(5 * 60);
/// Au plus autant de nœuds dans l'annuaire ; plein, il n'en admet plus de nouveaux.
pub const MAX_NODES: usize = 1000;
/// Âge maximal du profil d'un auteur absent de l'annuaire : la diffusion d'une minute
/// (`public::PROFILE_EVERY`), plus les horloges décalées (comme `public_profile` tolère
/// 2 min d'avance). Un auteur oublié l'a été après 5 min de silence (`FORGET_AFTER`) : ses
/// profils déjà vus ont donc plus de 5 min, et un rejeu ne le fait pas revivre.
pub const NEW_MAX_AGE: Duration = Duration::from_secs(3 * 60);
/// Nouveaux venus admis dans l'annuaire, toutes clés confondues : 100 d'un coup (un nœud qui
/// arrive voit ainsi d'emblée une bonne part du réseau), puis 30 par minute.
pub const NEWCOMERS: Cadence = Cadence::per(30.0, Duration::from_secs(60), 100.0);

/// Un nœud de l'annuaire.
#[derive(Debug, Clone)]
pub struct Seen {
    /// Date (signée) de son dernier profil.
    pub ts: u64,
    pub profile: Profile,
    /// Arrivée de son dernier profil.
    pub at: Instant,
    /// Première vue (depuis qu'il est dans l'annuaire, cette fois).
    pub first: Instant,
}

/// Ce que l'annuaire fait d'un message reçu.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Offer {
    /// Message illisible, signature fausse, profil périmé ou daté du futur.
    Unreadable,
    /// Le profil de ce nœud lui-même.
    Mine,
    /// La clé de l'auteur ne porte pas la preuve de travail : ignoré.
    NoProof,
    /// Auteur présent, profil pas plus récent que le sien (rejoué) : ignoré.
    Replayed,
    /// Auteur absent, profil de plus de `NEW_MAX_AGE` : ignoré.
    TooOld,
    /// Auteur absent, annuaire plein : ignoré, personne n'est chassé.
    Full,
    /// Auteur absent, trop de nouveaux venus en ce moment (`NEWCOMERS`) : ignoré (son profil
    /// suivant, dans la minute, retentera).
    Throttled,
    /// Auteur absent, admis.
    Admitted,
    /// Auteur présent, profil mis à jour.
    Updated,
}

/// L'annuaire : les nœuds vus, et la cadence des nouveaux venus.
#[derive(Debug)]
pub struct Nodes {
    nodes: HashMap<EndpointId, Seen>,
    newcomers: Bucket,
}

impl Default for Nodes {
    fn default() -> Self {
        Nodes::new(NEWCOMERS, Instant::now())
    }
}

fn seconds(now: SystemTime) -> u64 {
    now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs()
}

impl Nodes {
    pub fn new(newcomers: Cadence, now: Instant) -> Self {
        Nodes { nodes: HashMap::new(), newcomers: Bucket::new(newcomers, now) }
    }

    /// Un message du groupe de discussion : ouvert (signature, date, bornes du profil), la
    /// clé de son auteur vérifiée (preuve de `difficulty` bits), puis offert à l'annuaire.
    pub fn receive(&mut self, content: &[u8], me: EndpointId, difficulty: u8, now: SystemTime, instant: Instant) -> Offer {
        let Ok((author, ts, profile)) = public_profile::open(content, now) else {
            return Offer::Unreadable;
        };
        if author == me {
            return Offer::Mine;
        }
        if !public_identity::proves(&author, difficulty) {
            return Offer::NoProof;
        }
        self.offer(author, ts, profile, seconds(now), instant)
    }

    /// Un profil ouvert et vérifié, de cet auteur et daté `ts` (secondes depuis 1970), à
    /// l'instant `now` (`at` : le même, en `Instant`).
    pub fn offer(&mut self, author: EndpointId, ts: u64, profile: Profile, now: u64, at: Instant) -> Offer {
        self.nodes.retain(|_, seen| at.saturating_duration_since(seen.at) < FORGET_AFTER);
        if let Some(seen) = self.nodes.get_mut(&author) {
            if ts <= seen.ts {
                return Offer::Replayed;
            }
            seen.ts = ts;
            seen.profile = profile;
            seen.at = at;
            return Offer::Updated;
        }
        if ts.saturating_add(NEW_MAX_AGE.as_secs()) < now {
            return Offer::TooOld;
        }
        if self.nodes.len() >= MAX_NODES {
            return Offer::Full;
        }
        if !self.newcomers.take(at) {
            return Offer::Throttled;
        }
        self.nodes.insert(author, Seen { ts, profile, at, first: at });
        Offer::Admitted
    }

    /// Les nœuds qui se sont redits depuis moins de `FORGET_AFTER` à l'instant `now`.
    pub fn fresh(&self, now: Instant) -> impl Iterator<Item = (&EndpointId, &Seen)> {
        self.nodes.iter().filter(move |(_, seen)| now.saturating_duration_since(seen.at) < FORGET_AFTER)
    }

    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use iroh::SecretKey;

    use super::*;
    use crate::public_profile::seal;

    /// Difficulté des essais : 8 bits (256 essais en moyenne).
    const BITS: u8 = 8;

    fn profile(model: &str) -> Profile {
        Profile { version: "2.1.0".into(), models: vec![model.into()], lends: true, ..Profile::default() }
    }

    fn proven() -> SecretKey {
        public_identity::proven(BITS)
    }

    fn unproven() -> SecretKey {
        loop {
            let key = SecretKey::generate();
            if !public_identity::proves(&key.public(), BITS) {
                return key;
            }
        }
    }

    fn me() -> EndpointId {
        SecretKey::generate().public()
    }

    #[test]
    fn a_key_without_the_proof_is_ignored_and_one_with_it_admitted() {
        let mut nodes = Nodes::default();
        let (now, at) = (SystemTime::now(), Instant::now());
        let cheap = unproven();
        let message = seal(&cheap, &profile("qwen3.5:9b"), now).unwrap();
        assert_eq!(nodes.receive(&message, me(), BITS, now, at), Offer::NoProof);
        assert!(nodes.is_empty(), "ni gardé");
        // La même chose, d'une clé qui porte la preuve
        let costly = proven();
        let message = seal(&costly, &profile("qwen3.5:9b"), now).unwrap();
        assert_eq!(nodes.receive(&message, me(), BITS, now, at), Offer::Admitted);
        assert_eq!(nodes.fresh(at).next().map(|(id, _)| *id), Some(costly.public()));
        // Son propre profil, ou un message illisible : rien
        assert_eq!(nodes.receive(&message, costly.public(), BITS, now, at), Offer::Mine);
        assert_eq!(nodes.receive(b"{}", me(), BITS, now, at), Offer::Unreadable);
    }

    #[test]
    fn a_replayed_profile_is_refused_and_the_window_of_a_newcomer_is_short() {
        let mut nodes = Nodes::default();
        let (now, at) = (SystemTime::now(), Instant::now());
        let author = proven();
        let first = seal(&author, &profile("a"), now).unwrap();
        assert_eq!(nodes.receive(&first, me(), BITS, now, at), Offer::Admitted);
        // Le même message rejoué (même signature), puis un plus ancien : refusés
        assert_eq!(nodes.receive(&first, me(), BITS, now, at), Offer::Replayed);
        let older = seal(&author, &profile("ancien"), now - Duration::from_secs(30)).unwrap();
        assert_eq!(nodes.receive(&older, me(), BITS, now, at), Offer::Replayed);
        assert_eq!(nodes.fresh(at).next().unwrap().1.profile.models, vec!["a".to_string()]);
        // Plus récent : mis à jour, sans perdre l'ancienneté de sa première vue
        let later = now + Duration::from_secs(60);
        let next = seal(&author, &profile("b"), later).unwrap();
        let at_later = at + Duration::from_secs(60);
        assert_eq!(nodes.receive(&next, me(), BITS, later, at_later), Offer::Updated);
        let (_, seen) = nodes.fresh(at_later).next().unwrap();
        assert_eq!((seen.first, seen.profile.models[0].as_str()), (at, "b"));
        // Un auteur absent : 9 min (accepté avant la relecture, sous les 10 min) puis 2 min
        let stranger = proven();
        let replay = seal(&stranger, &profile("x"), now - Duration::from_secs(9 * 60)).unwrap();
        assert_eq!(nodes.receive(&replay, me(), BITS, now, at), Offer::TooOld);
        let recent = seal(&stranger, &profile("x"), now - Duration::from_secs(2 * 60)).unwrap();
        assert_eq!(nodes.receive(&recent, me(), BITS, now, at), Offer::Admitted);
        // Oublié après 5 min de silence : son dernier profil, rejoué, a plus de 3 min
        let gone = now + FORGET_AFTER + Duration::from_secs(60);
        let gone_at = at + FORGET_AFTER + Duration::from_secs(60);
        assert_eq!(nodes.receive(&next, me(), BITS, gone, gone_at), Offer::TooOld);
    }

    #[test]
    fn a_full_directory_keeps_its_nodes_and_a_newcomer_chases_no_one() {
        let start = Instant::now();
        let now = 1_000_000;
        // Pas de borne de cadence ici : seule la place compte
        let mut nodes = Nodes::new(Cadence { per_second: 1e9, burst: 1e9 }, start);
        let installed: Vec<EndpointId> = (0..MAX_NODES).map(|_| SecretKey::generate().public()).collect();
        for (i, id) in installed.iter().enumerate() {
            let at = start + Duration::from_millis(i as u64);
            assert_eq!(nodes.offer(*id, now, profile("m"), now, at), Offer::Admitted);
        }
        // Avant la relecture : le nœud vu depuis le plus longtemps sortait pour le dernier venu
        let later = start + Duration::from_secs(10);
        let newcomer = SecretKey::generate().public();
        assert_eq!(nodes.offer(newcomer, now + 10, profile("m"), now + 10, later), Offer::Full);
        assert_eq!(nodes.len(), MAX_NODES);
        assert!(nodes.fresh(later).any(|(id, _)| *id == installed[0]), "le plus ancien reste");
        // Les installés se redisent toujours, plein ou non
        assert_eq!(nodes.offer(installed[0], now + 60, profile("m"), now + 60, later), Offer::Updated);
        // Un nœud qui ne se redit plus sort : sa place va à un nouveau venu (les autres se
        // sont redits)
        let much_later = later + FORGET_AFTER + Duration::from_secs(1);
        let ts = now + 320;
        for id in &installed[1..] {
            assert_ne!(nodes.offer(*id, ts, profile("m"), ts, much_later), Offer::Full);
        }
        assert!(!nodes.fresh(much_later).any(|(id, _)| *id == installed[0]));
        assert_eq!(nodes.offer(newcomer, ts, profile("m"), ts, much_later), Offer::Admitted);
        assert_eq!(nodes.len(), MAX_NODES);
    }

    #[test]
    fn newcomers_are_admitted_at_a_bounded_pace() {
        let start = Instant::now();
        let now = 1_000_000;
        let mut nodes = Nodes::new(NEWCOMERS, start);
        let offer = |nodes: &mut Nodes, at: Instant| {
            nodes.offer(SecretKey::generate().public(), now, profile("m"), now, at)
        };
        let admitted = (0..500).filter(|_| offer(&mut nodes, start) == Offer::Admitted).count();
        assert_eq!(admitted, NEWCOMERS.burst as usize, "d'un coup, pas plus");
        assert_eq!(offer(&mut nodes, start), Offer::Throttled);
        // Une minute plus tard : 30 de plus
        let minute = start + Duration::from_secs(60);
        let admitted = (0..500).filter(|_| offer(&mut nodes, minute) == Offer::Admitted).count();
        assert_eq!(admitted, 30);
        // Les nœuds présents se redisent sans jeton
        let (id, _) = nodes.fresh(minute).next().map(|(id, seen)| (*id, seen.ts)).unwrap();
        assert_eq!(nodes.offer(id, now + 1, profile("m"), now + 1, minute), Offer::Updated);
    }
}
