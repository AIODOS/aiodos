//! aiodos-net : compagnon réseau d'AIODOS. Voir compagnon/README.md.
//!
//! Au démarrage, une ligne JSON sur la sortie standard annonce que le compagnon est
//! prêt : `{"event": "ready", "id": …, "listen": …}` (AIODOS la lit pour connaître la
//! clé et le port de l'API locale). Le journal, lui, va sur la sortie d'erreur.

use std::{
    io::{IsTerminal, Read},
    path::Path,
    process::ExitCode,
};

use aiodos_net::{config::Args, start};
use anyhow::{Context, Result};
use clap::Parser;
use serde_json::json;
use tokio_util::sync::CancellationToken;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

/// Variable d'environnement qui porte le jeton de l'API locale (jamais sur la ligne de
/// commande, visible des autres programmes).
const TOKEN_ENV: &str = "AIODOS_NET_TOKEN";
const TOKEN_FILE: &str = "aiodos-net.token";

/// Commit d'AIODOS dont ce programme est construit. L'outil de publication
/// (tools/publish.py) le donne à la construction, puis le retrouve dans le programme :
/// c'est ainsi qu'il vérifie qu'un compagnon livré correspond à la version publiée.
/// Absent d'une construction à la main.
const COMMIT: Option<&str> = option_env!("AIODOS_NET_COMMIT");

#[tokio::main]
async fn main() -> ExitCode {
    let args = Args::parse();
    init_logging(args.supervised);
    match run(args).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            error!("{err:#}");
            ExitCode::FAILURE
        }
    }
}

async fn run(args: Args) -> Result<()> {
    let supervised = args.supervised;
    let token = token(&args.data_dir)?;
    let config = args.into_config(token)?;
    let running = start(config).await?;
    let ready = json!({
        "event": "ready",
        "id": running.id.to_string(),
        "listen": running.listen.to_string(),
        "bound": running.bound.iter().map(|a| a.to_string()).collect::<Vec<_>>(),
        "alpn": "aiodos/2",
        "version": env!("CARGO_PKG_VERSION"),
        "commit": COMMIT,
        "created": running.created,
        "features": aiodos_net::features(),
    });
    println!("{ready}");
    let stop = running.shutdown_token();
    if supervised {
        watch_stdin(stop.clone());
    }
    tokio::select! {
        _ = interrupted() => info!("Signal d'arrêt reçu"),
        _ = stop.cancelled() => {}
    }
    running.stop().await;
    Ok(())
}

/// Ctrl+C, ou SIGTERM sous Unix (`kill` dans Termux). Un signal qu'on ne peut pas
/// écouter (pas de console) ne déclenche rien.
async fn interrupted() {
    let ctrl_c = async {
        if tokio::signal::ctrl_c().await.is_err() {
            std::future::pending::<()>().await;
        }
    };
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        match signal(SignalKind::terminate()) {
            Ok(mut term) => {
                tokio::select! {
                    _ = ctrl_c => {}
                    _ = term.recv() => {}
                }
            }
            Err(_) => ctrl_c.await,
        }
    }
    #[cfg(not(unix))]
    ctrl_c.await;
}

/// Jeton de l'API locale : donné par AIODOS (variable d'environnement), sinon tiré au
/// hasard et écrit dans le dossier de données (lancement à la main).
fn token(data_dir: &Path) -> Result<String> {
    if let Ok(token) = std::env::var(TOKEN_ENV)
        && !token.trim().is_empty()
    {
        return Ok(token.trim().to_string());
    }
    let mut bytes = [0u8; 24];
    getrandom::fill(&mut bytes).map_err(|e| anyhow::anyhow!("tirage du jeton : {e}"))?;
    let token: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    std::fs::create_dir_all(data_dir)?;
    let path = data_dir.join(TOKEN_FILE);
    std::fs::write(&path, &token).with_context(|| format!("écriture de {}", path.display()))?;
    info!("Jeton de l'API locale écrit dans {}", path.display());
    Ok(token)
}

/// Lancé par AIODOS : quand AIODOS s'arrête, même brutalement, son extrémité du tuyau se
/// ferme et le compagnon s'arrête avec lui (un fil ordinaire, qui ne retient pas la fin
/// du programme). L'arrêt est demandé avant d'écrire au journal : si AIODOS a été tué,
/// le journal (sa sortie d'erreur) ne mène plus nulle part.
fn watch_stdin(stop: CancellationToken) {
    std::thread::spawn(move || {
        let mut stdin = std::io::stdin();
        let mut buf = [0u8; 256];
        loop {
            match stdin.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
        stop.cancel();
        info!("AIODOS est parti : arrêt");
    });
}

fn init_logging(supervised: bool) {
    let filter = EnvFilter::try_from_env("AIODOS_NET_LOG")
        .unwrap_or_else(|_| EnvFilter::new("aiodos_net=info,iroh=warn,warn"));
    // Journal perdu (AIODOS parti, tuyau fermé) : on se tait plutôt que de s'arrêter net.
    let builder = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .log_internal_errors(false)
        .with_target(false)
        .with_ansi(!supervised && std::io::stderr().is_terminal());
    if supervised {
        builder.without_time().init();
    } else {
        builder.init();
    }
}
