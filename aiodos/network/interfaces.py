"""Les vraies cartes réseau de cette machine (Wi-Fi, Ethernet) : leurs adresses IPv4 et
l'adresse de diffusion de leur réseau, sans celles d'un VPN ni des machines virtuelles.

Sous un VPN (NordVPN, WireGuard, OpenVPN…), la route par défaut passe par la carte du VPN.
Deux conséquences, que ce module évite (demande de l'auteur, 21/10/2026) :
- une annonce envoyée à 255.255.255.255 peut sortir par la carte du VPN et n'atteindre
  aucune machine du Wi-Fi : on annonce aussi à l'adresse de diffusion de chaque vrai réseau
  local (192.168.1.255…), que sa propre route envoie par la bonne carte ;
- l'adresse « pour ajouter cette machine » devenait celle du VPN : on montre celle de la
  vraie carte.

D'où viennent les cartes :
- Windows : GetAdaptersAddresses (iphlpapi), qui donne le type de chaque carte ;
- Linux : les adresses par ioctl, la nature d'après le nom (wlan0, eth0, enp3s0… contre
  tun0, wg0, nordlynx, docker0…) ;
- appli Android : Android lui-même (Device.java, node/android_app.py) ;
- ailleurs, ou si rien n'est lisible : aucune, et l'appelant garde la route par défaut.
Relu au plus toutes les TTL secondes : un Wi-Fi qui change est vu en une minute.
"""
import ctypes
import ipaddress
import logging
import re
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass

from aiodos.node import android_app

log = logging.getLogger("aiodos.network")

TTL = 60
# Noms et descriptions des cartes qui ne sont pas un réseau local réel
_VIRTUAL = re.compile(
    r"(?i)(vpn|nordlynx|wireguard|wintun|openvpn|\btap\b|tap-|\btun\b|tun\d|^tun|^tap|"
    r"^wg\d|tailscale|zerotier|^zt|hamachi|radmin|virtualbox|vmware|hyper-v|vethernet|"
    r"\bwsl\b|docker|^veth|^br-|^virbr|^lxc|loopback|bluetooth|teredo|isatap|6to4)")
# Windows : types de cartes (ifType, RFC 1213)
_ETHERNET, _WIFI = 6, 71
_IF_UP = 1
_cache = {"at": None, "value": []}
_lock = threading.Lock()


@dataclass(frozen=True)
class Lan:
    """Une adresse IPv4 d'une vraie carte, et son réseau."""
    name: str
    address: str
    prefix: int

    @property
    def broadcast(self) -> str | None:
        """L'adresse de diffusion de son réseau ; None pour un réseau d'une ou deux
        adresses (/31, /32), qui n'en a pas."""
        if self.prefix >= 31:
            return None
        network = ipaddress.IPv4Interface(f"{self.address}/{self.prefix}").network
        return str(network.broadcast_address)


def is_virtual(*names: str) -> bool:
    """Une carte de VPN, de machine virtuelle ou de tunnel, d'après son nom ou sa
    description."""
    return any(_VIRTUAL.search(n or "") for n in names)


def _usable(address: str) -> bool:
    """Une adresse d'un réseau local joignable : ni boucle, ni lien local (169.254), ni
    l'espace partagé des opérateurs (100.64/10, souvent celui d'un VPN)."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return False
    return ip not in ipaddress.IPv4Network("100.64.0.0/10")


def lan_interfaces(max_age: float = TTL) -> list:
    """Les adresses des vraies cartes en service (liste de Lan), Wi-Fi et Ethernet d'abord
    dans l'ordre du système ; [] si on ne peut pas les lire."""
    now = time.monotonic()
    with _lock:
        if _cache["at"] is not None and now - _cache["at"] < max_age:
            return list(_cache["value"])
    try:
        if android_app.in_app():
            found = _from_android()
        elif sys.platform == "win32":
            found = _from_windows()
        elif sys.platform.startswith("linux") or sys.platform == "android":
            found = _from_linux()
        else:
            found = []
    except Exception as exc:  # noqa: BLE001 — un relevé raté ne doit rien arrêter
        log.debug("Cartes réseau illisibles : %s", exc)
        found = []
    found = [lan for lan in found if _usable(lan.address)]
    with _lock:
        _cache.update(at=now, value=found)
    return list(found)


def forget():
    """Relire les cartes au prochain appel (essais)."""
    with _lock:
        _cache.update(at=None, value=[])


# ------------------------------------------------------------------- Windows
class _SockAddr(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("port", ctypes.c_ushort),
                ("addr", ctypes.c_ubyte * 4), ("zero", ctypes.c_ubyte * 8)]


class _SocketAddress(ctypes.Structure):
    _fields_ = [("sockaddr", ctypes.POINTER(_SockAddr)), ("length", ctypes.c_int)]


class _Unicast(ctypes.Structure):
    pass


_Unicast._fields_ = [("length", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                     ("next", ctypes.POINTER(_Unicast)), ("address", _SocketAddress),
                     ("prefix_origin", ctypes.c_int), ("suffix_origin", ctypes.c_int),
                     ("dad_state", ctypes.c_int), ("valid_lifetime", ctypes.c_ulong),
                     ("preferred_lifetime", ctypes.c_ulong), ("lease_lifetime", ctypes.c_ulong),
                     ("on_link_prefix_length", ctypes.c_ubyte)]


class _Adapter(ctypes.Structure):
    """Le début de IP_ADAPTER_ADDRESSES_LH : seulement les champs lus ici."""


_Adapter._fields_ = [("length", ctypes.c_ulong), ("if_index", ctypes.c_ulong),
                     ("next", ctypes.POINTER(_Adapter)), ("adapter_name", ctypes.c_char_p),
                     ("first_unicast", ctypes.POINTER(_Unicast)),
                     ("first_anycast", ctypes.c_void_p), ("first_multicast", ctypes.c_void_p),
                     ("first_dns_server", ctypes.c_void_p), ("dns_suffix", ctypes.c_wchar_p),
                     ("description", ctypes.c_wchar_p), ("friendly_name", ctypes.c_wchar_p),
                     ("physical_address", ctypes.c_ubyte * 8),
                     ("physical_address_length", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                     ("mtu", ctypes.c_ulong), ("if_type", ctypes.c_ulong),
                     ("oper_status", ctypes.c_int)]


def _from_windows() -> list:
    """GetAdaptersAddresses, IPv4 seulement : les cartes Ethernet et Wi-Fi en service,
    sans les VPN, les machines virtuelles ni Hyper-V (vEthernet), qui se disent parfois
    « Ethernet »."""
    get = ctypes.windll.iphlpapi.GetAdaptersAddresses
    get.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ulong)]
    get.restype = ctypes.c_ulong
    flags = 0x0002 | 0x0004 | 0x0008          # sans anycast, multicast ni serveurs DNS
    size = ctypes.c_ulong(16 * 1024)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = get(socket.AF_INET, flags, None, buffer, ctypes.byref(size))
        if result == 0:
            break
        if result != 111:                         # ERROR_BUFFER_OVERFLOW : plus grand
            return []
    else:
        return []
    found = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(_Adapter))
    while adapter:
        a = adapter.contents
        if (a.oper_status == _IF_UP and a.if_type in (_ETHERNET, _WIFI)
                and not is_virtual(a.friendly_name, a.description)):
            unicast = a.first_unicast
            while unicast:
                u = unicast.contents
                sockaddr = u.address.sockaddr
                if sockaddr and sockaddr.contents.family == socket.AF_INET:
                    address = ".".join(str(b) for b in sockaddr.contents.addr)
                    found.append(Lan(a.friendly_name or "", address,
                                     int(u.on_link_prefix_length)))
                unicast = u.next
        adapter = a.next
    # Le Wi-Fi et l'Ethernet dans l'ordre du système : une seule adresse par réseau
    return list(dict.fromkeys(found))


# --------------------------------------------------------------------- Linux
def _from_linux() -> list:
    """Les adresses IPv4 des cartes par ioctl (SIOCGIFADDR, SIOCGIFNETMASK), sans les
    cartes virtuelles d'après leur nom (tun0, wg0, nordlynx, docker0, veth…)."""
    import fcntl
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _index, name in socket.if_nameindex():
            if name == "lo" or is_virtual(name):
                continue
            request = struct.pack("256s", name.encode()[:15])
            try:
                address = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, request)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, request)[20:24])
            except OSError:
                continue                                  # pas d'adresse IPv4, ou refusé
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            found.append(Lan(name, address, prefix))
    return found


# ------------------------------------------------------------- appli Android
def _from_android() -> list:
    """Les adresses du Wi-Fi et de l'Ethernet que donne Android (pas celles du VPN)."""
    return [Lan(str(item.get("name") or ""), str(item.get("address") or ""),
                int(item.get("prefix") or 32))
            for item in android_app.lan_addresses()
            if isinstance(item, dict) and item.get("address")]
