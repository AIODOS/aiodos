//! Identité du nœud public, coûteuse à créer (relecture de sécurité du 28/10/2026, bilan
//! « annuaire noyé ») : une clé neuve ne coûte rien, et des milliers de profils signés par
//! des clés neuves pouvaient remplir l'annuaire des autres nœuds (public.rs) et attirer les
//! conversations. Désormais, la clé d'un nœud public n'est reçue dans l'annuaire des autres
//! (ni comme voisin du groupe de discussion) que si
//! SHA-256(`DOMAIN` ‖ clé publique) commence par `DIFFICULTY` bits nuls : une preuve de
//! travail portée par la clé elle-même, sans rien de plus sur le fil, vérifiée d'un seul
//! SHA-256.
//!
//! Le compagnon tire des clés jusqu'à en trouver une, **une fois**, à la création, sur tous
//! les cœurs (`search`), puis la garde dans `public::KEY_FILE` ; une clé gardée qui ne
//! satisfait pas la difficulté (créée avant le 29/10, ou sous une difficulté plus basse) est
//! remplacée : le nœud public change d'identité, ce qui ne coûte rien (rien n'est publié
//! sous cette clé, elle ne sert qu'au réseau public).
//!
//! Chaque essai coûte surtout la clé Ed25519 (un SHA-512 et une multiplication scalaire),
//! le SHA-256 de la preuve est négligeable. Mesuré le 29/10/2026 (`measure_the_cost`, PC
//! portable i9-14900HX, 8 grands cœurs et 16 petits) : 48 000 à 60 000 essais par seconde
//! sur un fil, 300 000 à 360 000 sur les 32 (le gain des fils est borné par la puissance du
//! portable). Un téléphone récent, 4 à 6 fois plus lent par cœur, sur 8 cœurs inégaux
//! (l'équivalent de 4 à 5 grands) : de 35 000 à 75 000 par seconde. À `DIFFICULTY` = 19
//! (2^19 = 524 288 essais en moyenne), une identité coûte 1,5 à 2 s à ce PC, et 7 à 15 s en
//! moyenne à un téléphone ; le tirage suit une loi géométrique (1 fois sur 20, trois fois
//! la moyenne : 20 à 45 s au téléphone). 1 000 identités : ~25 min de ce PC à pleine
//! charge, 2 à 4 h d'un téléphone. À 20 bits, le double : un téléphone dépasserait 30 s une
//! fois sur quatre.
//!
//! Limite assumée : la preuve porte sur le calcul d'une clé Ed25519, qu'une carte graphique
//! fait bien plus vite (des générateurs de clés « personnalisées » en font des dizaines de
//! millions par seconde) ; elle rend coûteux d'en créer des milliers, pas impossible. D'où
//! aussi l'annuaire qui garde les nœuds installés et n'admet qu'un nombre borné de nouveaux
//! venus par minute (public_nodes.rs).

use std::{
    path::Path,
    sync::{
        Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread,
};

use anyhow::{Context, Result};
use iroh::{EndpointId, SecretKey};
use sha2::{Digest, Sha256};
use tracing::info;

use crate::identity;

/// Préfixe de la preuve de travail : la même clé ne sert à rien pour un autre usage.
pub const DOMAIN: &[u8] = b"aiodos/public/pow";
/// Bits nuls exigés en tête de SHA-256(`DOMAIN` ‖ clé publique) (cf. en tête pour la mesure).
/// Les essais en prennent une plus basse (`Config::public_difficulty`, option
/// `--public-difficulty`).
pub const DIFFICULTY: u8 = 19;
/// Au-delà, la recherche ne finirait pas (2^40 essais : des mois) : refusée.
pub const MAX_DIFFICULTY: u8 = 32;
/// Essais entre deux regards sur les autres fils (une clé trouvée ailleurs arrête tout).
const BATCH: u64 = 256;

/// Bits nuls en tête de SHA-256(`DOMAIN` ‖ clé publique).
pub fn work(key: &EndpointId) -> u32 {
    let digest = Sha256::new().chain_update(DOMAIN).chain_update(key.as_bytes()).finalize();
    let mut bits = 0;
    for byte in digest {
        bits += byte.leading_zeros();
        if byte != 0 {
            break;
        }
    }
    bits
}

/// La clé porte-t-elle une preuve d'au moins `difficulty` bits ?
pub fn proves(key: &EndpointId, difficulty: u8) -> bool {
    work(key) >= u32::from(difficulty)
}

/// Tire des clés jusqu'à en trouver une qui satisfait `difficulty`, sur `threads` fils (au
/// moins un), ou jusqu'à ce que `stop` soit levé. → (la clé, essais faits en tout), ou `None`
/// si arrêtée. Bloquant : à lancer hors de la boucle asynchrone
/// (`tokio::task::spawn_blocking`). Chaque fil part d'une graine tirée au hasard (32 octets
/// du système), dont il fait varier les 8 derniers : 192 bits de hasard au moins dans chaque
/// clé essayée, sans un appel au système par essai.
pub fn search(difficulty: u8, threads: usize, stop: &AtomicBool) -> Option<(SecretKey, u64)> {
    let found = AtomicBool::new(false);
    let tries = AtomicU64::new(0);
    let winner: Mutex<Option<SecretKey>> = Mutex::new(None);
    thread::scope(|scope| {
        for _ in 0..threads.max(1) {
            scope.spawn(|| {
                let mut seed = [0u8; 32];
                getrandom::fill(&mut seed).expect("hasard du système");
                let base = u64::from_le_bytes(seed[24..].try_into().expect("8 octets"));
                let mut counter = 0u64;
                while !found.load(Ordering::Relaxed) && !stop.load(Ordering::Relaxed) {
                    for _ in 0..BATCH {
                        counter = counter.wrapping_add(1);
                        seed[24..].copy_from_slice(&(base ^ counter).to_le_bytes());
                        let secret = SecretKey::from_bytes(&seed);
                        if proves(&secret.public(), difficulty) {
                            found.store(true, Ordering::Relaxed);
                            winner.lock().expect("clé trouvée").get_or_insert(secret);
                            break;
                        }
                    }
                    tries.fetch_add(BATCH, Ordering::Relaxed);
                }
            });
        }
    });
    let secret = winner.into_inner().expect("clé trouvée")?;
    Some((secret, tries.into_inner()))
}

/// Une clé qui porte `difficulty` bits de preuve, cherchée sur un fil (essais).
#[doc(hidden)]
pub fn proven(difficulty: u8) -> SecretKey {
    search(difficulty, 1, &AtomicBool::new(false)).expect("jamais arrêtée").0
}

/// Fils pour la recherche : tous les cœurs (une fois, à la création).
pub fn threads() -> usize {
    thread::available_parallelism().map_or(1, |n| n.get())
}

/// D'où vient la clé du nœud public.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// Gardée dans son fichier, et elle satisfait la difficulté.
    Kept,
    /// Aucune clé encore : une nouvelle, cherchée.
    Created,
    /// La clé gardée ne satisfaisait pas la difficulté : remplacée (nouvelle identité).
    Replaced,
}

/// La clé du nœud public, gardée dans `file` du dossier de données : celle du fichier si
/// elle satisfait `difficulty`, sinon une nouvelle, cherchée (`search`, jusqu'à `stop`)
/// puis écrite à sa place. Bloquant (cf. `search`). Une clé illisible arrête, comme celle
/// de la machine (cf. identity.rs), plutôt que d'être écrasée en silence.
pub fn load_or_create(data_dir: &Path, file: &str, difficulty: u8, stop: &AtomicBool) -> Result<(SecretKey, Origin)> {
    anyhow::ensure!(difficulty <= MAX_DIFFICULTY, "difficulté trop haute ({difficulty} bits, {MAX_DIFFICULTY} au plus)");
    let kept = identity::read_file(data_dir, file)?;
    if let Some(secret) = &kept
        && proves(&secret.public(), difficulty)
    {
        return Ok((secret.clone(), Origin::Kept));
    }
    let started = std::time::Instant::now();
    let Some((secret, tries)) = search(difficulty, threads(), stop) else {
        anyhow::bail!("recherche de la clé du réseau public arrêtée");
    };
    identity::write_file(data_dir, file, &secret)
        .with_context(|| format!("clé du réseau public ({file})"))?;
    info!(
        "Réseau public : identité créée ({difficulty} bits de preuve de travail, {tries} essais, {:.1} s){}",
        started.elapsed().as_secs_f64(),
        if kept.is_some() { " ; l'ancienne clé, sans cette preuve, est remplacée" } else { "" },
    );
    let origin = if kept.is_some() { Origin::Replaced } else { Origin::Created };
    Ok((secret, origin))
}

#[cfg(test)]
mod tests {
    use std::time::Instant;

    use super::*;

    #[test]
    fn work_counts_the_leading_zero_bits_of_the_proof() {
        // Une clé tirée au hasard : la moitié n'a aucun bit nul en tête
        let keys: Vec<EndpointId> = (0..256).map(|_| SecretKey::generate().public()).collect();
        let none = keys.iter().filter(|k| work(k) == 0).count();
        assert!((64..192).contains(&none), "{none} sur 256");
        for key in &keys {
            let digest = Sha256::new().chain_update(DOMAIN).chain_update(key.as_bytes()).finalize();
            assert_eq!(work(key) >= 8, digest[0] == 0);
            assert_eq!(proves(key, 1), digest[0] & 0x80 == 0);
            assert!(proves(key, 0));
        }
    }

    #[test]
    fn a_searched_key_carries_its_proof() {
        let (secret, tries) = search(12, 2, &AtomicBool::new(false)).unwrap();
        assert!(proves(&secret.public(), 12));
        assert!(tries >= 1);
        // Signée comme une autre (rien ne change pour Iroh)
        let signature = secret.sign(b"profil");
        assert!(secret.public().verify(b"profil", &signature).is_ok());
    }

    #[test]
    fn a_search_stops_when_asked() {
        // 30 bits : des heures ; arrêtée (arrêt du compagnon), elle rend la main aussitôt
        let stop = AtomicBool::new(false);
        let started = Instant::now();
        let stopped = thread::scope(|scope| {
            let search = scope.spawn(|| search(30, 2, &stop));
            thread::sleep(std::time::Duration::from_millis(200));
            stop.store(true, Ordering::Relaxed);
            search.join().unwrap()
        });
        assert!(stopped.is_none());
        assert!(started.elapsed().as_secs() < 5);
        let dir = tempfile::tempdir().unwrap();
        assert!(load_or_create(dir.path(), "aiodos-public.key", 30, &stop).is_err());
        assert!(!dir.path().join("aiodos-public.key").exists(), "rien d'écrit");
    }

    #[test]
    fn a_kept_key_without_the_proof_is_replaced_once_then_kept() {
        let dir = tempfile::tempdir().unwrap();
        const FILE: &str = "aiodos-public.key";
        let go = AtomicBool::new(false);
        // Une clé d'avant le 29/10 (aucune preuve exigée), sans les 10 bits
        let old = loop {
            let key = SecretKey::generate();
            if !proves(&key.public(), 10) {
                break key;
            }
        };
        identity::write_file(dir.path(), FILE, &old).unwrap();
        let (new, origin) = load_or_create(dir.path(), FILE, 10, &go).unwrap();
        assert_eq!(origin, Origin::Replaced);
        assert_ne!(new.public(), old.public(), "nouvelle identité");
        assert!(proves(&new.public(), 10));
        // Gardée ensuite, sans nouvelle recherche
        let (again, origin) = load_or_create(dir.path(), FILE, 10, &go).unwrap();
        assert_eq!((again.public(), origin), (new.public(), Origin::Kept));
        // Aucune clé : créée
        let empty = tempfile::tempdir().unwrap();
        let (_, origin) = load_or_create(empty.path(), FILE, 4, &go).unwrap();
        assert_eq!(origin, Origin::Created);
        // Une clé illisible n'est pas écrasée
        std::fs::write(dir.path().join(FILE), "abîmée").unwrap();
        assert!(load_or_create(dir.path(), FILE, 10, &go).is_err());
        assert!(load_or_create(empty.path(), FILE, MAX_DIFFICULTY + 1, &go).is_err());
    }

    /// Mesure du coût (`cargo test --release -- --ignored --nocapture measure_the_cost`) :
    /// essais par seconde sur un fil, puis sur tous ; et ce qu'en déduit `DIFFICULTY`.
    #[test]
    #[ignore = "mesure, plusieurs secondes"]
    fn measure_the_cost() {
        // Des recherches assez longues pour que le lancement des fils ne compte pas
        let rate = |threads: usize, difficulty: u8| {
            let started = Instant::now();
            let mut tries = 0;
            while started.elapsed().as_secs_f64() < 8.0 {
                tries += search(difficulty, threads, &AtomicBool::new(false)).unwrap().1;
            }
            tries as f64 / started.elapsed().as_secs_f64()
        };
        let one = rate(1, 14);
        let all = rate(threads(), 20);
        let expected = 2f64.powi(i32::from(DIFFICULTY));
        println!("un fil : {one:.0} essais/s ; {} fils : {all:.0} essais/s", threads());
        println!(
            "{DIFFICULTY} bits : {:.1} s sur ce PC en moyenne ; 1 000 identités : {:.0} min",
            expected / all,
            1000.0 * expected / all / 60.0
        );
        for (slower, cores) in [(4.0, 8.0), (6.0, 8.0), (6.0, 4.0)] {
            println!(
                "téléphone {slower} fois plus lent par cœur, {cores} cœurs : {:.0} s en moyenne",
                expected / (one / slower * cores)
            );
        }
    }
}
