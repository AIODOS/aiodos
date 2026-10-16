//! Changements de réseau (Wi-Fi ↔ 4G, câble débranché, partage de connexion…).
//!
//! Iroh suit lui-même les interfaces réseau sur la plupart des systèmes, mais pas
//! toujours sous Android, où un programme natif ne reçoit pas ces notifications. On
//! regarde donc toutes les 5 s l'adresse locale de la route par défaut, en IPv4 et en
//! IPv6 (un `connect` UDP ne fait que choisir la route : aucun paquet ne part). Si elle
//! change, Iroh est prévenu : il refait son bilan réseau, se réinscrit auprès de son
//! relais et cherche de nouveaux chemins pour les connexions en cours, qui survivent
//! au changement sans que les requêtes d'AIODOS aient à le savoir.

use std::{
    net::{IpAddr, SocketAddr, UdpSocket},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use iroh::Endpoint;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info};

const CHECK_EVERY: Duration = Duration::from_secs(5);

/// Adresses locales des routes par défaut : leur changement signale un autre réseau.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Routes {
    pub v4: Option<IpAddr>,
    pub v6: Option<IpAddr>,
}

pub fn current_routes() -> Routes {
    Routes {
        v4: local_for("0.0.0.0:0", "192.0.2.1:9"),
        v6: local_for("[::]:0", "[2001:db8::1]:9"),
    }
}

/// Adresse locale qu'utiliserait un paquet vers `target` (adresses réservées à la
/// documentation : rien n'est envoyé).
fn local_for(bind: &str, target: &str) -> Option<IpAddr> {
    let socket = UdpSocket::bind(bind).ok()?;
    let target: SocketAddr = target.parse().ok()?;
    socket.connect(target).ok()?;
    let ip = socket.local_addr().ok()?.ip();
    (!ip.is_unspecified()).then_some(ip)
}

pub async fn watch(endpoint: Endpoint, changes: Arc<AtomicU64>, shutdown: CancellationToken) {
    let mut last = current_routes();
    loop {
        tokio::select! {
            _ = shutdown.cancelled() => break,
            _ = tokio::time::sleep(CHECK_EVERY) => {}
        }
        let now = match tokio::task::spawn_blocking(current_routes).await {
            Ok(routes) => routes,
            Err(_) => continue,
        };
        if now != last {
            info!("Changement de réseau détecté : nouveaux chemins recherchés");
            debug!("routes : {last:?} → {now:?}");
            changes.fetch_add(1, Ordering::SeqCst);
            endpoint.network_change().await;
            last = now;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_are_stable_without_network_change() {
        assert_eq!(current_routes(), current_routes());
    }
}
