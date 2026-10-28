"""Connexions au serveur entre machines (0.0.0.0:47701) : d'où elles viennent, combien,
et combien de temps elles peuvent rester sans rien demander.

Audit du 20/10/2026 : le plafond global de connexions d'uvicorn (limit_concurrency), posé
contre les corps sans limite, ouvrait un autre déni de service. uvicorn n'a aucun délai
pour lire les en-têtes d'une requête : 64 connexions muettes d'un appareil du réseau local,
sans appairage, donnaient 503 à tout le monde (mes machines, mes amis, et le trafic Internet
relayé par le compagnon), aussi longtemps qu'il les gardait. Désormais :

- les en-têtes d'une requête doivent être arrivés HEADER_S après l'ouverture de la
  connexion (ou après la réponse précédente) ; sinon, la connexion est fermée ;
- un corps qui arrive au goutte-à-goutte (moins de MIN_RATE en moyenne, après BODY_GRACE,
  hors des moments où ce serveur lui-même ne lit pas) aussi ; sauf depuis cette machine
  (same_machine : le compagnon a ses propres bornes, compagnon/src/relay_body.rs) ;
- PER_ADDRESS connexions au plus par adresse inconnue, et MAX_STRANGERS au plus pour
  toutes les adresses inconnues réunies (ni une de mes machines ou de mes amis à son
  adresse connue, ni cette machine) : un appareil inconnu, même avec plusieurs adresses, ne
  prive pas mes machines du serveur. Une adresse connue en a PER_KNOWN : une rafale de
  conversations d'une de mes machines (une par connexion, client.STREAMS_PER_PEER à la fois)
  et ses autres requêtes y tiennent largement (relecture finale du 21/10/2026 : à 32, ses
  connexions en trop étaient fermées sans réponse, « Machine injoignable »). Au-delà, la
  connexion est fermée aussitôt.

Et l'en-tête X-Aiodos-Peer (la clé Internet de l'appelant) n'est cru que d'une connexion
venue de cette machine même : seul le compagnon l'ajoute, et un appareil du réseau local
qui l'enverrait lui-même se ferait passer pour une machine jointe par Internet. De même
pour X-Aiodos-Public (la clé jetable d'un inconnu du réseau public, network/public_lending.py) :
sans lui, crédible, aucune route /public/… n'est ouverte.
"""
import ipaddress
import logging
import re
import time

from uvicorn.protocols.http.h11_impl import H11Protocol

from aiodos.network import peers

log = logging.getLogger("aiodos.network")

PEER_HEADER = b"x-aiodos-peer"     # clé Internet de l'appelant, ajoutée par le compagnon
PUBLIC_HEADER = b"x-aiodos-public"  # clé jetable d'un inconnu du réseau public, de même
_PUBLIC_KEY = re.compile(r"[0-9a-f]{16,128}")   # une clé d'Iroh en hexadécimal : 64 signes
HEADER_S = 10            # s pour envoyer les en-têtes d'une requête
BODY_GRACE = 10          # s de lecture du corps avant de regarder son débit
MIN_RATE = 8 * 1024      # octets/s en moyenne, au moins, pour le corps d'une requête
WATCH_EVERY = 1.0        # s entre deux coups d'œil sur une connexion
PER_ADDRESS = 32         # connexions à la fois depuis une même adresse inconnue du réseau
PER_KNOWN = 128          # depuis l'adresse connue d'une de mes machines ou d'un ami
MAX_STRANGERS = 256      # connexions à la fois depuis toutes les adresses inconnues réunies
MAX_LOOPBACK = 512       # connexions à la fois depuis cette machine (compagnon, essais)
KNOWN_TTL = 10           # s : adresses connues de mes machines et amis, relues au plus

_known = (0.0, frozenset())


def normal_host(host) -> str:
    """L'adresse sous une forme comparable (« ::ffff:192.168.1.20 » = « 192.168.1.20 »)."""
    text = str(host or "").strip("[]")
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return text.lower()
    mapped = getattr(ip, "ipv4_mapped", None)
    return str(mapped or ip)


def is_loopback(host) -> bool:
    """Connexion venue de cette machine (le compagnon Internet, ou un essai) ?"""
    try:
        return ipaddress.ip_address(normal_host(host)).is_loopback
    except ValueError:
        return False


def same_machine(client_host, server_host) -> bool:
    """La connexion vient-elle de cette machine ? Depuis la boucle locale, ou depuis
    l'adresse même où elle arrive (serveur lancé sur une adresse précise, --p2p-host : le
    compagnon s'y connecte). Un autre appareil ne peut pas avoir cette adresse-là."""
    if is_loopback(client_host):
        return True
    return bool(client_host) and bool(server_host) \
        and normal_host(client_host) == normal_host(server_host)


def _from_here(scope, header: bytes) -> str:
    """L'en-tête `header` (en minuscules), seulement si la requête vient de cette machine."""
    client = scope.get("client") or (None, None)
    server = scope.get("server") or (None, None)
    if not same_machine(client[0], server[0]):
        return ""
    for name, value in scope.get("headers") or []:
        if name == header:
            return value.decode("latin-1").strip().lower()
    return ""


def internet_key(scope) -> str:
    """Clé Internet de l'appelant (X-Aiodos-Peer), seulement si la requête vient du
    compagnon, donc de cette machine ; "" sinon : venue du réseau local, l'en-tête ne
    prouve rien et la requête est traitée comme venue du réseau local."""
    return _from_here(scope, PEER_HEADER)


def public_key(scope) -> str:
    """Clé jetable d'un inconnu du réseau public (X-Aiodos-Public), seulement si la requête
    vient du compagnon, donc de cette machine, et si c'est bien une clé (hexadécimal) ; ""
    sinon. Elle n'est ni gardée ni comparée à rien : elle dit seulement que la demande vient
    du réseau public, par le compagnon (network/public_lending.py)."""
    key = _from_here(scope, PUBLIC_HEADER)
    return key if _PUBLIC_KEY.fullmatch(key) else ""


def known_addresses() -> frozenset:
    """Adresses de mes machines et de mes amis sur le réseau local (relues au plus toutes
    les KNOWN_TTL secondes : appelé à chaque connexion, dans la boucle du nœud)."""
    global _known
    at, found = _known
    if time.monotonic() - at > KNOWN_TTL:
        try:
            found = frozenset(normal_host(a) for a in peers.addresses())
        except Exception as exc:  # noqa: BLE001 — base indisponible : personne n'est connu
            log.debug("Adresses des machines appairées illisibles : %s", exc)
            found = frozenset()
        _known = (time.monotonic(), found)
    return found


def forget_known():
    """Les adresses connues seront relues à la prochaine connexion (essais, appairage)."""
    global _known
    _known = (0.0, frozenset())


class GuardedH11(H11Protocol):
    """Protocole HTTP/1.1 d'uvicorn, avec les délais et plafonds décrits plus haut. Passé à
    uvicorn.Config(http=…) par manager._listen ; le reste (requêtes, réponses, maintien
    de la connexion) est celui d'uvicorn."""

    def connection_made(self, transport):
        super().connection_made(transport)
        self._watch_handle = None
        self._bytes = 0                  # octets reçus sur cette connexion
        self._waiting_since = self.loop.time()   # attente des en-têtes d'une requête
        self._body_cycle = None          # requête dont on mesure le corps
        self._body_start = 0
        self._body_active = 0.0          # temps de lecture du corps, hors pauses de ce serveur
        host = normal_host(self.client[0]) if self.client else ""
        self._host = host
        self._local = same_machine(host, self.server[0] if self.server else None)
        self._stranger = not self._local and host not in known_addresses()
        if not self._room():
            transport.close()
            return
        self._watch_handle = self.loop.call_later(WATCH_EVERY, self._watch)

    def _room(self) -> bool:
        """Cette connexion a-t-elle sa place ? (déjà comptée dans self.connections)"""
        same = strangers = 0
        for other in self.connections:
            if getattr(other, "_host", None) == self._host:
                same += 1
            if getattr(other, "_stranger", False):
                strangers += 1
        if self._local:
            full = same > MAX_LOOPBACK
        elif self._stranger:
            full = same > PER_ADDRESS or strangers > MAX_STRANGERS
        else:
            full = same > PER_KNOWN
        if full:
            log.debug("Connexion de %s refusée : trop de connexions ouvertes", self._host)
        return not full

    def connection_lost(self, exc):
        handle = getattr(self, "_watch_handle", None)
        if handle is not None:
            handle.cancel()
            self._watch_handle = None
        super().connection_lost(exc)

    def data_received(self, data):
        self._bytes += len(data)
        super().data_received(data)

    def on_response_complete(self):
        self._waiting_since = self.loop.time()   # en-têtes de la requête suivante attendus
        super().on_response_complete()

    def _watch(self):
        self._watch_handle = None
        if self.transport is None or self.transport.is_closing():
            return
        now = self.loop.time()
        cycle = self.cycle
        if cycle is None or cycle.response_complete:
            # Entre deux requêtes : les en-têtes de la suivante (sans rien reçu, le maintien
            # de la connexion d'uvicorn la ferme déjà au bout de quelques secondes)
            if now - self._waiting_since > HEADER_S:
                return self._close("en-têtes trop lents")     # i18n: tel quel (journal)
        elif cycle.more_body and not self._local:
            if self._body_cycle is not cycle:
                self._body_cycle, self._body_start, self._body_active = cycle, self._bytes, 0.0
            elif not self.flow.read_paused:     # ce serveur lit : le débit compte
                self._body_active += WATCH_EVERY
                rate = (self._bytes - self._body_start) / self._body_active
                if self._body_active > BODY_GRACE and rate < MIN_RATE:
                    # i18n: tel quel (journal)
                    return self._close("corps envoyé au goutte-à-goutte")
        self._watch_handle = self.loop.call_later(WATCH_EVERY, self._watch)

    def _close(self, why: str):
        log.debug("Connexion de %s fermée : %s", self._host, why)
        self.transport.close()
