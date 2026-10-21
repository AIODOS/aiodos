# SPDX-License-Identifier: Apache-2.0
# aiodos-client 0.1.0
"""Le transport vers la passerelle : http.client directement, sur cette machine seulement.

- L'hôte est vérifié avant tout envoi : 127.0.0.1, localhost ou [::1], sinon l'erreur
  `non_local_url`. http.client ne lit aucun proxy dans l'environnement et ne suit aucune
  redirection : une réponse 3xx est une erreur `protocol`.
- Une connexion par requête, fermée à la fin.
- La lecture n'attend jamais plus de 0,1 s d'affilée (select) : elle compte elle-même
  l'inactivité, remise à zéro à chaque octet reçu (morceaux du flux et commentaires
  « : aiodos loading N » compris), et voit aussitôt un arrêt demandé.
- L'arrêt : un fil de surveillance interroge cancel() toutes les 0,1 s ; dès qu'il devient
  vrai, il ferme la connexion dans les deux sens (sock.shutdown) : AIODOS voit l'appli partir
  et arrête le calcul, même si le fil de l'appli est occupé ailleurs (dans on_text…), et la
  lecture lève Cancelled. Sous Windows, shutdown() ne réveille pas une lecture déjà en attente
  (mesuré le 21/10/2026 : elle ne repart qu'à l'octet suivant) ; d'où les attentes courtes.
"""

import http.client
import io
import json
import logging
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from . import errors, texts

log = logging.getLogger("aiodos_client")

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
SLICE = 0.1                     # attente la plus longue d'un seul tenant
CANCEL_POLL = 0.1               # cadence du fil de surveillance
CONNECT_TIMEOUT = 5.0
MAX_BODY = 256 * 1024 * 1024    # une réponse JSON (embeddings d'un gros lot compris)
MAX_LINE = 16 * 1024 * 1024     # une ligne d'un flux SSE


def as_cancel(cancel):
    """Une fonction sans argument qui dit si l'appli demande l'arrêt, ou None.

    `cancel` : une fonction (QThread.isInterruptionRequested…) ou un threading.Event. Elle est
    interrogée par un fil de surveillance : elle doit être sûre entre threads. Si elle lève une
    exception, elle compte pour « pas d'arrêt » (signalé une fois au journal).
    """
    if cancel is None:
        return None
    is_set = getattr(cancel, "is_set", None)
    if callable(is_set):
        return is_set
    if not callable(cancel):
        raise TypeError("cancel : une fonction sans argument, ou un threading.Event")
    warned = []

    def asked():
        try:
            return bool(cancel())
        except Exception:
            if not warned:
                warned.append(True)
                log.warning("cancel() a levé une exception : elle est ignorée.", exc_info=True)
            return False

    return asked


def pause(seconds, stop=None) -> bool:
    """Attend `seconds` secondes par tranches courtes, dans le fil de l'appelant ; vrai si
    `stop()` est devenu vrai entre-temps."""
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        if stop is not None and stop():
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(0.05, left))


class Endpoint:
    """L'adresse de la passerelle, vérifiée : http, et un hôte de cette machine seulement.

    Seule l'origine compte (http://hôte:port) : un chemin donné avec l'adresse est ignoré.
    """

    __slots__ = ("host", "port")

    def __init__(self, url, lang="fr"):
        text = str(url or "").strip()
        try:
            parts = urlsplit(text)
            port = parts.port
            host = parts.hostname or ""
        except ValueError:
            raise errors.make("non_local_url", lang, url="?") from None
        if (parts.scheme != "http" or host not in LOCAL_HOSTS
                or parts.username is not None or parts.password is not None):
            shown = "?"
            if parts.scheme and host:                   # sans ce qui précède « @ »
                shown = f"{parts.scheme}://{f'[{host}]' if ':' in host else host}"
                shown += f":{port}" if port else ""
            raise errors.make("non_local_url", lang, url=shown)
        self.host = host
        self.port = port or 80

    @property
    def netloc(self) -> str:
        """« 127.0.0.1:11435 » (« [::1]:11435 »)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.netloc}"

    def __eq__(self, other):
        return isinstance(other, Endpoint) and (self.host, self.port) == (other.host, other.port)

    def __hash__(self):
        return hash((self.host, self.port))

    def __repr__(self):
        return f"Endpoint({self.url!r})"


class Response:
    """Une réponse lue en entier."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        return self.headers.get(name) if self.headers is not None else None

    def json(self, lang="fr"):
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise errors.make("protocol", lang) from None


def content_type(headers) -> str:
    value = headers.get("Content-Type") if headers is not None else None
    return (value or "").split(";", 1)[0].strip().lower()


class _Stopped(Exception):
    """Arrêt demandé, vu par la lecture."""


class _Idle(Exception):
    """Inactivité trop longue, vue par la lecture."""


class _Gate:
    """Le délai d'inactivité et l'arrêt, partagés par la lecture et le fil de surveillance."""

    def __init__(self, first, idle):
        self.limit = first          # avant les premières données : le délai de chargement
        self._after = idle          # ensuite : le délai d'inactivité
        self.deadline = time.monotonic() + first
        self.cancelled = False

    def touch(self):
        self.deadline = time.monotonic() + self.limit

    def started(self):
        self.limit = self._after
        self.touch()

    def wait_readable(self, sock):
        while True:
            if self.cancelled:
                raise _Stopped()
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise _Idle()
            ready, _, _ = select.select([sock], [], [], min(SLICE, left))
            if ready:
                return


class _GatedRaw(io.RawIOBase):
    """La lecture de la socket, par la garde."""

    def __init__(self, sock, gate):
        super().__init__()
        self._sock = sock
        self._gate = gate

    def readable(self):
        return True

    def readinto(self, buffer):
        self._gate.wait_readable(self._sock)
        count = self._sock.recv_into(buffer)
        if count:
            self._gate.touch()
        return count


class _GatedSocket:
    """La socket telle que la voit http.client : la lecture passe par la garde, et la
    fermeture est faite par l'échange lui-même, une fois la réponse lue."""

    def __init__(self, sock, gate):
        self._sock = sock
        self._gate = gate

    def makefile(self, mode="rb", *args, **kwargs):
        return io.BufferedReader(_GatedRaw(self._sock, self._gate), 64 * 1024)

    def sendall(self, data, *args):
        return self._sock.sendall(data, *args)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _Connection(http.client.HTTPConnection):
    """La connexion d'une requête. « localhost » se joint sur 127.0.0.1 sans résolution de
    nom (la passerelle n'écoute pas en IPv6 : sous Windows, essayer ::1 d'abord coûte 2 s),
    l'en-tête Host restant « localhost:port »."""

    def __init__(self, endpoint, gate, connect_timeout):
        super().__init__(endpoint.host, endpoint.port, timeout=connect_timeout)
        self._gate = gate
        self.raw = None

    def connect(self):
        address = "127.0.0.1" if self.host == "localhost" else self.host
        self.raw = socket.create_connection((address, self.port), self.timeout)
        try:
            self.raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.sock = _GatedSocket(self.raw, self._gate)


class _Watcher:
    """Le fil de surveillance : ferme la connexion dès que cancel() devient vrai."""

    def __init__(self, sock, gate, cancel):
        self._sock = sock
        self._gate = gate
        self._cancel = cancel
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aiodos-client-arret",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(CANCEL_POLL):
            if self._cancel():
                self._gate.cancelled = True
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                log.debug("arrêt demandé : connexion fermée")
                return

    def stop(self):
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(1.0)


class Exchange:
    """Une requête sur sa propre connexion : connexion, envoi, lecture gardée, fermeture.

    `timeout` : le délai d'inactivité ; `first_timeout` : celui d'avant les premières
    données (un modèle qui se charge), le même par défaut.
    """

    def __init__(self, endpoint, *, timeout, first_timeout=None,
                 connect_timeout=CONNECT_TIMEOUT, cancel=None, lang="fr"):
        self.endpoint = endpoint
        self.lang = lang
        self.timeout = float(timeout)
        first = float(first_timeout) if first_timeout is not None else self.timeout
        self._gate = _Gate(first, self.timeout)
        self._connect_timeout = float(connect_timeout)
        self._cancel = as_cancel(cancel)
        self._conn = None
        self._watcher = None
        self.response = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def cancelled(self) -> bool:
        return self._gate.cancelled

    def send(self, method, path, headers=None, body=None):
        """Envoie la requête et lit la ligne d'état et les en-têtes de la réponse."""
        if self._cancel is not None and self._cancel():
            raise errors.make("cancelled", self.lang)
        conn = _Connection(self.endpoint, self._gate, self._connect_timeout)
        self._conn = conn
        try:
            conn.connect()
        except OSError as exc:
            log.debug("connexion à %s impossible (%s)", self.endpoint.url, type(exc).__name__)
            raise errors.make("unreachable", self.lang, host=self.endpoint.netloc) from exc
        conn.raw.settimeout(max(self.timeout, self._connect_timeout))     # pour l'envoi
        if self._cancel is not None:
            self._watcher = _Watcher(conn.raw, self._gate, self._cancel)
        self._gate.touch()
        try:
            conn.request(method, path, body=body, headers=headers or {})
            self.response = conn.getresponse()
        except Exception as exc:
            raise self._translate(exc) from exc
        return self.response

    def read_body(self, limit=MAX_BODY) -> bytes:
        """Le corps entier de la réponse (au plus `limit` octets)."""
        resp = self.response
        try:
            if resp.length is not None and resp.length > limit:
                raise errors.make("protocol", self.lang, message=texts.error("too_large",
                                                                             self.lang))
            data = resp.read(limit + 1)
        except Exception as exc:
            raise self._translate(exc) from exc
        if self._gate.cancelled:
            raise errors.make("cancelled", self.lang)
        if len(data) > limit:
            raise errors.make("protocol", self.lang, message=texts.error("too_large", self.lang))
        return data

    def _lines(self):
        """Les lignes brutes de la réponse. Lues par read1() : HTTPResponse.readline() passe
        par peek(), qui prend une coupure entre deux morceaux (chunked) pour une fin propre ;
        read1() lève IncompleteRead."""
        resp = self.response
        pending = b""
        while True:
            end = pending.find(b"\n")
            if end >= 0:
                line, pending = pending[:end + 1], pending[end + 1:]
                yield line
                continue
            if len(pending) > MAX_LINE:
                raise errors.make("protocol", self.lang, message=texts.error("too_large",
                                                                             self.lang))
            try:
                piece = resp.read1(64 * 1024)
            except Exception as exc:
                raise self._translate(exc) from exc
            if not piece:
                if pending:
                    yield pending
                return
            pending += piece

    def events(self):
        """Les événements du flux SSE, dans l'ordre : ("comment", texte) pour une ligne
        « : … », ("data", texte) pour un événement (ses lignes « data: » réunies)."""
        data = []
        first = True
        for raw in self._lines():
            if first:
                self._gate.started()
                first = False
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if data:
                    yield "data", "\n".join(data)
                    data = []
                continue
            if line.startswith(":"):
                yield "comment", line[1:].strip()
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
        if self._gate.cancelled:                        # fin due à l'arrêt demandé
            raise errors.make("cancelled", self.lang)
        if data:
            yield "data", "\n".join(data)

    def _translate(self, exc):
        if isinstance(exc, errors.AiodosError):
            return exc
        if self._gate.cancelled or isinstance(exc, _Stopped):
            return errors.make("cancelled", self.lang)
        if isinstance(exc, (_Idle, TimeoutError)):
            return errors.make("timeout", self.lang, detail={"seconds": self._gate.limit})
        host = self.endpoint.netloc
        if isinstance(exc, (http.client.RemoteDisconnected, http.client.IncompleteRead,
                            ConnectionError)):
            if self.response is not None:               # coupée en route
                return errors.make("unreachable", self.lang,
                                   message=texts.error("connection_lost", self.lang, host=host),
                                   hint=texts.hint("connection_lost", self.lang))
            return errors.make("unreachable", self.lang, host=host)
        if isinstance(exc, http.client.HTTPException):
            return errors.make("protocol", self.lang)
        if isinstance(exc, (OSError, ValueError)):
            return errors.make("unreachable", self.lang, host=host)
        return exc

    def close(self):
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        resp, self.response = self.response, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            if conn.raw is not None:
                try:
                    conn.raw.close()
                except OSError:
                    pass


def request(endpoint, method, path, *, headers=None, body=None, timeout=30.0,
            connect_timeout=CONNECT_TIMEOUT, cancel=None, lang="fr", limit=MAX_BODY):
    """Une requête complète : la réponse lue en entier (Response)."""
    with Exchange(endpoint, timeout=timeout, connect_timeout=connect_timeout, cancel=cancel,
                  lang=lang) as exchange:
        resp = exchange.send(method, path, headers, body)
        data = exchange.read_body(limit)
        return Response(resp.status, resp.headers, data)
