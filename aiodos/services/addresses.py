"""Quelles adresses une lecture de page web a le droit de joindre.

Une page demandée par l'utilisateur lui-même, sur sa machine, peut viser son réseau
local s'il le veut (le wiki de son NAS) : c'est une unité « page du réseau local »,
jamais confiée ailleurs (jobs/kinds/index.py). Toute autre lecture — une unité « page
web » qu'une autre machine nous confie, un résultat de recherche du chat — ne joint que
l'Internet public : jamais la boucle locale, le réseau privé, le lien local (dont les
métadonnées cloud en 169.254.169.254), le CGNAT, ni rien de réservé. Sinon une autre
machine lirait, à travers la nôtre, des services de notre réseau qu'elle ne peut pas
joindre elle-même.

Le contrôle porte sur l'adresse IP réellement jointe, pas sur le nom : le nom est résolu
une seule fois, chaque adresse est vérifiée, puis la connexion part vers l'adresse
vérifiée elle-même (pas de seconde résolution qui pourrait basculer vers l'interne entre
la vérification et la connexion). Le nom reste celui de la requête (en-tête Host, SNI et
certificat TLS). Chaque redirection ouvre une nouvelle connexion, donc repasse ici.

Quelle page est « du réseau local » se décide sans résolution de nom (is_private_target) :
une adresse IP privée écrite telle quelle, ou un nom local (« nas », « wiki.lan »,
« imprimante.local »). Un nom public qui résoudrait, ce jour-là, vers une adresse interne
reste soumis à la règle publique : la décision ne dépend pas d'un DNS qui ne nous
appartient pas.

La garde (Guard) retient aussi les connexions ouvertes, pour qu'une lecture qui dépasse
son délai puisse être coupée net (services/web.py) ; elle porte l'échéance de la lecture,
que la résolution du nom et chaque essai de connexion respectent aussi. Aucune dépendance
en plus : urllib3 arrive avec requests. La garde passe par des fonctions internes
d'urllib3 : si une version les change, la lecture est refusée (supported, Guard.joined)
plutôt que faite sans garde.
"""
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

import requests
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.poolmanager import PoolManager
from urllib3.util.connection import allowed_gai_family

MAX_TRIED = 4                 # adresses essayées pour un nom (IPv6 puis IPv4…)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# IPv6 que ipaddress.is_global dit publiques, mais qu'aucune page de l'Internet n'utilise
# et qu'un système peut encore mener à son réseau : « compatibles IPv4 » (::a.b.c.d,
# dépréciées), « traduites » (::ffff:0:a.b.c.d, SIIT), site-local (fec0::/10, dépréciées)
_NEVER_PUBLIC = tuple(ipaddress.ip_network(n) for n in ("::/96", "::ffff:0:0:0/96",
                                                         "fec0::/10"))
# Suffixes des noms du réseau local (mDNS, box, réseaux d'entreprise) ; un nom sans point
# (« nas ») en est un aussi
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".intranet",
                  ".localdomain", ".localhost")


class BlockedAddress(Exception):
    """Adresse refusée. Le message est toujours le même, sans hôte ni port : renvoyé à
    l'autre machine en cas d'échec, il ne doit rien dire de ce qui existe sur notre réseau."""

    MESSAGE = ("adresse refusée : introuvable ou hors de l'Internet public (réseau local, "
               "cette machine). Seule une page demandée sur cette machine peut viser son "
               "réseau local, par son adresse IP (http://192.168.1.20/…) ou son nom local "
               "(http://nas.local/…).")

    def __init__(self, message=None):
        super().__init__(message or self.MESSAGE)


# La garde s'accroche à des fonctions internes d'urllib3 (_new_conn, _dns_host) : si une
# version les change, elle ne s'appliquerait plus, sans bruit. On le vérifie avant toute
# connexion (supported), et après (Guard.joined) : la lecture est alors refusée.
GUARD_BYPASSED = ("lecture refusée : la garde des adresses ne s'applique pas avec cette "
                  "version de requests ou d'urllib3. Mets AIODOS à jour, ou reviens à la "
                  "version de ces paquets qu'il installe.")


def supported() -> bool:
    """Cette version d'urllib3 ouvre-t-elle ses connexions comme la garde l'attend ?"""
    return ("_new_conn" in vars(HTTPConnection)
            and isinstance(getattr(HTTPConnection, "host", None), property)
            and "_new_conn" in vars(HTTPConnectionPool))


def _unwrapped(ip):
    """Une IPv6 qui enveloppe une IPv4 (::ffff:a.b.c.d, 6to4, Teredo, NAT64) ramenée à
    cette IPv4 : sans quoi une adresse privée déguisée passerait pour publique."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip.teredo is not None:
            return ip.teredo[1]
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_public(ip_text) -> bool:
    """Vrai si cette adresse IP est routable sur l'Internet public (ipaddress.is_global,
    enveloppes IPv6 ouvertes), n'est pas du multicast, ni une des IPv6 de _NEVER_PUBLIC."""
    try:
        ip = ipaddress.ip_address(str(ip_text).split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in net for net in _NEVER_PUBLIC):
        return False
    ip = _unwrapped(ip)
    return bool(ip.is_global) and not ip.is_multicast


class SlowResolution(Exception):
    """La résolution du nom n'a pas abouti avant l'échéance de la lecture."""


def _getaddrinfo(host, port, timeout):
    """socket.getaddrinfo, borné par `timeout` (secondes ; None : sans borne). Le système ne
    sait pas interrompre une résolution : elle se poursuit dans un fil à part, sans nous."""
    if timeout is None:
        return socket.getaddrinfo(host, port, allowed_gai_family(), socket.SOCK_STREAM)
    box = {}

    def run():
        try:
            box["infos"] = socket.getaddrinfo(host, port, allowed_gai_family(),
                                              socket.SOCK_STREAM)
        except Exception as exc:          # noqa: BLE001  (rendue au fil qui attend)
            box["error"] = exc
    worker = threading.Thread(target=run, daemon=True, name="aiodos-resolution")
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        raise SlowResolution()
    if "error" in box:
        raise box["error"]
    return box["infos"]


def resolve(host, port, timeout=None) -> list:
    """Les adresses IP de `host`, dans l'ordre du système. BlockedAddress si introuvable,
    SlowResolution si la réponse n'arrive pas en `timeout` secondes."""
    if not host:
        raise BlockedAddress()
    try:
        infos = _getaddrinfo(host.strip("[]"), port, timeout)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise BlockedAddress() from exc
    found = []
    for info in infos:
        address = info[4][0]
        if address not in found:
            found.append(address)
    if not found:
        raise BlockedAddress()
    return found


def is_private_target(url) -> bool:
    """Cette URL vise-t-elle le réseau local ou cette machine ? Sans résolution de nom : une
    adresse IP non publique écrite telle quelle (192.168.1.20, [::1]), « localhost », ou un
    nom local (sans point, ou en .local, .lan… : LOCAL_SUFFIXES). Tout autre nom suit la
    règle commune, adresses publiques seulement, même s'il résout aujourd'hui vers
    l'interne : ce DNS ne nous appartient pas."""
    try:
        host = (urlparse(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host:
        return False
    try:
        return not is_public(ipaddress.ip_address(host.split("%")[0]))
    except ValueError:
        pass
    return (host == "localhost" or "." not in host
            or any(host.endswith(suffix) for suffix in LOCAL_SUFFIXES))


class Guard:
    """La garde d'une lecture : quelles adresses elle peut joindre (public_only), son
    échéance (deadline, en time.monotonic ; None : sans), et les connexions qu'elle a
    ouvertes, que cut() coupe net (délai dépassé, arrêt demandé)."""

    def __init__(self, public_only=True, deadline=None):
        self.public_only = public_only
        self.deadline = deadline
        self.was_cut = False
        self._sockets = []
        self._lock = threading.Lock()

    @property
    def joined(self) -> bool:
        """Au moins une connexion est passée par la garde (cf. GUARD_BYPASSED)."""
        with self._lock:
            return bool(self._sockets)

    def remaining(self):
        """Secondes avant l'échéance (0 au plus bas), None sans échéance."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def addresses(self, host, port) -> list:
        """Les adresses à essayer pour ce nom : toutes publiques, sinon BlockedAddress. Un
        nom qui résout aussi vers l'interne est refusé en entier."""
        found = resolve(host, port, self.remaining())
        if self.public_only and not all(is_public(a) for a in found):
            raise BlockedAddress()
        return found[:MAX_TRIED]

    def connected(self, sock):
        """Une connexion vient de s'ouvrir : dernier contrôle de l'adresse jointe, et
        retenue pour pouvoir la couper."""
        try:
            peer = sock.getpeername()[0]
        except OSError:
            peer = ""
        if self.public_only and not is_public(peer):
            sock.close()
            raise BlockedAddress()
        with self._lock:
            self._sockets.append(sock)
            cut = self.was_cut
        if cut:
            _shutdown(sock)

    def cut(self):
        """Coupe les connexions de cette lecture (depuis un autre fil) : une lecture bloquée
        se termine aussitôt en erreur."""
        with self._lock:
            self.was_cut = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _PinnedConnection:
    """Connexion vers une adresse vérifiée par la garde, résolue une seule fois. Sans garde
    (ne devrait pas arriver), la règle la plus stricte : adresses publiques seulement.
    L'échéance de la garde borne la résolution et chaque essai de connexion : le fil de
    garde ne coupe qu'une connexion établie, pas une connexion en cours d'établissement."""

    guard = None

    def _new_conn(self):
        guard = self.guard or Guard(public_only=True)
        name, configured = getattr(self, "_dns_host", None), self.timeout
        if not name:
            raise BlockedAddress(GUARD_BYPASSED)     # urllib3 a changé : rien ne part
        try:
            found = guard.addresses(name, self.port)
        except SlowResolution as exc:
            raise ConnectTimeoutError(self, "résolution du nom trop lente") from exc
        error = None
        for address in found:
            left = guard.remaining()
            if left is not None and left <= 0:
                break
            self._dns_host = address          # connexion vers l'adresse vérifiée…
            if left is not None:
                bounded = isinstance(configured, (int, float))
                self.timeout = min(configured, left) if bounded else left
            try:
                sock = super()._new_conn()
            except (NewConnectionError, ConnectTimeoutError) as exc:
                error = exc
                continue
            finally:
                self._dns_host = name         # … le nom reste celui de la requête (Host, TLS)
                self.timeout = configured
            guard.connected(sock)
            return sock
        raise error or ConnectTimeoutError(self, "délai total de la lecture dépassé")


class _GuardedHTTPConnection(_PinnedConnection, HTTPConnection):
    pass


class _GuardedHTTPSConnection(_PinnedConnection, HTTPSConnection):
    pass


class _GuardedPoolMixin:
    guard = None

    def _new_conn(self):
        conn = super()._new_conn()
        conn.guard = self.guard
        return conn


class _GuardedHTTPPool(_GuardedPoolMixin, HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(_GuardedPoolMixin, HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedPoolManager(PoolManager):
    def __init__(self, guard, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = guard
        self.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}

    def _new_pool(self, scheme, host, port, request_context=None):
        pool = super()._new_pool(scheme, host, port, request_context)
        pool.guard = self.guard
        return pool


class GuardedAdapter(requests.adapters.HTTPAdapter):
    """Adaptateur requests dont chaque connexion passe par la garde."""

    def __init__(self, guard: Guard, **kwargs):
        if not supported():
            raise BlockedAddress(GUARD_BYPASSED)
        self.guard = guard
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        self.poolmanager = _GuardedPoolManager(self.guard, num_pools=connections,
                                               maxsize=maxsize, block=block, **kwargs)
