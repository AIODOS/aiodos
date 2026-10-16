//! Le compagnon en marche : identité, point de terminaison, API locale, et tâches de
//! fond (connexions entrantes, ménage des doublons, changements de réseau). Utilisé par
//! `main.rs` et par les tests, qui lancent deux compagnons dans le même processus.

use std::{
    net::SocketAddr,
    sync::{Arc, atomic::AtomicU64},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use iroh::{Endpoint, EndpointId};
use tokio::{net::TcpListener, task::JoinSet};
use tokio_util::sync::CancellationToken;
use tracing::info;

use crate::{
    allowlist::Allowlist,
    config::Config,
    control::{self, Ctx},
    endpoint, identity, inbound, netmon,
    pool::Pool,
};

/// Délai laissé aux autres machines pour prendre acte de la fermeture des connexions.
const CLOSE_TIMEOUT: Duration = Duration::from_secs(3);

pub struct Running {
    /// Clé publique de cette machine (son adresse sur Internet).
    pub id: EndpointId,
    /// Adresse réelle de l'API locale (utile avec le port 0).
    pub listen: SocketAddr,
    /// Sockets UDP réellement ouvertes par Iroh.
    pub bound: Vec<SocketAddr>,
    /// La clé vient d'être créée (premier lancement).
    pub created: bool,
    pub endpoint: Endpoint,
    pool: Arc<Pool>,
    tasks: JoinSet<()>,
}

pub async fn start(config: Config) -> Result<Running> {
    if !config.listen.ip().is_loopback() {
        bail!("l'API de contrôle n'écoute que sur la boucle locale");
    }
    std::fs::create_dir_all(&config.data_dir)
        .with_context(|| format!("dossier de données {}", config.data_dir.display()))?;
    let (secret, created) = identity::load_or_create(&config.data_dir)?;
    let allowlist = Allowlist::default();
    let endpoint = endpoint::bind(&config, secret, allowlist.clone()).await?;
    let listener = TcpListener::bind(config.listen)
        .await
        .with_context(|| format!("API locale : port indisponible ({})", config.listen))?;
    let listen = listener.local_addr()?;
    let shutdown = CancellationToken::new();
    let pool = Pool::new(
        endpoint.clone(),
        allowlist,
        config.forward,
        config.dial_timeout,
        shutdown.clone(),
    );
    let network_changes = Arc::new(AtomicU64::new(0));
    info!(
        "Compagnon {} : clé {} ({}), relais {}, annuaire {}, API locale {listen}, vers {}",
        env!("CARGO_PKG_VERSION"),
        endpoint.id().fmt_short(),
        if created { "nouvelle" } else { "existante" },
        config.relays.label(),
        match config.directory.labels() {
            labels if labels.is_empty() => "aucun".to_string(),
            labels => labels.join(" + "),
        },
        config.forward,
    );
    let ctx = Arc::new(Ctx {
        pool: pool.clone(),
        config,
        started: Instant::now(),
        network_changes: network_changes.clone(),
    });
    let mut tasks = JoinSet::new();
    tasks.spawn(inbound::accept_loop(pool.clone()));
    tasks.spawn(control::serve(listener, ctx));
    tasks.spawn(netmon::watch(endpoint.clone(), network_changes, shutdown.clone()));
    pool.spawn_sweeper();
    Ok(Running {
        id: endpoint.id(),
        listen,
        bound: endpoint.bound_sockets(),
        created,
        endpoint,
        pool,
        tasks,
    })
}

impl Running {
    /// Jeton d'arrêt : annulé par l'API locale (`POST /net/v1/shutdown`), Ctrl+C ou la
    /// fermeture de l'entrée standard (cf. main.rs).
    pub fn shutdown_token(&self) -> CancellationToken {
        self.pool.shutdown.clone()
    }

    /// Arrêt propre : les autres machines sont prévenues que les connexions se ferment.
    pub async fn stop(mut self) {
        self.pool.shutdown.cancel();
        self.pool.close_all();
        let _ = tokio::time::timeout(CLOSE_TIMEOUT, self.endpoint.close()).await;
        self.tasks.abort_all();
        while self.tasks.join_next().await.is_some() {}
        info!("Compagnon arrêté");
    }
}
