"""Balise UDP d'une machine appairée : un indice, jamais une preuve.

N'importe quel appareil du réseau local peut envoyer une balise au nom d'une de mes
machines : son identifiant circule en clair, toutes les 5 s. Faille du 20/10/2026 : la
balise réécrivait l'adresse, le port et le nom gardés pour elle, et tous les appels
partaient vers l'appareil qui l'avait envoyée (sans repli par Internet s'il acceptait la
connexion). Désormais, une balise qui annonce une autre adresse (DHCP, autre Wi-Fi, port
de secours) déclenche seulement un ping scellé vers cette adresse : elle n'est retenue que
si la réponse est scellée avec la clé de la paire, et le nom vient de cette réponse.

La réponse scellée prouve que la machine est au bout du chemin, pas que l'adresse est la
sienne : un appareil du réseau local pourrait annoncer l'identifiant depuis sa propre
adresse et relayer la connexion vers la vraie machine, puis écarter les requêtes qu'il
veut (relecture du 21/10/2026). La machine dit donc aussi, dans sa réponse scellée, où la
requête lui est arrivée (« where », p2p_app._arrived_at) : l'adresse n'est retenue que si
c'est bien celle qui est annoncée. Une machine d'une version précédente ne le dit pas : elle
n'est pas suivie sur balise (mets AIODOS à jour sur elle) ; ses propres messages scellés
apprennent toujours sa nouvelle adresse à celle-ci (p2p_app, peers.touch).

Une même adresse n'est vérifiée qu'une fois par CHECK_EVERY, quelques-unes à la fois, et
chaque vérification a un délai total : un flot de balises ne coûte presque rien.
"""
import http.client
import json
import logging
import socket
import threading
import time

from aiodos.network import connections, envelope, peers, route

log = logging.getLogger("aiodos.network")

CHECK_EVERY = 30          # s : une même adresse annoncée est vérifiée une fois par période
MAX_RUNNING = 4           # vérifications en même temps, toutes machines confondues
TIMEOUT = 3               # s par lecture : sur le réseau local, une réponse vient en quelques ms
DEADLINE = 8              # s en tout, même si l'autre côté répond goutte à goutte
MAX_REPLY = 64 * 1024     # une réponse à « ping » fait quelques centaines d'octets
REMEMBERED = 1024         # adresses retenues (pour ne pas les revérifier), au plus


class BeaconChecks:
    """Vérifications en cours ou récentes des adresses annoncées par mes machines."""

    def __init__(self, manager):
        self.m = manager
        self.threads = []              # vérifications en cours
        self.started = 0               # vérifications lancées (essais)
        self._lock = threading.Lock()
        self._last = {}                # (machine, adresse, port) -> instant de la vérification
        self._checking = set()         # machines en cours de vérification
        self._outdated = set()         # machines d'une version qui ne dit pas « where »

    def hint(self, node_id: str, address: str, port: int, known: dict):
        """Balise d'une machine appairée (`known` : ce qu'on garde d'elle, peers.transport).
        Même adresse et même port : rien à faire. Sinon, une vérification en tâche de fond,
        si cette adresse ne l'a pas été récemment."""
        if (known.get("address"), known.get("port")) == (address, port):
            return
        key = (node_id, address, port)
        now = time.monotonic()
        with self._lock:
            self.threads = [t for t in self.threads if t.is_alive()]
            seen = self._last.get(key)
            if seen is not None and now - seen < CHECK_EVERY:
                return
            if len(self.threads) >= MAX_RUNNING or node_id in self._checking:
                return                 # la prochaine balise réessaiera
            if len(self._last) >= REMEMBERED:
                self._last = {k: at for k, at in self._last.items() if now - at < CHECK_EVERY}
                if len(self._last) >= REMEMBERED:
                    self._last.clear()
            self._last[key] = now
            self._checking.add(node_id)
            self.started += 1
            thread = threading.Thread(target=self._check, args=(node_id, address, port),
                                      daemon=True, name="aiodos-balise")
            thread.start()
            self.threads.append(thread)

    def _check(self, node_id: str, address: str, port: int):
        try:
            peer = peers.get(node_id)
            if not peer or not peer.get("key"):
                return
            reply = confirm(self.m.node_id, peer, address, port)
            peers.touch(node_id, address, port, reply.get("name"))
            connections.forget_known()     # sa nouvelle adresse n'est plus celle d'un inconnu
            route.lan_ok(node_id)
            log.info("« %s » jointe à sa nouvelle adresse %s:%s (réponse scellée)",
                     peer["name"], address, port)
            self.m.changed()
        except Outdated:
            if node_id not in self._outdated:
                self._outdated.add(node_id)
                log.info("« %s » annonce une nouvelle adresse (%s:%s) sans pouvoir prouver "
                         "qu'elle est la sienne : mets AIODOS à jour sur « %s ». En attendant, "
                         "seuls ses propres messages font connaître sa nouvelle adresse.",
                         peer["name"], address, port, peer["name"])
        except Exception as exc:  # noqa: BLE001 — rien de prouvé : rien ne change
            log.debug("Balise de %s depuis %s:%s non confirmée : %s", node_id, address, port, exc)
        finally:
            with self._lock:
                self._checking.discard(node_id)


class Outdated(ValueError):
    """La machine a répondu, mais sans dire où la requête lui est arrivée : version d'avant
    le 21/10/2026."""


def _cut(conn):
    """Délai total dépassé : la connexion est coupée, la lecture en cours s'arrête."""
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def confirm(my_id: str, peer: dict, address: str, port: int) -> dict:
    """Ping scellé vers cette adresse et ce port seulement : ni Internet, ni mandataire, ni
    décompression, réponse bornée. → la réponse déscellée avec la clé de la paire, qui
    prouve que la machine est bien là, et où la requête lui est arrivée : cette adresse et
    ce port mêmes ; une exception sinon (Outdated : version qui ne le dit pas). Ses
    messages ne vont qu'au journal (BeaconChecks._check) : ils restent en français."""
    env = envelope.wrap(peer["key"], "ping", my_id, peer["node_id"], {"where": True})
    host = address.strip("[]")
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    timer = threading.Timer(DEADLINE, _cut, args=(conn,))
    timer.daemon = True
    timer.start()
    try:
        conn.request("POST", "/p2p/v1/ping", json.dumps(env).encode(),
                     {"Content-Type": "application/json", "Accept-Encoding": "identity"})
        response = conn.getresponse()
        data = response.read(MAX_REPLY + 1)
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        if len(data) > MAX_REPLY:
            raise ValueError("réponse trop longue pour un ping")  # i18n: tel quel (journal)
    finally:
        timer.cancel()
        conn.close()
    reply = json.loads(data)
    if not isinstance(reply, dict):
        raise ValueError("réponse illisible")  # i18n: tel quel (journal)
    found = envelope.unwrap_reply(peer["key"], "ping", peer["node_id"], my_id, env["ts"], reply)
    if not isinstance(found, dict):
        raise ValueError("réponse illisible")  # i18n: tel quel (journal)
    where = found.get("where")
    if "where" not in found:
        # i18n: tel quel (journal)
        raise Outdated("version qui ne dit pas où la requête est arrivée")
    if not (isinstance(where, list) and len(where) == 2
            and connections.normal_host(where[0]) == connections.normal_host(address)
            and where[1] == port):
        # i18n: tel quel (journal)
        raise ValueError(f"requête arrivée ailleurs ({where}) : un relais, pas la machine")
    return found
