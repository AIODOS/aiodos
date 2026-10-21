"""Réponses des autres machines bornées par celle qui demande (network/client.py) : suite des
failles 19, 23 et 25 (relectures du 21/10/2026), et de la faille 16 pour les nombres non finis.

Un vrai serveur HTTP local joue la machine d'en face : réponse énorme (annoncée ou non), bombe
gzip, filet d'octets dans le corps ou dans l'en-tête, refus démesuré, morceau de flux sans fin.
Le client d'avant lisait tout, décompressé, sans échéance : chacun de ces essais échoue avec
lui. Les plafonds sont aussi confrontés aux plus grosses réponses honnêtes, calculées avec les
bornes mêmes des machines qui répondent : ils ne doivent rien refuser de légitime."""
import gzip
import http.server
import itertools
import json
import os
import threading
import time
import tracemalloc

import pytest

from aiodos.imaging import share
from aiodos.network import client, consult, crypto, envelope, peers, route, sharing, work, worker
from aiodos.network.client import PeerError
from aiodos.updates import manifest
from aiodos.updates import p2p as updates
from aiodos.updates.store import KEEP_PACKAGES

ME, FRIEND = "a" * 32, "f" * 32
KEY = bytes(range(32))
ZEROS = bytes(1024 ** 2)
BOMB = gzip.compress(bytes(64 * 1024 ** 2), 9)        # 64 Mio une fois décompressés
MEMORY = 16 * 1024 ** 2                                # mémoire de pointe tolérée pour 64 Mio
HEAD_TRICKLE = 6                                       # s d'un en-tête qui n'en finit pas


# ------------------------------------------------------------- la machine d'en face
class Peer(http.server.BaseHTTPRequestHandler):
    """La machine d'en face : `answer(handler, enveloppe)` décide de ce qu'elle renvoie."""
    protocol_version = "HTTP/1.1"
    answer = None
    stop = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            env = json.loads(body) if body else {}
        except ValueError:
            env = {}
        try:
            self.answer(self, env)
        except OSError:
            pass                  # celle qui demande a coupé : c'est ce qu'on attend d'elle
        self.close_connection = True

    def log_message(self, *args):
        pass


@pytest.fixture
def friend(monkeypatch):
    """start(answer) : la machine d'en face répond ainsi ; rend la machine appairée telle que
    client.call la joint (réseau local)."""
    monkeypatch.setattr(route, "_lan_allowed", True)
    servers, stop = [], threading.Event()

    def start(answer):
        handler = type("Handler", (Peer,), {"answer": staticmethod(answer), "stop": stop})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        peers.save(FRIEND, "PC d'un ami", KEY, "127.0.0.1", server.server_address[1],
                   trust=sharing.FRIEND)
        return peers.get(FRIEND)
    yield start
    stop.set()
    for server in servers:
        server.shutdown()
        server.server_close()
    client._lingering.clear()


def sealed(handler, env, action, payload, status=200):
    """Réponse scellée honnête (envelope.wrap_reply), comme celle de p2p_app."""
    data = json.dumps(envelope.wrap_reply(KEY, action, FRIEND, ME, env["ts"], payload)).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def raw_reply(handler, status, data: bytes, headers=()):
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def huge(announced: bool, status=200):
    """64 Mio de réponse, annoncés (Content-Length) ou non (jusqu'à la fermeture)."""
    def answer(handler, env):
        handler.send_response(status)
        if announced:
            handler.send_header("Content-Length", str(64 * len(ZEROS)))
        else:
            handler.send_header("Connection", "close")
        handler.end_headers()
        for _ in range(64):
            handler.wfile.write(ZEROS)
    return answer


def bomb(handler, env):
    raw_reply(handler, 200, BOMB, [("Content-Encoding", "gzip")])


def trickle_body(handler, env):
    """En-tête aussitôt, puis 60 octets, un toutes les 0,2 s : chaque octet arrive dans le
    délai de lecture, qui repart à chaque fois."""
    handler.send_response(200)
    handler.send_header("Content-Length", "60")
    handler.end_headers()
    for _ in range(60):
        if handler.stop.wait(0.2):
            return
        handler.wfile.write(b"x")


def endless_head(seconds, release=None):
    """Un en-tête qui n'en finit pas : « 100 Continue », un octet toutes les 0,05 s, pendant
    `seconds` s, ou jusqu'à `release` ; la connexion est ensuite fermée."""
    def answer(handler, env):
        end = time.monotonic() + seconds
        for byte in itertools.cycle(b"HTTP/1.1 100 Continue\r\n\r\n"):
            if time.monotonic() > end or handler.stop.wait(0.05) \
                    or (release is not None and release.is_set()):
                return
            handler.wfile.write(bytes([byte]))
    return answer


def in_turn(*answers):
    """La n-ième demande reçue a la réponse answers[n], les suivantes une réponse honnête à
    « ping »."""
    count = itertools.count()

    def answer(handler, env):
        n = next(count)
        if n < len(answers):
            answers[n](handler, env)
        else:
            sealed(handler, env, "ping", {"ok": True})
    return answer


def called_again(peer, within: float):
    """La machine est de nouveau appelée (et répond) avant `within` s."""
    end = time.monotonic() + within
    while True:
        try:
            return client.call(ME, peer, "ping", {}, timeout=2)
        except PeerError as exc:
            assert "toujours pas fini" in str(exc) and time.monotonic() < end
            time.sleep(0.1)


def peak_memory(run) -> int:
    tracemalloc.start()
    try:
        run()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# ----------------------------------------------------------------------- taille
@pytest.mark.parametrize("answer,why", [(huge(True), "trop volumineuse"),
                                        (huge(False), "trop volumineuse"),
                                        (bomb, "compressée")],
                         ids=["annoncée", "sans taille", "gzip"])
@pytest.mark.parametrize("action", ["update/manifest", "update/chunk", "library/search"])
def test_a_friend_cannot_flood_the_memory_of_this_machine(friend, action, answer, why):
    """Failles 23 et 25 (relecture du 21/10/2026) : client.call lisait toute la réponse,
    décompressée, avant de la refuser. Un ami répondait à update/manifest ou update/chunk par
    des centaines de Mio, ou par une bombe gzip."""
    peer = friend(answer)

    def run():
        with pytest.raises(PeerError, match=why):
            client.call(ME, peer, action, {}, timeout=5)
    assert peak_memory(run) < MEMORY


def test_hello_and_pairing_answers_are_bounded_too(friend):
    """« hello » part vers une machine annoncée sur le réseau local, avant tout appairage."""
    peer = friend(huge(False))

    def run():
        with pytest.raises(PeerError, match="trop volumineuse"):
            client.hello("127.0.0.1", peer["port"])
        with pytest.raises(PeerError, match="trop volumineuse"):
            client.pair_start("127.0.0.1", peer["port"], {"pairing": 2})
    assert peak_memory(run) < MEMORY
    friend(bomb)
    with pytest.raises(PeerError, match="compressée"):
        client.hello("127.0.0.1", peers.get(FRIEND)["port"])


def test_a_huge_answer_shaped_like_json_is_never_analysed(friend):
    """Sous le plafond d'update/chunk, 1,8 Mio de [{}, {}, …] deviendraient près de 40 Mio
    d'objets : une grosse réponse n'est reconnue qu'à sa forme {"box": "…"}."""
    shaped = b"[" + b"{}," * 600_000 + b"{}]"           # sous le plafond : refusée pour sa forme
    peer = friend(lambda handler, env: raw_reply(handler, 200, shaped))

    def run():
        with pytest.raises(PeerError, match="illisible"):
            client.call(ME, peer, "update/chunk", {}, timeout=5)
    assert peak_memory(run) < MEMORY


def test_a_refusal_is_read_bounded_and_made_readable(friend):
    """Le « detail » d'un refus vient de l'autre machine : lu borné, montré sur une ligne, sans
    caractère invisible (U+202E retourne le texte affiché), et pas plus long que DETAIL."""
    peer = friend(huge(True, status=400))

    def run():
        with pytest.raises(PeerError, match=r"^Refus de l'autre machine \(HTTP 400\)\.$"):
            client.call(ME, peer, "ping", {}, timeout=5)
    assert peak_memory(run) < MEMORY
    odd = "Refusé‮ par\n\x07 l'autre" + " é" * 2000
    friend(lambda handler, env: raw_reply(handler, 403, json.dumps({"detail": odd}).encode()))
    with pytest.raises(PeerError) as refused:
        client.call(ME, peers.get(FRIEND), "ping", {}, timeout=5)
    message = str(refused.value)
    assert message.startswith("Refusé par l'autre é é") and len(message) <= client.DETAIL
    assert not any(c in message for c in "‮\n\x07")


def test_every_failure_becomes_a_readable_peer_error(friend):
    """Scellée avec une autre clé, chargée d'un NaN ou coupée en route : la réponse est
    refusée par une PeerError lisible, que tous les appelants attrapent (et non une SealError
    ou une erreur de requests qui remonterait jusqu'à eux)."""
    def other_key(handler, env):
        data = json.dumps(envelope.wrap_reply(bytes(32), "ping", FRIEND, ME, env["ts"], {}))
        raw_reply(handler, 200, data.encode())
    friend(other_key)
    with pytest.raises(PeerError, match="Réponse refusée : Message altéré"):
        client.call(ME, peers.get(FRIEND), "ping", {}, timeout=5)

    def cut_short(handler, env):          # 1000 octets annoncés, 9 envoyés, puis fermée
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        handler.wfile.write(b'{"box": "')
    friend(cut_short)
    with pytest.raises(PeerError, match="Connexion coupée"):
        client.call(ME, peers.get(FRIEND), "ping", {}, timeout=5)

    def not_finite(handler, env):
        aad = envelope._aad("rep", "ping", FRIEND, ME, env["ts"])
        box = crypto.seal(KEY, b'{"name": "PC", "time": NaN}', aad)
        raw_reply(handler, 200, json.dumps({"box": envelope.b64e(box)}).encode())
    friend(not_finite)
    with pytest.raises(PeerError, match="non fini"):
        client.call(ME, peers.get(FRIEND), "ping", {}, timeout=5)


# ----------------------------------------------------------------------- durée
def test_a_trickle_of_bytes_does_not_hold_the_caller(friend):
    """Faille 23 : le délai de lecture repart à chaque octet reçu, si bien qu'un filet d'octets
    retenait l'appel indéfiniment. L'échéance (4 délais de lecture, plus la connexion) le rend
    à l'appelant, et la connexion coupée libère aussi le fil qui lisait."""
    peer = friend(trickle_body)
    started = time.monotonic()
    with pytest.raises(PeerError, match="trop lentement"):
        client.call(ME, peer, "ping", {}, timeout=0.5)
    assert time.monotonic() - started < 6         # échéance : 2,5 s ; le filet, lui, dure 12 s
    # Le fil qui lisait finit au plus tard au prochain octet : la machine est vite rappelée
    peer = friend(lambda handler, env: sealed(handler, env, "ping", {"ok": True}))
    assert called_again(peer, within=3) == {"ok": True}


def test_a_header_that_never_ends_holds_one_thread_at_most(friend):
    """L'en-tête aussi peut arriver goutte à goutte (« 100 Continue » sans fin), et requests
    ne laisse pas couper la connexion avant de l'avoir. L'appelant est quand même rendu à
    l'échéance ; tant que le fil pris n'a pas fini, rien de plus n'est demandé à cette machine
    (sans quoi chaque nouvel appel y laisserait un fil) ; ensuite, elle l'est de nouveau."""
    peer = friend(in_turn(endless_head(HEAD_TRICKLE)))
    started = time.monotonic()
    with pytest.raises(PeerError, match="trop lentement"):
        client.call(ME, peer, "ping", {}, timeout=0.5)
    assert time.monotonic() - started < HEAD_TRICKLE - 0.5     # échéance : 2,5 s
    started = time.monotonic()
    with pytest.raises(PeerError, match="toujours pas fini"):
        client.call(ME, peer, "ping", {}, timeout=0.5)
    assert time.monotonic() - started < 1                       # sans rien envoyer
    assert called_again(peer, within=HEAD_TRICKLE + 10) == {"ok": True}


def test_a_machine_cannot_free_one_held_call_and_keep_the_others(friend):
    """Deux appels partis ensemble, tous deux abandonnés : la machine libère le dernier et
    garde l'autre. Elle n'est toujours pas rappelée tant que l'autre n'a pas fini, sans quoi
    elle retiendrait les fils un à un en libérant chaque fois celui qu'on surveille."""
    release, errors = threading.Event(), []
    peer = friend(in_turn(endless_head(60, release), endless_head(4)))

    def ask():
        try:
            client.call(ME, peer, "ping", {}, timeout=0.5)
        except PeerError as exc:
            errors.append(str(exc))
    first = threading.Thread(target=ask)
    first.start()
    time.sleep(0.5)                       # le second part après, et il est abandonné après
    second = threading.Thread(target=ask)
    second.start()
    first.join(10)
    second.join(10)
    assert len(errors) == 2 and all("trop lentement" in error for error in errors)
    time.sleep(2)                         # le second en-tête s'est arrêté, pas le premier
    with pytest.raises(PeerError, match="toujours pas fini"):
        client.call(ME, peer, "ping", {}, timeout=0.5)
    release.set()
    assert called_again(peer, within=5) == {"ok": True}


# ------------------------------------------------------------- réponses en flux
def chunked(handler, pieces, headers=()):
    """Réponse en flux comme celle de p2p_app (StreamingResponse) : découpée en morceaux."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Transfer-Encoding", "chunked")
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    for piece in pieces:
        handler.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
    handler.wfile.write(b"0\r\n\r\n")


def frames(env, items) -> list:
    return [envelope.wrap_frame(KEY, "chat", FRIEND, ME, env["ts"], seq, item).encode() + b"\n"
            for seq, item in enumerate(items)]


def test_a_stream_is_read_as_it_comes_with_bounded_frames(friend):
    """Une conversation prêtée arrive morceau par morceau : chacun est borné (MAX_FRAME), une
    ligne sans fin est refusée avant de remplir la mémoire ; un flux compressé est refusé."""
    peer = friend(lambda handler, env: chunked(handler, frames(
        env, [{"content": "Bon"}, {"alive": True}, {"content": "jour"}, {"end": True}])))
    assert list(client.stream(ME, peer, "chat", {})) == [
        {"content": "Bon"}, {"alive": True}, {"content": "jour"}]

    friend(lambda handler, env: chunked(handler, [b"A" * 1024 ** 2, b"A" * 1024 ** 2]))
    with pytest.raises(PeerError, match="trop volumineux"):
        list(client.stream(ME, peers.get(FRIEND), "chat", {}))

    friend(lambda handler, env: chunked(
        handler, [gzip.compress(b"".join(frames(env, [{"content": "Bon"}, {"end": True}])))],
        headers=[("Content-Encoding", "gzip")]))
    with pytest.raises(PeerError, match="compressée"):
        list(client.stream(ME, peers.get(FRIEND), "chat", {}))

    friend(huge(True, status=400))
    with pytest.raises(PeerError, match=r"HTTP 400"):
        list(client.stream(ME, peers.get(FRIEND), "chat", {}))


def test_the_deadline_leaves_time_to_send_a_large_body(friend, monkeypatch):
    """L'échéance d'un appel compte le temps d'envoyer son corps au débit minimal : un gros
    résultat honnête sur un lien lent n'est plus déclaré « trop lent » en plein envoi, ce
    qui laissait cette machine « occupée » pour tous ses appels jusqu'à la fin de l'envoi
    abandonné (relecture finale du 21/10/2026)."""
    left = []
    monkeypatch.setattr(client, "_within", lambda destination, deadline, work:
                        left.append(deadline - time.monotonic()) or {"box": "x"})
    peer = friend(lambda handler, env: None)
    for payload in ({"x": 1}, {"output": "A" * (8 * 1024 ** 2)}):
        with pytest.raises(PeerError):                    # la fausse réponse ne s'ouvre pas
            client.call(ME, peer, "work/result", payload, timeout=10)
    sealed_size = 8 * 1024 ** 2 * 4 / 3
    assert left[1] - left[0] >= 0.9 * sealed_size / client.SEND_RATE
    assert left[0] < 4 * client.DEADLINE_FACTOR * 10                # un petit appel : inchangé


def test_a_burst_of_streams_waits_its_turn_here(friend, monkeypatch):
    """Pas plus de STREAMS_PER_PEER réponses en flux à la fois vers une même machine : une
    rafale de conversations (un outil branché sur la passerelle) attend son tour ici, au lieu
    de prendre chez l'autre machine toutes les places de son compagnon (relecture finale du
    21/10/2026 : 21 conversations sur 40 y étaient refusées, et ses nouvelles ne passaient
    plus). Une conversation arrêtée pendant qu'elle attend ne part jamais."""
    monkeypatch.setattr(client, "STREAMS_PER_PEER", 2)
    client._turns.clear()
    release, lock = threading.Event(), threading.Lock()
    inside, most = [0], [0]

    def answer(handler, env):
        with lock:
            inside[0] += 1
            most[0] = max(most[0], inside[0])
        try:
            release.wait(10)
            chunked(handler, frames(env, [{"content": "Bon"}, {"end": True}]))
        finally:
            with lock:
                inside[0] -= 1
    peer = friend(answer)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        list(client.stream(ME, peer, "chat", {})))) for _ in range(5)]
    try:
        for thread in threads:
            thread.start()
        time.sleep(1)
        assert inside[0] == 2              # deux chez l'autre machine, les autres attendent ici
        stop = threading.Event()
        stop.set()
        assert list(client.stream(ME, peer, "chat", {}, stop=stop)) == []   # rien ne part
        release.set()
        for thread in threads:
            thread.join(15)
        assert results == [[{"content": "Bon"}]] * 5 and most[0] == 2
    finally:
        release.set()
        client._turns.clear()


# ------------------------------------------- plafonds et réponses honnêtes
def reply_size(action: str, payload: dict) -> int:
    """Taille, sur le réseau, de la réponse scellée qui porte `payload`."""
    reply = envelope.wrap_reply(KEY, action, FRIEND, ME, time.time(), payload)
    return len(json.dumps(reply).encode())


def largest_signed() -> dict:
    """Le plus gros manifeste que manifest.verify accepte : MAX_TEXT caractères de 4 octets
    (le pire en UTF-8), et toutes ses approbations."""
    return {"manifest": "\U0001f600" * manifest.MAX_TEXT, "key": "0" * 16, "signature": "A" * 88,
            "approvals": [{"key": f"{i:016x}", "signature": "A" * 88}
                          for i in range(manifest.MAX_APPROVALS)]}


def test_each_limit_holds_the_largest_honest_answer():
    """Chaque plafond, confronté aux bornes mêmes des machines qui répondent : un morceau de
    fichier entier, le plus gros manifeste, les versions en attente, un profil plein, une
    recherche pleine. Une borne relevée là-bas sans l'être ici ferait échouer ces essais."""
    piece = lambda size: {"data": envelope.b64e(bytes(size))}      # noqa: E731
    assert reply_size("update/chunk", piece(updates.CHUNK)) <= client.limit_for("update/chunk")
    assert reply_size("images/file", piece(share.CHUNK)) <= client.limit_for("images/file")
    assert reply_size("work/blob", piece(work.BLOB_CHUNK)) <= client.limit_for("work/blob")
    assert client.limit_for("work/lease") >= worker.LEASE_REPLY
    assert client.MANIFEST_TEXT == manifest.MAX_TEXT
    signed = largest_signed()
    assert reply_size("update/manifest", {"signed": signed}) <= client.limit_for("update/manifest")
    pending = [{"version": f"2.0.{i}", "signed": signed} for i in range(2 * KEEP_PACKAGES)]
    assert reply_size("update/pending", {"pending": pending}) <= client.limit_for("update/pending")
    model = {"name": "é" * 200, "digest": "x" * 100, "size": 2 ** 62, "family": "é" * 80,
             "parameters": "x" * 40, "quantization": "x" * 40, "caps": ["x" * 40] * 16,
             "fits_vram": True, "loaded": False}
    profile = {"name": "é" * 60, "ollama": {"models": [model] * 300},
               "images": {"models": ["é" * 100] * 200},
               "updates": {"files": {f"2.0.{i}": ["x" * 60] * 32 for i in range(64)}}}
    assert reply_size("profile", profile) <= client.limit_for("profile")
    found = {"passages": [{"text": "é" * consult.MAX_TEXT, "document": "é" * 200,
                           "library": "é" * 200, "score": 0.5}] * consult.MAX_PASSAGES,
             "open": 3}
    assert reply_size("library/search", found) <= client.limit_for("library/search")
    assert client.limit_for("ping") == client.DEFAULT_REPLY


@pytest.mark.parametrize("action,payload", [
    ("update/chunk", lambda: {"data": envelope.b64e(os.urandom(updates.CHUNK))}),
    ("images/file", lambda: {"data": envelope.b64e(os.urandom(share.CHUNK))}),
    ("update/manifest", lambda: {"signed": largest_signed()}),
])
def test_the_largest_honest_answers_still_pass(friend, action, payload):
    expected = payload()
    peer = friend(lambda handler, env: sealed(handler, env, action, expected))
    assert client.call(ME, peer, action, {}, timeout=10) == expected
