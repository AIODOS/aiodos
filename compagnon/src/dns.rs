//! Résolution DNS sous Android (Termux).
//!
//! Sous Android, Iroh lit les serveurs DNS du système par l'environnement Java (JNI),
//! qui n'existe pas pour un binaire lancé dans Termux : la lecture échoue (et ferait
//! même s'arrêter le programme s'il était compilé avec `panic = "abort"`). On lui donne
//! donc les serveurs de `$PREFIX/etc/resolv.conf` (paquet `resolv-conf` de Termux), avec
//! les résolveurs publics d'Iroh en secours. La recette Termux de dumbpipe arrive au
//! même résultat en modifiant les bibliothèques pour qu'elles lisent ce fichier ; ici,
//! rien à modifier.

use std::net::IpAddr;

/// Serveurs listés par un fichier resolv.conf (`nameserver <ip>`).
#[cfg_attr(not(target_os = "android"), allow(dead_code))]
pub(crate) fn nameservers(text: &str) -> Vec<IpAddr> {
    text.lines()
        .filter_map(|line| {
            let mut words = line.split_whitespace();
            match (words.next(), words.next()) {
                (Some("nameserver"), Some(ip)) => ip.split('%').next()?.parse().ok(),
                _ => None,
            }
        })
        .collect()
}

#[cfg(target_os = "android")]
pub(crate) fn resolver() -> Option<iroh::dns::DnsResolver> {
    use iroh::dns::{DnsResolver, NameserverConfig};

    let prefix = std::env::var("PREFIX").unwrap_or_else(|_| "/data/data/com.termux/files/usr".into());
    let mut builder = DnsResolver::builder();
    for path in [format!("{prefix}/etc/resolv.conf"), "/etc/resolv.conf".to_string()] {
        if let Ok(text) = std::fs::read_to_string(&path) {
            let servers = nameservers(&text);
            tracing::debug!("DNS : {} serveur(s) lu(s) dans {path}", servers.len());
            for ip in servers {
                builder = builder.add_nameserver_config(NameserverConfig::udp(ip));
            }
            break;
        }
    }
    Some(builder.build())
}

/// Ailleurs, Iroh lit lui-même la configuration du système.
#[cfg(not(target_os = "android"))]
pub(crate) fn resolver() -> Option<iroh::dns::DnsResolver> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_nameservers_of_a_resolv_conf() {
        let text = "# Termux\nnameserver 8.8.8.8\nnameserver 2001:4860:4860::8888\n\
                    nameserver fe80::1%wlan0\nsearch lan\noptions ndots:1\n";
        let servers = nameservers(text);
        assert_eq!(servers.len(), 3);
        assert_eq!(servers[0], "8.8.8.8".parse::<IpAddr>().unwrap());
    }
}
