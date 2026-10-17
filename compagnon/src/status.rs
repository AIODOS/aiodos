//! État du compagnon pour AIODOS : sa clé publique, ses relais, et pour chaque machine
//! autorisée, la connexion retenue (directe ou relayée, latence, ouverte par qui).
//! Jamais la clé secrète ni le jeton.

use std::{
    sync::atomic::Ordering,
    time::{SystemTime, UNIX_EPOCH},
};

use iroh::{TransportAddr, Watcher};
use iroh_tickets::endpoint::EndpointTicket;
use serde_json::{Value, json};

use crate::{
    ALPN,
    control::Ctx,
    pool::{self, PeerView},
};

pub fn snapshot(ctx: &Ctx) -> Value {
    let endpoint = &ctx.pool.endpoint;
    let addr = endpoint.addr();
    let home_relays: Vec<Value> = endpoint
        .home_relay_status()
        .get()
        .into_iter()
        .map(|relay| {
            json!({
                "url": relay.url().to_string(),
                "connected": relay.is_connected(),
                "error": relay.last_error().map(|e| e.to_string()),
                // Le relais refuse cette clé (absente de sa liste blanche, jeton manquant) :
                // Iroh réessaie, mais en vain tant que son hébergeur ne l'a pas ajoutée.
                "denied": relay.auth_denied_reason(),
            })
        })
        .collect();
    let (refused, last_refused) = ctx.pool.allowlist.refused();
    json!({
        "id": endpoint.id().to_string(),
        "version": env!("CARGO_PKG_VERSION"),
        "alpn": String::from_utf8_lossy(ALPN),
        "relay_mode": ctx.config.relays.label(),
        "relay_only": ctx.config.relay_only,
        "home_relays": home_relays,
        "relay_urls": addr.relay_urls().map(|u| u.to_string()).collect::<Vec<_>>(),
        "addrs": addr.ip_addrs().map(|a| a.to_string()).collect::<Vec<_>>(),
        "bound": endpoint.bound_sockets().iter().map(|a| a.to_string()).collect::<Vec<_>>(),
        "directory": ctx.config.directory.labels(),
        "portmap": ctx.config.portmap,
        "allowed": ctx.pool.allowlist.len(),
        "refused": {
            "count": refused,
            "last": last_refused.map(|(id, at)| json!({
                "id": id.fmt_short().to_string(),
                "at": unix(at),
            })),
        },
        "network_changes": ctx.network_changes.load(Ordering::SeqCst),
        "uptime_s": ctx.started.elapsed().as_secs(),
        "peers": ctx.pool.snapshot().into_iter().map(|v| peer(ctx, v)).collect::<Vec<_>>(),
    })
}

fn peer(ctx: &Ctx, view: PeerView) -> Value {
    let current = view.current.as_ref();
    let path = current.and_then(|t| pool::path_kind(&t.conn));
    json!({
        "id": view.id.to_string(),
        "allowed": ctx.pool.allowlist.contains(&view.id),
        "connected": current.is_some(),
        "path": path.as_ref().map(|p| p.0),
        "remote": path.as_ref().map(|p| p.1.clone()),
        "rtt_ms": path.as_ref().map(|p| (p.2.as_secs_f64() * 10_000.0).round() / 10.0),
        "direction": current.map(|t| if t.outgoing { "out" } else { "in" }),
        "since": current.map(|t| unix(t.opened)),
        "connections": view.open,
        "dials": view.stats.dials,
        "dial_failures": view.stats.dial_failures,
        "requests_out": view.stats.requests_out,
        "requests_in": view.stats.requests_in,
        "last_error": view.stats.last_error.as_ref().map(|(message, at)| json!({
            "message": message,
            "at": unix(*at),
        })),
    })
}

/// Ticket à donner à une autre machine : la clé, le relais et les adresses directes de
/// celle-ci (de quoi la joindre sans annuaire). Il ne contient aucun secret ; à lui seul,
/// il ne fait pas entrer dans la liste blanche.
pub fn ticket(ctx: &Ctx) -> Value {
    let addr = ctx.pool.endpoint.addr();
    let relays: Vec<String> = addr
        .addrs
        .iter()
        .filter_map(|a| match a {
            TransportAddr::Relay(url) => Some(url.to_string()),
            _ => None,
        })
        .collect();
    let addrs: Vec<String> = addr.ip_addrs().map(|a| a.to_string()).collect();
    json!({
        "ticket": EndpointTicket::new(addr.clone()).to_string(),
        "id": addr.id.to_string(),
        "relays": relays,
        "addrs": addrs,
    })
}

fn unix(at: SystemTime) -> f64 {
    at.duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}
