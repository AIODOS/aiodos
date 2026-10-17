//! Le point de terminaison Iroh de la machine : sa clé, le protocole `aiodos/2`, les
//! relais et annuaires choisis, l'ouverture de port automatique, et la liste blanche
//! branchée sur la poignée de main.
//!
//! « Relais seulement » (`--relay-only`, pour les essais) retire à Iroh ses transports IP :
//! sans socket UDP, il n'a ni chemin direct à essayer ni adresse directe à annoncer, et
//! l'autre machine ne peut pas non plus le joindre en direct. C'est ainsi qu'Iroh 1.3.0
//! coupe le direct dans ses propres essais (`endpoint_two_relay_only_no_ip`). Un
//! sélecteur de chemin maison (`path_selector`, API instable) ne choisirait que le chemin
//! d'envoi de cette machine-ci : les box seraient percées quand même, et l'autre machine
//! passerait en direct.

use anyhow::{Context, Result, bail};
use iroh::{
    Endpoint, RelayMode, SecretKey,
    address_lookup::{DnsAddressLookup, PkarrPublisher, PkarrResolver},
    endpoint::{IdleTimeout, PortmapperConfig, QuicTransportConfig, presets},
};
use iroh_mainline_address_lookup::DhtAddressLookup;

use crate::{
    ALPN,
    allowlist::{Allowlist, Gate},
    config::{Config, Relays},
    dns,
};

pub async fn bind(config: &Config, secret: SecretKey, allowlist: Allowlist) -> Result<Endpoint> {
    let idle: IdleTimeout = config
        .idle_timeout
        .try_into()
        .context("délai d'inactivité trop grand")?;
    let transport = QuicTransportConfig::builder().max_idle_timeout(Some(idle)).build();
    let relay_mode = match &config.relays {
        Relays::N0 => RelayMode::Default,
        Relays::Custom(urls) => RelayMode::custom(urls.iter().cloned()),
        Relays::Disabled if config.relay_only => {
            bail!("relais seulement, mais aucun relais : aucune machine ne serait joignable")
        }
        Relays::Disabled => RelayMode::Disabled,
    };
    let mut builder = Endpoint::builder(presets::Minimal)
        .secret_key(secret)
        .alpns(vec![ALPN.to_vec()])
        .relay_mode(relay_mode)
        .transport_config(transport)
        .portmapper_config(if config.portmap && !config.relay_only {
            PortmapperConfig::default()
        } else {
            PortmapperConfig::Disabled
        })
        .hooks(Gate::new(allowlist));
    // Annuaire : seule l'adresse du relais de la machine est publiée (pas ses IP).
    if config.directory.n0_dns {
        builder = builder
            .address_lookup(PkarrPublisher::n0_dns())
            .address_lookup(PkarrResolver::n0_dns())
            .address_lookup(DnsAddressLookup::n0_dns());
    }
    if config.directory.dht {
        builder = builder.address_lookup(DhtAddressLookup::builder());
    }
    if config.relay_only {
        builder = builder.clear_ip_transports();
    } else if let Some(bind) = config.bind {
        builder = builder
            .clear_ip_transports()
            .bind_addr(bind)
            .with_context(|| format!("adresse d'écoute UDP refusée : {bind}"))?;
    }
    if let Some(resolver) = dns::resolver() {
        builder = builder.dns_resolver(resolver);
    }
    builder.bind().await.context("ouverture du point de terminaison Iroh")
}
