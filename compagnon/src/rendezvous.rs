//! Rendez-vous du réseau public dans la DHT Mainline de BitTorrent (phase 3.0).
//!
//! Iroh se connecte à une clé, pas à une adresse : un nouveau nœud public doit donc
//! trouver les clés de quelques autres, sans serveur. Une annonce BEP 5 ne rend que des
//! adresses IP, et l'annonce signée (« signed peers ») n'est comprise que par quelques
//! nœuds de la DHT. Les éléments signés BEP 44, eux, le sont par la plupart (ce sont eux
//! qui portent l'annuaire DHT d'Iroh) : chaque créneau de 15 minutes a sa clé de
//! signature, tirée du nom du réseau et du numéro du créneau, que tout nœud public sait
//! recalculer ; sous elle, huit cases (sels « 0 » à « 7 »). Chaque nœud inscrit sa clé
//! dans la case que lui désigne son empreinte, à côté des clés inscrites récemment, et
//! lit toutes les cases du créneau en cours et du précédent.
//!
//! Limites assumées : la clé d'un créneau est connue de tous, donc n'importe qui peut
//! réécrire une case (la vider, y mettre des clés inventées). Cela gêne le rendez-vous,
//! sans plus : une clé lue ne sert qu'à proposer une connexion, qu'Iroh authentifie, et
//! rien n'est confié en 3.0. Et les nœuds de la DHT qui gardent une case voient
//! l'adresse IP de qui l'écrit (comme pour l'annuaire DHT d'Iroh).

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Result, anyhow};
use iroh::EndpointId;
use n0_mainline::{Dht, MutableItem, SigningKey, errors::PutMutableError};
use sha2::{Digest, Sha256};
use tokio::task::JoinSet;

/// Nom du réseau : un autre nom, c'est un autre réseau (essais, versions incompatibles).
pub const NETWORK: &str = "aiodos/public/1";
/// Durée d'un créneau.
pub const SLOT: Duration = Duration::from_secs(15 * 60);
/// Cases par créneau.
pub const CELLS: u8 = 8;
/// Clés gardées par case : 2 + 26 × 36 = 938 octets, sous la limite de 1000 du BEP 44.
pub const PER_CELL: usize = 26;
const MAGIC: &[u8; 2] = b"A1";
const ENTRY: usize = 32 + 4;
/// Une inscription plus vieille que deux créneaux est retirée de sa case.
const FRESH_MINUTES: u32 = 2 * 15;

/// Numéro du créneau à cet instant.
pub fn slot_of(now: SystemTime) -> u64 {
    now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() / SLOT.as_secs()
}

fn minute_of(now: SystemTime) -> u32 {
    (now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() / 60) as u32
}

/// Clé de signature d'un créneau, la même pour tous les nœuds publics.
pub fn slot_signer(slot: u64) -> SigningKey {
    let seed: [u8; 32] = Sha256::digest(format!("{NETWORK}/rendez-vous/{slot}").as_bytes()).into();
    SigningKey::from_bytes(&seed)
}

/// Case où ce nœud s'inscrit pendant ce créneau (elle change d'un créneau à l'autre).
pub fn cell_of(me: &EndpointId, slot: u64) -> u8 {
    let mut hash = Sha256::new();
    hash.update(me.as_bytes());
    hash.update(slot.to_be_bytes());
    hash.finalize()[0] % CELLS
}

fn salt(cell: u8) -> [u8; 1] {
    [b'0' + cell]
}

/// Contenu d'une case : « A1 », puis pour chaque clé ses 32 octets et la minute de son
/// inscription (secondes depuis 1970 / 60, en gros-boutiste).
pub fn encode(entries: &[(EndpointId, u32)]) -> Vec<u8> {
    let mut value = Vec::with_capacity(MAGIC.len() + entries.len().min(PER_CELL) * ENTRY);
    value.extend_from_slice(MAGIC);
    for (id, minute) in entries.iter().take(PER_CELL) {
        value.extend_from_slice(id.as_bytes());
        value.extend_from_slice(&minute.to_be_bytes());
    }
    value
}

/// Lit une case ; ce qui n'est pas une clé valable est ignoré.
pub fn decode(value: &[u8]) -> Vec<(EndpointId, u32)> {
    let Some(rest) = value.strip_prefix(MAGIC.as_slice()) else {
        return Vec::new();
    };
    rest.as_chunks::<ENTRY>()
        .0
        .iter()
        .take(PER_CELL)
        .filter_map(|chunk| {
            let key: [u8; 32] = chunk[..32].try_into().ok()?;
            let minute = u32::from_be_bytes(chunk[32..].try_into().ok()?);
            Some((EndpointId::from_bytes(&key).ok()?, minute))
        })
        .collect()
}

/// Nouvelle valeur de la case : ce nœud en tête, puis les inscriptions récentes des
/// autres (sans doublon), au plus `PER_CELL`.
pub fn merge(current: &[(EndpointId, u32)], me: EndpointId, now: SystemTime) -> Vec<(EndpointId, u32)> {
    let minute = minute_of(now);
    let mut entries = vec![(me, minute)];
    for (id, at) in current {
        if fresh(*at, minute) && !entries.iter().any(|(seen, _)| seen == id) {
            entries.push((*id, *at));
        }
    }
    entries.truncate(PER_CELL);
    entries
}

/// Une inscription récente, et pas datée du futur (au-delà de 5 minutes d'écart d'horloge) :
/// un tricheur ne passe pas devant les autres en datant ses clés de demain.
fn fresh(at: u32, minute: u32) -> bool {
    at <= minute.saturating_add(5) && minute.saturating_sub(at) <= FRESH_MINUTES
}

/// Inscrit ce nœud dans sa case du créneau en cours. → nombre de clés dans la case. Une
/// case bloquée (numéro de version au maximum, réécrite sans cesse) : la suivante.
pub async fn announce(dht: &Dht, me: EndpointId, now: SystemTime) -> Result<usize> {
    let slot = slot_of(now);
    let signer = slot_signer(slot);
    let key = signer.verifying_key().to_bytes();
    let first = cell_of(&me, slot);
    let mut last = None;
    for step in 0..CELLS {
        let salt = salt((first + step) % CELLS);
        for _ in 0..3 {
            let current = dht
                .get_mutable_most_recent(&key, Some(&salt))
                .await
                .map_err(|_| anyhow!("DHT arrêtée"))?;
            let (entries, cas) = match &current {
                Some(item) => (decode(item.value()), Some(item.seq())),
                None => (Vec::new(), None),
            };
            // Numéro au maximum : personne ne peut plus écrire cette case
            let Some(seq) = cas.map_or(Some(1), |seq| seq.checked_add(1)) else {
                last = Some(anyhow!("case bloquée"));
                break;
            };
            let next = merge(&entries, me, now);
            let item = MutableItem::new(&signer, &encode(&next), seq, Some(&salt));
            match dht.put_mutable(item, cas).await {
                Ok(_) => return Ok(next.len()),
                // Écrite entre-temps par un autre nœud : relire, puis réessayer
                Err(PutMutableError::Concurrency(err)) => last = Some(anyhow!("{err}")),
                Err(PutMutableError::Query(err)) => {
                    return Err(anyhow!("inscription dans la DHT : {err}"));
                }
            }
        }
    }
    Err(last.unwrap_or_else(|| anyhow!("inscription dans la DHT impossible")))
}

/// Clés récentes inscrites dans les cases du créneau en cours et du précédent, sauf
/// celle-ci, dans un ordre tiré au hasard : les dates sont écrites par n'importe qui, elles
/// ne décident pas des nœuds joints en premier.
pub async fn discover(dht: &Dht, me: EndpointId, now: SystemTime) -> Vec<EndpointId> {
    let slot = slot_of(now);
    let mut lookups = JoinSet::new();
    for slot in [slot, slot.saturating_sub(1)] {
        let key = slot_signer(slot).verifying_key().to_bytes();
        for cell in 0..CELLS {
            let dht = dht.clone();
            lookups.spawn(async move {
                let found = dht.get_mutable_most_recent(&key, Some(&salt(cell))).await;
                found.ok().flatten().map(|item| decode(item.value())).unwrap_or_default()
            });
        }
    }
    let minute = minute_of(now);
    let mut seen: Vec<EndpointId> = Vec::new();
    while let Some(Ok(entries)) = lookups.join_next().await {
        for (id, at) in entries {
            if id != me && fresh(at, minute) && !seen.contains(&id) {
                seen.push(id);
            }
        }
    }
    shuffle(&mut seen);
    seen
}

/// Mélange de Fisher-Yates, avec le hasard du système.
fn shuffle<T>(items: &mut [T]) {
    for i in (1..items.len()).rev() {
        let j = getrandom::u64().map_or(0, |r| (r % (i as u64 + 1)) as usize);
        items.swap(i, j);
    }
}

#[cfg(test)]
mod tests {
    use iroh::SecretKey;

    use super::*;

    fn key() -> EndpointId {
        SecretKey::generate().public()
    }

    #[test]
    fn a_cell_round_trips_and_ignores_garbage() {
        let entries = vec![(key(), 100), (key(), 99)];
        assert_eq!(decode(&encode(&entries)), entries);
        assert!(decode(b"autre chose").is_empty());
        let mut value = encode(&entries);
        value.extend_from_slice(&[7; 10]); // reste incomplet : ignoré
        assert_eq!(decode(&value), entries);
        assert!(encode(&vec![(key(), 1); 40]).len() <= 1000);
    }

    #[test]
    fn every_public_node_computes_the_same_slot_key() {
        assert_eq!(slot_signer(42).verifying_key(), slot_signer(42).verifying_key());
        assert_ne!(slot_signer(42).verifying_key(), slot_signer(43).verifying_key());
        let me = key();
        assert!(cell_of(&me, 42) < CELLS);
    }

    #[test]
    fn merging_puts_me_first_and_drops_stale_or_duplicate_keys() {
        let now = UNIX_EPOCH + Duration::from_secs(60 * 10_000);
        let (me, other, old) = (key(), key(), key());
        let current = vec![(other, 9_990), (me, 9_000), (old, 9_000), (other, 9_980)];
        let merged = merge(&current, me, now);
        assert_eq!(merged, vec![(me, 10_000), (other, 9_990)]);
        let full: Vec<_> = (0..40).map(|_| (key(), 10_000)).collect();
        assert_eq!(merge(&full, me, now).len(), PER_CELL);
    }

    #[tokio::test]
    async fn two_nodes_meet_through_a_local_dht() {
        let testnet = n0_mainline::Testnet::new(5).await.unwrap();
        let dht = || Dht::builder().bootstrap(&testnet.bootstrap).build().unwrap();
        let (a, b) = (dht(), dht());
        let (ka, kb) = (key(), key());
        let now = SystemTime::now();
        announce(&a, ka, now).await.unwrap();
        announce(&b, kb, now).await.unwrap();
        assert_eq!(discover(&a, ka, now).await, vec![kb]);
        assert_eq!(discover(&b, kb, now).await, vec![ka]);
    }

    /// Relecture du 20/10 : une case bloquée au numéro maximal (n'importe qui peut l'écrire)
    /// ne fait ni planter ni taire le nœud ; des clés datées du futur sont ignorées.
    #[tokio::test]
    async fn a_jammed_cell_or_future_dated_keys_do_not_win() {
        let testnet = n0_mainline::Testnet::new(5).await.unwrap();
        let dht = Dht::builder().bootstrap(&testnet.bootstrap).build().unwrap();
        let now = SystemTime::now();
        let slot = slot_of(now);
        let signer = slot_signer(slot);
        let (me, honest, cheat) = (key(), key(), key());
        let mine = salt(cell_of(&me, slot));
        let jam = MutableItem::new(&signer, &encode(&[]), i64::MAX, Some(&mine));
        dht.put_mutable(jam, None).await.unwrap();
        announce(&dht, me, now).await.unwrap();          // dans la case suivante
        announce(&dht, honest, now).await.unwrap();
        // Une case qu'aucun des deux n'a prise (le tricheur ne réécrit pas la leur)
        let used = [cell_of(&me, slot), (cell_of(&me, slot) + 1) % CELLS, cell_of(&honest, slot),
                    (cell_of(&honest, slot) + 1) % CELLS];
        let free = (0..CELLS).find(|cell| !used.contains(cell)).unwrap();
        let other = salt(free);
        let future = MutableItem::new(&signer, &encode(&[(cheat, u32::MAX)]), 1, Some(&other));
        let _ = dht.put_mutable(future, None).await;
        let found = discover(&dht, me, now).await;
        assert!(found.contains(&honest) && !found.contains(&cheat));
    }
}
