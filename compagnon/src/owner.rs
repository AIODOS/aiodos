//! Quel programme est au bout d'une connexion locale, et qui est connecté à un port ? Sur
//! 127.0.0.1, n'importe quel programme de la machine (un autre compte du PC, une autre appli
//! du téléphone) peut se connecter :
//! - un port de tunnel (cf. tunnel.rs) ne doit relayer que le llama-server lancé par
//!   AIODOS, sans quoi ce programme parlerait au serveur RPC d'une autre machine, qui n'a
//!   aucune défense ;
//! - le serveur RPC prêté (cf. hold.rs) ne doit servir que le compagnon.
//!
//! - **Windows** : table des connexions TCP du système, avec l'état et le programme de
//!   chacune (`GetExtendedTcpTable` d'iphlpapi.dll, déclarée ici : pas de bibliothèque de plus) ;
//! - **Linux** : `/proc/net/tcp` donne l'état et l'inode du socket, puis on cherche l'inode
//!   parmi les fichiers ouverts du programme attendu (`/proc/<pid>/fd`).
//!
//! Une seule lecture de la table sert à contrôler toutes les connexions en attente : sous
//! une avalanche de connexions d'un intrus, le contrôle de celle du programme attendu ne
//! prend pas de retard (cf. tunnel.rs).
//!
//! Ailleurs, ou si le système le refuse (Android récent ferme `/proc/net` aux applis),
//! `available()` est faux : AIODOS n'ouvre alors aucun tunnel gardé, et n'emprunte ni ne
//! prête.

use std::{
    collections::HashMap,
    io,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    sync::OnceLock,
};

use tracing::{debug, info};

/// Une ligne de la table des connexions TCP du système.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Row {
    pub local: SocketAddr,
    pub remote: SocketAddr,
    /// Établie, ou fermée par ce bout-ci sans que l'autre l'ait encore lue (`FIN_WAIT`) :
    /// elle est dans la file du serveur ou servie, et des octets peuvent encore arriver à
    /// l'autre bout. N'en sont pas : une tentative pas encore acceptée par le système
    /// (`SYN_SENT`, file du serveur pleine : elle ne sera servie qu'après y être entrée, et
    /// vue alors), ni les connexions que l'autre bout a déjà fermées (`CLOSE_WAIT`,
    /// `LAST_ACK`, `TIME_WAIT`…).
    pub live: bool,
    /// Programme qui la détient (Windows).
    pub pid: Option<u32>,
    /// Inode du socket (Linux ; 0 : plus détenu par aucun programme).
    pub inode: Option<u64>,
}

/// La connexion locale `client` → `server` (vue du serveur : `client` est l'adresse de
/// l'appelant, `server` celle du port d'écoute) a-t-elle été ouverte par le programme `pid` ?
/// Une connexion introuvable dans la table du système compte pour « non ».
pub fn connected_by(pid: u32, client: SocketAddr, server: SocketAddr) -> io::Result<bool> {
    Ok(connected_by_all(pid, &[client], server)?.first().copied().unwrap_or(false))
}

/// `connected_by` pour plusieurs connexions vers `server`, d'une seule lecture de la table.
pub fn connected_by_all(pid: u32, clients: &[SocketAddr], server: SocketAddr) -> io::Result<Vec<bool>> {
    let rows = imp::rows()?;
    imp::owned_by(pid, &rows, clients, server)
}

/// Adresses (côté appelant) des connexions vivantes vers `server`, de n'importe quel
/// programme de la machine (cf. `Row::live`).
pub fn clients_of(server: SocketAddr) -> io::Result<Vec<SocketAddr>> {
    Ok(live_clients(&imp::rows()?, server))
}

fn live_clients(rows: &[Row], server: SocketAddr) -> Vec<SocketAddr> {
    rows.iter().filter(|r| r.live && r.remote == server).map(|r| r.local).collect()
}

/// Ligne vivante de chaque connexion `client` → `server` demandée.
fn rows_for(rows: &[Row], server: SocketAddr) -> HashMap<SocketAddr, &Row> {
    rows.iter().filter(|r| r.live && r.remote == server).map(|r| (r.local, r)).collect()
}

/// Le contrôle marche-t-il ici ? Essayé une fois, sur une connexion de ce programme à
/// lui-même : sous Android récent ou dans un conteneur, la table peut être fermée.
pub fn available() -> bool {
    static AVAILABLE: OnceLock<bool> = OnceLock::new();
    *AVAILABLE.get_or_init(|| match self_test() {
        Ok(true) => true,
        Ok(false) => {
            info!("Tunnels gardés impossibles : ce système ne dit pas quel programme se connecte");
            false
        }
        Err(err) => {
            info!("Tunnels gardés impossibles sur ce système : {err}");
            false
        }
    })
}

fn self_test() -> io::Result<bool> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    let server = listener.local_addr()?;
    let _client = std::net::TcpStream::connect(server)?;
    let (_accepted, client) = listener.accept()?;
    let found = connected_by(std::process::id(), client, server)? && clients_of(server)?.contains(&client);
    debug!("contrôle du programme d'une connexion locale : {}", if found { "possible" } else { "impossible" });
    Ok(found)
}

/// Une adresse lue dans `/proc/net/tcp` ou `/proc/net/tcp6` : « 0100007F:1F90 ».
/// L'adresse est écrite mot de 32 bits par mot de 32 bits, dans l'ordre de la machine ;
/// le port en hexadécimal. Une adresse IPv4 vue par un socket IPv6 (::ffff:a.b.c.d) est
/// rendue en IPv4.
#[cfg_attr(not(any(target_os = "linux", target_os = "android")), allow(dead_code))]
fn parse_proc_addr(text: &str) -> Option<SocketAddr> {
    let (ip, port) = text.split_once(':')?;
    let port = u16::from_str_radix(port, 16).ok()?;
    let word = |hex: &str| u32::from_str_radix(hex, 16).ok().map(u32::to_ne_bytes);
    let ip = match ip.len() {
        8 => IpAddr::V4(Ipv4Addr::from(word(ip)?)),
        32 => {
            let mut bytes = [0u8; 16];
            for (i, chunk) in bytes.chunks_mut(4).enumerate() {
                chunk.copy_from_slice(&word(ip.get(i * 8..i * 8 + 8)?)?);
            }
            unmapped(Ipv6Addr::from(bytes))
        }
        _ => return None,
    };
    Some(SocketAddr::new(ip, port))
}

fn unmapped(ip: Ipv6Addr) -> IpAddr {
    ip.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(IpAddr::V6(ip))
}

/// Lignes du texte de `/proc/net/tcp` : adresses, état (01 établie, 04 et 05 fermée de ce
/// côté seulement : vivantes), inode.
#[cfg_attr(not(any(target_os = "linux", target_os = "android")), allow(dead_code))]
fn parse_proc_table(table: &str) -> Vec<Row> {
    table
        .lines()
        .skip(1)
        .filter_map(|line| {
            let fields: Vec<&str> = line.split_whitespace().collect();
            if fields.len() < 10 {
                return None;
            }
            let state = u8::from_str_radix(fields[3], 16).ok()?;
            Some(Row {
                local: parse_proc_addr(fields[1])?,
                remote: parse_proc_addr(fields[2])?,
                live: matches!(state, 0x01 | 0x04 | 0x05),
                pid: None,
                inode: fields[9].parse::<u64>().ok(),
            })
        })
        .collect()
}

#[cfg(any(target_os = "linux", target_os = "android"))]
mod imp {
    use std::{collections::HashSet, fs, io, net::SocketAddr};

    use super::Row;

    pub fn rows() -> io::Result<Vec<Row>> {
        let mut rows = Vec::new();
        for file in ["/proc/net/tcp", "/proc/net/tcp6"] {
            match fs::read_to_string(file) {
                Ok(table) => rows.extend(super::parse_proc_table(&table)),
                // Noyau sans IPv6 : seul tcp6 peut manquer
                Err(err) if file.ends_with('6') && err.kind() == io::ErrorKind::NotFound => {}
                Err(err) => return Err(err),
            }
        }
        Ok(rows)
    }

    pub fn owned_by(pid: u32, rows: &[Row], clients: &[SocketAddr], server: SocketAddr) -> io::Result<Vec<bool>> {
        let found = super::rows_for(rows, server);
        let inodes: Vec<Option<u64>> = clients
            .iter()
            .map(|c| found.get(c).and_then(|r| r.inode).filter(|inode| *inode != 0))
            .collect();
        if inodes.iter().all(Option::is_none) {
            return Ok(vec![false; clients.len()]);
        }
        // Les sockets du programme, lus une fois pour toutes les connexions demandées
        let mut sockets = HashSet::new();
        for entry in fs::read_dir(format!("/proc/{pid}/fd"))? {
            let Ok(entry) = entry else { continue };
            if let Ok(target) = fs::read_link(entry.path())
                && let Some(inode) = target
                    .to_str()
                    .and_then(|t| t.strip_prefix("socket:["))
                    .and_then(|t| t.strip_suffix(']'))
                    .and_then(|t| t.parse::<u64>().ok())
            {
                sockets.insert(inode);
            }
        }
        Ok(inodes.into_iter().map(|inode| inode.is_some_and(|i| sockets.contains(&i))).collect())
    }
}

#[cfg(windows)]
mod imp {
    use std::{
        ffi::c_void,
        io,
        net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    };

    use super::Row;

    const AF_INET: u32 = 2;
    const AF_INET6: u32 = 23;
    const TCP_TABLE_OWNER_PID_CONNECTIONS: u32 = 4;
    const NO_ERROR: u32 = 0;
    const ERROR_INSUFFICIENT_BUFFER: u32 = 122;
    /// MIB_TCPROW_OWNER_PID : état, adresse et port locaux, distants, programme (6 mots).
    const ROW_V4: usize = 6;
    /// MIB_TCP6ROW_OWNER_PID : adresse (4 mots), portée, port locaux ; de même pour
    /// l'autre bout ; état, programme (14 mots).
    const ROW_V6: usize = 14;
    /// MIB_TCP_STATE vivants : établie (5), fermée de ce côté seulement (6 et 7).
    fn live(state: u32) -> bool {
        matches!(state, 5..=7)
    }

    #[link(name = "iphlpapi")]
    unsafe extern "system" {
        fn GetExtendedTcpTable(
            table: *mut c_void,
            size: *mut u32,
            order: i32,
            family: u32,
            class: u32,
            reserved: u32,
        ) -> u32;
    }

    pub fn rows() -> io::Result<Vec<Row>> {
        let mut rows = Vec::new();
        // Un programme IPv4 est dans la table IPv4 ; un socket double pile, dans l'autre
        for (family, size) in [(AF_INET, ROW_V4), (AF_INET6, ROW_V6)] {
            let table = table(family)?;
            let count = *table.first().unwrap_or(&0) as usize;
            let words = table.get(1..).unwrap_or_default();
            if words.len() < count.saturating_mul(size) {
                return Err(io::Error::other("table des connexions TCP tronquée"));
            }
            for w in words.chunks_exact(size).take(count) {
                rows.push(if family == AF_INET {
                    Row {
                        local: v4(w[1], w[2]),
                        remote: v4(w[3], w[4]),
                        live: live(w[0]),
                        pid: Some(w[5]),
                        inode: None,
                    }
                } else {
                    Row {
                        local: v6(&w[0..4], w[5]),
                        remote: v6(&w[6..10], w[11]),
                        live: live(w[12]),
                        pid: Some(w[13]),
                        inode: None,
                    }
                });
            }
        }
        Ok(rows)
    }

    pub fn owned_by(pid: u32, rows: &[Row], clients: &[SocketAddr], server: SocketAddr) -> io::Result<Vec<bool>> {
        let found = super::rows_for(rows, server);
        Ok(clients.iter().map(|c| found.get(c).is_some_and(|r| r.pid == Some(pid))).collect())
    }

    /// Port : les deux premiers octets du mot, dans l'ordre du réseau.
    fn port(word: u32) -> u16 {
        let bytes = word.to_ne_bytes();
        u16::from_be_bytes([bytes[0], bytes[1]])
    }

    fn v4(addr: u32, port_word: u32) -> SocketAddr {
        SocketAddr::new(IpAddr::V4(Ipv4Addr::from(addr.to_ne_bytes())), port(port_word))
    }

    fn v6(addr: &[u32], port_word: u32) -> SocketAddr {
        let mut bytes = [0u8; 16];
        for (chunk, word) in bytes.chunks_mut(4).zip(addr) {
            chunk.copy_from_slice(&word.to_ne_bytes());
        }
        SocketAddr::new(super::unmapped(Ipv6Addr::from(bytes)), port(port_word))
    }

    /// La table du système, en mots de 32 bits (l'alignement de ses lignes). Elle peut
    /// grandir entre la demande de taille et la lecture : quelques essais.
    fn table(family: u32) -> io::Result<Vec<u32>> {
        let mut size: u32 = 16 * 1024;
        for _ in 0..8 {
            let mut buffer = vec![0u32; (size as usize).div_ceil(4)];
            let mut given = (buffer.len() * 4) as u32;
            // SAFETY : la table s'écrit dans `buffer`, dont la taille en octets est `given`.
            let code = unsafe {
                GetExtendedTcpTable(
                    buffer.as_mut_ptr().cast(),
                    &mut given,
                    0,
                    family,
                    TCP_TABLE_OWNER_PID_CONNECTIONS,
                    0,
                )
            };
            match code {
                NO_ERROR => return Ok(buffer),
                ERROR_INSUFFICIENT_BUFFER => size = given.saturating_add(4096),
                other => return Err(io::Error::from_raw_os_error(other as i32)),
            }
        }
        Err(io::Error::other("table des connexions TCP qui change sans cesse"))
    }
}

#[cfg(not(any(windows, target_os = "linux", target_os = "android")))]
mod imp {
    use std::{io, net::SocketAddr};

    use super::Row;

    pub fn rows() -> io::Result<Vec<Row>> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "système non pris en charge"))
    }

    pub fn owned_by(_pid: u32, _rows: &[Row], _clients: &[SocketAddr], _server: SocketAddr) -> io::Result<Vec<bool>> {
        Err(io::Error::new(io::ErrorKind::Unsupported, "système non pris en charge"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proc_net_tcp_lines_are_read() {
        let lo = |port| SocketAddr::from(([127, 0, 0, 1], port));
        // Telles qu'un noyau petit-boutiste les écrit (127.0.0.1 → 0100007F)
        let le = cfg!(target_endian = "little");
        let addr = if le { "0100007F" } else { "7F000001" };
        assert_eq!(parse_proc_addr(&format!("{addr}:1F90")), Some(lo(8080)));
        let mapped = if le { "0000000000000000FFFF00000100007F" } else { "00000000000000000000FFFF7F000001" };
        assert_eq!(parse_proc_addr(&format!("{mapped}:0050")), Some(lo(80)));
        assert_eq!(parse_proc_addr("zz:1"), None);
        assert_eq!(parse_proc_addr("0100007F"), None);
        let table = format!(
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n\
             \x20  0: {addr}:C350 {addr}:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 4242 1 0\n\
             \x20  1: {addr}:1F90 {addr}:C350 01 00000000:00000000 00:00000000 00000000  1000        0 4343 1 0\n\
             \x20  2: {addr}:C351 {addr}:1F90 06 00000000:00000000 00:00000000 00000000  1000        0 0 1 0\n\
             \x20  3: {addr}:C352 {addr}:1F90 05 00000000:00000000 00:00000000 00000000  1000        0 0 1 0\n"
        );
        let rows = parse_proc_table(&table);
        assert_eq!(rows.len(), 4);
        // L'appelant (port 50000) vers le port d'écoute (8080) : sa ligne à lui, pas l'autre
        let found = rows_for(&rows, lo(8080));
        assert_eq!(found.get(&lo(50000)).and_then(|r| r.inode), Some(4242));
        assert!(!found.contains_key(&lo(1)));
        // Vivantes vers 8080 : l'établie et celle fermée de son côté seulement (05), pas
        // celle en TIME_WAIT (06)
        assert_eq!(live_clients(&rows, lo(8080)), vec![lo(50000), lo(50002)]);
    }

    #[cfg(any(windows, target_os = "linux"))]
    #[test]
    fn a_connection_is_traced_to_its_program() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let server = listener.local_addr().unwrap();
        let _client = std::net::TcpStream::connect(server).unwrap();
        let (_accepted, client) = listener.accept().unwrap();
        let me = std::process::id();
        assert!(connected_by(me, client, server).unwrap(), "ce programme est au bout");
        // Un autre programme (sous Windows, aucun numéro n'est impair)
        assert!(!connected_by(me + 1, client, server).unwrap_or(false));
        // Une connexion qui n'existe pas
        let absent = SocketAddr::from(([127, 0, 0, 1], 1));
        assert!(!connected_by(me, absent, server).unwrap());
        // Plusieurs d'un coup, d'une seule lecture
        assert_eq!(connected_by_all(me, &[absent, client], server).unwrap(), vec![false, true]);
        assert!(clients_of(server).unwrap().contains(&client));
        assert!(available());
    }
}
