//! Réglages du compagnon : ligne de commande et valeurs par défaut.
//!
//! Par défaut : relais publics de n0, annuaire DNS de n0 puis DHT Mainline, ouverture de
//! port automatique (décisions de l'étape 2.1, Q16). Tout se remplace : relais
//! auto-hébergés, aucun relais, aucun annuaire.

use std::{net::SocketAddr, path::PathBuf, time::Duration};

use anyhow::{Result, bail};
use clap::Parser;
use iroh::RelayUrl;

/// Relais utilisés pour joindre une machine qu'on ne peut pas joindre en direct.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Relays {
    /// Relais publics de n0 (gratuits, réservés au développement et aux usages amateurs).
    N0,
    /// Relais choisis : auto-hébergés, ceux d'un membre du cercle… (avec ou sans ceux de n0).
    Custom(Vec<RelayUrl>),
    /// Aucun relais : connexion directe seulement (IPv6, port ouvert, même réseau).
    Disabled,
}

impl Relays {
    /// « n0 » (défaut), « none », ou des adresses https://… ; « n0 » plus des adresses
    /// ajoute ces relais à ceux de n0.
    pub fn parse(values: &[String]) -> Result<Self> {
        let values: Vec<&str> = values
            .iter()
            .flat_map(|v| v.split(','))
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .collect();
        if values.is_empty() {
            return Ok(Relays::N0);
        }
        if values.contains(&"none") {
            if values.len() > 1 {
                bail!("« --relay none » ne se combine pas avec d'autres relais");
            }
            return Ok(Relays::Disabled);
        }
        let with_n0 = values.contains(&"n0");
        let mut urls = Vec::new();
        for value in values.iter().filter(|v| **v != "n0") {
            match value.parse::<RelayUrl>() {
                Ok(url) => urls.push(url),
                Err(err) => bail!("adresse de relais illisible « {value} » : {err}"),
            }
        }
        if urls.is_empty() {
            return Ok(Relays::N0);
        }
        if with_n0 {
            let n0 = iroh::RelayMode::Default.relay_map();
            urls.extend(n0.urls::<Vec<_>>());
        }
        Ok(Relays::Custom(urls))
    }

    /// Nom court pour l'état et le journal.
    pub fn label(&self) -> &'static str {
        match self {
            Relays::N0 => "n0",
            Relays::Custom(_) => "custom",
            Relays::Disabled => "none",
        }
    }
}

/// Annuaires où publier et chercher l'adresse d'une clé. Seule l'adresse du relais de
/// la machine y est publiée, jamais ses adresses IP (comportement par défaut d'Iroh).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Directory {
    /// Serveur DNS de n0 (dns.iroh.link).
    pub n0_dns: bool,
    /// DHT Mainline de BitTorrent (pkarr), sans serveur.
    pub dht: bool,
}

impl Directory {
    pub const NONE: Directory = Directory { n0_dns: false, dht: false };

    /// « n0-dns,dht » (défaut), « n0-dns », « dht » ou « none ».
    pub fn parse(text: &str) -> Result<Self> {
        let mut directory = Directory::NONE;
        for item in text.split(',').map(str::trim).filter(|s| !s.is_empty()) {
            match item {
                "n0-dns" | "dns" => directory.n0_dns = true,
                "dht" | "mainline" => directory.dht = true,
                "none" => {}
                other => bail!("annuaire inconnu « {other} » (attendu : n0-dns, dht ou none)"),
            }
        }
        Ok(directory)
    }

    pub fn labels(&self) -> Vec<&'static str> {
        let mut labels = Vec::new();
        if self.n0_dns {
            labels.push("n0-dns");
        }
        if self.dht {
            labels.push("dht");
        }
        labels
    }
}

/// Réglages complets d'un compagnon.
#[derive(Debug, Clone)]
pub struct Config {
    /// Dossier de données d'AIODOS (la clé de la machine y est gardée).
    pub data_dir: PathBuf,
    /// API de contrôle et mandataire, sur 127.0.0.1 (port 0 : libre, annoncé au démarrage).
    pub listen: SocketAddr,
    /// Serveur réseau local d'AIODOS (`p2p_app`) qui reçoit les requêtes des autres machines.
    pub forward: SocketAddr,
    pub relays: Relays,
    pub directory: Directory,
    /// Sockets UDP d'Iroh ; `None` : toutes les interfaces, IPv4 et IPv6, port libre.
    pub bind: Option<SocketAddr>,
    /// Ouverture de port automatique sur la box (UPnP, NAT-PMP, PCP).
    pub portmap: bool,
    /// Jeton exigé sur l'API locale (contrôle et mandataire).
    pub token: String,
    /// Délai pour établir une connexion avec une machine.
    pub dial_timeout: Duration,
    /// Silence toléré sur une connexion avant de la tenir pour morte : large, pour les
    /// liens mobiles qui décrochent quelques secondes (Iroh envoie un battement toutes
    /// les 5 s, la connexion ne s'éteint pas d'elle-même).
    pub idle_timeout: Duration,
}

impl Config {
    /// Réglages d'essai : tout en local, sans relais ni annuaire ni box (tests).
    pub fn local(data_dir: PathBuf, forward: SocketAddr, token: &str) -> Self {
        Config {
            data_dir,
            listen: SocketAddr::from(([127, 0, 0, 1], 0)),
            forward,
            relays: Relays::Disabled,
            directory: Directory::NONE,
            bind: Some(SocketAddr::from(([127, 0, 0, 1], 0))),
            portmap: false,
            token: token.to_string(),
            dial_timeout: Duration::from_secs(10),
            idle_timeout: Duration::from_secs(60),
        }
    }
}

/// Ligne de commande.
#[derive(Parser, Debug)]
#[command(
    name = "aiodos-net",
    version,
    about = "Compagnon réseau d'AIODOS : relie les machines appairées par Internet (Iroh).",
    after_help = "Le jeton de l'API locale se donne par la variable AIODOS_NET_TOKEN ; \
                  sans elle, il est tiré au hasard et écrit dans <data-dir>/aiodos-net.token."
)]
pub struct Args {
    /// Dossier de données d'AIODOS (la clé de cette machine y est gardée)
    #[arg(long, value_name = "DOSSIER")]
    pub data_dir: PathBuf,

    /// API de contrôle et mandataire : 127.0.0.1 seulement (port 0 : libre)
    #[arg(long, default_value = "127.0.0.1:47800", value_name = "ADRESSE")]
    pub listen: SocketAddr,

    /// Serveur réseau local d'AIODOS qui reçoit les requêtes des autres machines
    #[arg(long, default_value = "127.0.0.1:47701", value_name = "ADRESSE")]
    pub forward: SocketAddr,

    /// Relais : « n0 » (publics, défaut), « none » (aucun) ou https://… (répétable)
    #[arg(long = "relay", value_name = "n0|none|URL")]
    pub relays: Vec<String>,

    /// Annuaires, séparés par des virgules : « n0-dns », « dht », ou « none »
    #[arg(long, default_value = "n0-dns,dht", value_name = "LISTE")]
    pub directory: String,

    /// Adresse des sockets UDP d'Iroh (défaut : toutes les interfaces, port libre)
    #[arg(long, value_name = "ADRESSE")]
    pub bind: Option<SocketAddr>,

    /// Pas d'ouverture de port automatique sur la box (UPnP, NAT-PMP, PCP)
    #[arg(long)]
    pub no_portmap: bool,

    /// Délai pour joindre une machine, en secondes (sous les 30 s qu'AIODOS attend une
    /// réponse par Internet, pour qu'il reçoive une erreur lisible plutôt qu'un silence)
    #[arg(long, default_value_t = 20, value_name = "S")]
    pub dial_timeout: u64,

    /// Lancé par AIODOS : s'arrête quand son entrée standard se ferme ; journal sans heure
    /// ni couleurs (AIODOS ajoute les siennes)
    #[arg(long)]
    pub supervised: bool,
}

impl Args {
    pub fn into_config(self, token: String) -> Result<Config> {
        if !self.listen.ip().is_loopback() {
            bail!("l'API de contrôle n'écoute que sur la boucle locale (127.0.0.1 ou ::1)");
        }
        if self.dial_timeout == 0 {
            bail!("--dial-timeout doit être supérieur à 0");
        }
        Ok(Config {
            data_dir: self.data_dir,
            listen: self.listen,
            forward: self.forward,
            relays: Relays::parse(&self.relays)?,
            directory: Directory::parse(&self.directory)?,
            bind: self.bind,
            portmap: !self.no_portmap,
            token,
            dial_timeout: Duration::from_secs(self.dial_timeout),
            idle_timeout: Duration::from_secs(60),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    #[test]
    fn relays_default_to_n0() {
        assert_eq!(Relays::parse(&[]).unwrap(), Relays::N0);
        assert_eq!(Relays::parse(&strings(&["n0"])).unwrap(), Relays::N0);
    }

    #[test]
    fn relays_can_be_disabled_or_custom() {
        assert_eq!(Relays::parse(&strings(&["none"])).unwrap(), Relays::Disabled);
        assert!(Relays::parse(&strings(&["none", "n0"])).is_err());
        let custom = Relays::parse(&strings(&["https://relay.example.org"])).unwrap();
        assert!(matches!(custom, Relays::Custom(ref urls) if urls.len() == 1));
        let with_n0 = Relays::parse(&strings(&["n0,https://relay.example.org"])).unwrap();
        assert!(matches!(with_n0, Relays::Custom(ref urls) if urls.len() > 1));
        assert!(Relays::parse(&strings(&["pas une adresse"])).is_err());
    }

    #[test]
    fn directory_parsing() {
        assert_eq!(Directory::parse("n0-dns,dht").unwrap(), Directory { n0_dns: true, dht: true });
        assert_eq!(Directory::parse("dht").unwrap(), Directory { n0_dns: false, dht: true });
        assert_eq!(Directory::parse("none").unwrap(), Directory::NONE);
        assert!(Directory::parse("annuaire").is_err());
    }

    #[test]
    fn control_api_stays_on_loopback() {
        let args = Args::parse_from(["aiodos-net", "--data-dir", "d", "--listen", "0.0.0.0:47800"]);
        assert!(args.into_config("t".into()).is_err());
        let args = Args::parse_from(["aiodos-net", "--data-dir", "d"]);
        let config = args.into_config("t".into()).unwrap();
        assert!(config.listen.ip().is_loopback());
        assert_eq!(config.relays, Relays::N0);
        assert_eq!(config.directory, Directory { n0_dns: true, dht: true });
        assert!(config.portmap);
    }
}
