"""Sécurité de l'appairage par code (audit du 20/10/2026).

- Engagement avant révélation (comme la comparaison numérique de Bluetooth) : la machine
  qui lance l'appairage ne montre sa clé qu'après avoir reçu celle de l'autre, et le code
  vient de toute la transcription ; un intermédiaire ne peut plus choisir sa clé après
  coup pour faire apparaître le même code des deux côtés.
- Une demande en attente n'est jamais remplacée ; la réponse de l'utilisateur porte la
  demande qu'il a vue (ref) ; une machine déjà appairée n'est remplacée que sur un geste
  explicite.
- Une machine d'une version sans engagement reçoit un refus clair, dans les deux sens.
- Vue Réseau : le profil envoyé par une machine appairée n'entre dans le HTML que typé ou
  échappé.

Deux machines jouées dans ce processus (pas de réseau), puis l'interface avec Node.js."""
import json
import shutil
import subprocess

import pytest

from aiodos import db, paths
from aiodos.network import crypto, envelope, pairing, peers, sharing
from aiodos.network.client import PeerError
from aiodos.network.pairing import PairingRefused

A_ID, B_ID, M_ID = "a" * 32, "b" * 32, "c" * 32
A_IP, B_IP, M_IP = "192.168.1.10", "192.168.1.20", "192.168.1.66"


class Machine:
    """Une machine jouée ici : le minimum du gestionnaire dont l'appairage se sert."""
    p2p_port = 47701

    def __init__(self, node_id, name, address):
        self.node_id, self._name, self.address = node_id, name, address
        self.events = []
        self.pairing = pairing.Pairing(self)

    def name(self):
        return self._name

    def publish(self, event):
        self.events.append(event)

    def requests(self):
        return [e for e in self.events if e["type"] == "pair_request"]

    def changed(self):
        pass

    def on_paired(self, node_id):
        pass

    def present_later(self, *args, **kwargs):
        pass

    def present_new_machine(self, *args, **kwargs):
        pass

    def net_changed(self, *args):
        pass


class Lan:
    """Le réseau local entre ces machines : ce que client.hello, pair_start et call feraient
    (un refus de l'autre machine revient en PeerError, avec son message)."""

    def __init__(self, monkeypatch, *machines):
        self.by_address = {m.address: m for m in machines}
        self.by_id = {m.node_id: m for m in machines}
        self.sent = []                    # (adresse jointe, contenu) de chaque pair/start
        monkeypatch.setattr(pairing.client, "hello", self.hello)
        monkeypatch.setattr(pairing.client, "pair_start", self.pair_start)
        monkeypatch.setattr(pairing.client, "call", self.call)

    def hello(self, address, port, timeout=4):
        m = self.by_address[address]
        return {"protocol": envelope.PROTOCOL, "node_id": m.node_id, "name": m.name(),
                "port": m.p2p_port}

    def pair_start(self, address, port, payload, timeout=10):
        payload = json.loads(json.dumps(payload))          # comme sur le fil
        self.sent.append((address, payload))
        caller = self.by_id[payload["node_id"]].address
        try:
            return json.loads(json.dumps(self.by_address[address].pairing.incoming(payload,
                                                                                  caller)))
        except PeerError as exc:
            raise PeerError(str(exc)) from None            # HTTP 400 : le message seul

    def call(self, my_id, peer, action, payload, key=None, timeout=10):
        assert action == "pair/confirm"
        other = self.by_address.get(peer["address"])
        if other is None:
            raise PeerError("Machine injoignable (ConnectionError).")
        if other.pairing.key_for(my_id) != key:            # p2p_app : clé de l'appairage
            raise PeerError("Message altéré, ou clé incorrecte.")
        return other.pairing.remote_answer(my_id, payload["accept"])


@pytest.fixture
def lan(monkeypatch):
    db.connect()
    a, b = Machine(A_ID, "PC portable", A_IP), Machine(B_ID, "PC fixe", B_IP)
    return a, b, Lan(monkeypatch, a, b)


def _commit_payload(node_id=A_ID, to=B_ID, name="PC portable"):
    """Une demande (étape « commit ») telle qu'une machine la ferait : → (contenu, ouverture)."""
    private, public = crypto.new_exchange()
    nonce = crypto.new_nonce()
    payload = {"pairing": crypto.PAIRING, "step": "commit", "node_id": node_id, "name": name,
               "port": 47701, "commit": envelope.b64e(crypto.commitment(node_id, to, public,
                                                                        nonce))}
    reveal = {"pairing": crypto.PAIRING, "step": "reveal", "node_id": node_id,
              "pub": envelope.b64e(public), "nonce": envelope.b64e(nonce)}
    return payload, reveal


# --------------------------------------------------------------- engagement
def test_the_initiator_shows_its_key_only_after_receiving_the_other(lan):
    a, b, net = lan
    pending = a.pairing.start(B_IP, 47701)
    (_, commit), (_, reveal) = net.sent
    assert commit["step"] == "commit" and "pub" not in commit and "nonce" not in commit
    assert reveal["step"] == "reveal" and reveal["pub"] and reveal["nonce"]
    # B n'a montré sa demande qu'après la révélation, avec le même code et la même clé
    request = b.requests()[0]
    assert request["code"] == pending.code and request["node_id"] == A_ID
    assert b.pairing.get(A_ID).key == pending.key and len(pending.key) == 32
    assert request["ref"] and request["address"] == A_IP and request["known"] is None


def test_the_code_comes_from_the_whole_transcript():
    a_priv, a_pub = crypto.new_exchange()
    b_priv, b_pub = crypto.new_exchange()
    n_a, n_b = crypto.new_nonce(), crypto.new_nonce()
    side_a = crypto.transcript_secrets(a_priv, (A_ID, a_pub, n_a), (B_ID, b_pub, n_b))
    side_b = crypto.transcript_secrets(b_priv, (A_ID, a_pub, n_a), (B_ID, b_pub, n_b))
    assert side_a == side_b and len(side_a[1]) == 6 and side_a[1].isdigit()
    # Mêmes clés, autre nonce : autre code et autre clé (l'intermédiaire ne les prévoit pas)
    other = crypto.transcript_secrets(a_priv, (A_ID, a_pub, crypto.new_nonce()),
                                      (B_ID, b_pub, n_b))
    assert other[0] != side_a[0]
    # Une clé faible (secret commun nul) est refusée
    with pytest.raises(ValueError):
        crypto.transcript_secrets(a_priv, (A_ID, a_pub, n_a), (B_ID, bytes(32), n_b))


def test_a_commitment_binds_the_key_the_nonce_and_both_machines():
    _, pub = crypto.new_exchange()
    _, other = crypto.new_exchange()
    nonce = crypto.new_nonce()
    commit = crypto.commitment(A_ID, B_ID, pub, nonce)
    assert crypto.commitment_ok(commit, A_ID, B_ID, pub, nonce)
    assert not crypto.commitment_ok(commit, A_ID, B_ID, other, nonce)
    assert not crypto.commitment_ok(commit, A_ID, B_ID, pub, crypto.new_nonce())
    assert not crypto.commitment_ok(commit, M_ID, B_ID, pub, nonce)
    assert not crypto.commitment_ok(commit, A_ID, M_ID, pub, nonce)


def test_a_key_changed_after_the_commitment_is_refused(lan):
    """L'intermédiaire s'engage, voit la clé de B, puis voudrait révéler une autre clé
    (choisie pour obtenir un code voulu) : refusé, et la place reste prise."""
    _, b, _ = lan
    commit, _ = _commit_payload(M_ID)
    _, other_reveal = _commit_payload(M_ID)
    reply = b.pairing.incoming(commit, M_IP)
    assert reply["pub"] and reply["nonce"] and b.requests() == []     # rien de montré encore
    with pytest.raises(PeerError, match="engagement"):
        b.pairing.incoming(other_reveal, M_IP)
    assert b.requests() == [] and b.pairing.snapshot() == []
    # La place n'est pas libérée : pas de nouvel essai avant l'expiration
    with pytest.raises(PeerError, match="déjà un appairage en cours"):
        b.pairing.incoming(_commit_payload(M_ID)[0], M_IP)


def test_only_the_machine_that_committed_can_reveal(lan):
    _, b, _ = lan
    commit, reveal = _commit_payload()
    b.pairing.incoming(commit, A_IP)
    with pytest.raises(PeerError):
        b.pairing.incoming(reveal, M_IP)                   # même contenu, autre adresse
    assert b.requests() == []
    b.pairing.incoming(reveal, A_IP)
    assert len(b.requests()) == 1


def test_by_internet_the_reveal_needs_the_same_invitation(lan):
    _, b, _ = lan
    invitation = {"node_id": A_ID, "net_id": "d" * 64, "hints": {}, "secret": "e" * 32}
    commit, reveal = _commit_payload()
    b.pairing.incoming(commit, None, invitation)
    with pytest.raises(PeerError):
        b.pairing.incoming(reveal, None, dict(invitation, secret="f" * 32))
    with pytest.raises(PeerError):
        b.pairing.incoming(reveal, A_IP)                   # par le réseau local : non plus
    b.pairing.incoming(reveal, None, invitation)
    assert b.requests()[0]["internet"] is True


def test_attempts_are_limited_even_when_refused_at_once(lan):
    """Refuser une demande libère sa place, mais le nombre d'essais reste borné : un
    intermédiaire n'enchaîne pas les clés jusqu'à tomber sur le bon code."""
    _, b, _ = lan
    for i in range(pairing.MAX_STARTS):
        node_id = f"{i:032x}"
        commit, reveal = _commit_payload(node_id)
        b.pairing.incoming(commit, M_IP)
        b.pairing.incoming(reveal, M_IP)
        b.pairing.answer(node_id, False)                   # refusée tout de suite
    assert b.pairing.snapshot() == []
    with pytest.raises(PeerError, match="Trop de demandes"):
        b.pairing.incoming(_commit_payload("9" * 32)[0], M_IP)


def test_an_intermediary_that_does_not_reveal_cannot_try_again(lan):
    """Après la réponse de B, l'intermédiaire connaît le code qui s'afficherait ; s'il ne lui
    convient pas, il voudrait se taire et recommencer avec une autre clé. La place reste prise
    jusqu'à l'expiration, et les demandes ouvertes sont peu nombreuses : quelques essais à
    une chance sur un million, pas un million d'essais."""
    _, b, _ = lan
    b.pairing.incoming(_commit_payload(A_ID)[0], M_IP)     # au nom de A, jamais révélée
    with pytest.raises(PeerError, match="déjà un appairage en cours"):
        b.pairing.incoming(_commit_payload(A_ID)[0], M_IP)
    for i in range(pairing.MAX_PENDING - 1):               # sous d'autres identifiants
        b.pairing.incoming(_commit_payload(f"{i:032x}")[0], M_IP)
    with pytest.raises(PeerError, match="Trop de demandes"):
        b.pairing.incoming(_commit_payload("9" * 32)[0], M_IP)
    assert b.requests() == [] and b.pairing.snapshot() == []


def test_a_mirrored_key_is_refused():
    """Sa propre clé renvoyée par l'autre côté : pas un appairage avec une autre machine."""
    private, public = crypto.new_exchange()
    with pytest.raises(ValueError):
        crypto.transcript_secrets(private, (A_ID, public, crypto.new_nonce()),
                                  (B_ID, public, crypto.new_nonce()))


# ------------------------------------------- demandes jamais remplacées, réponse liée
def test_a_pending_request_is_never_replaced(lan):
    a, b, _ = lan
    a.pairing.start(B_IP, 47701)
    shown = b.pairing.snapshot()[0]
    # Un appareil du réseau local reprend l'identifiant de A, avec sa propre clé
    with pytest.raises(PeerError, match="déjà un appairage en cours"):
        b.pairing.incoming(_commit_payload(A_ID)[0], M_IP)
    assert b.pairing.snapshot() == [shown] and len(b.requests()) == 1
    # Côté A non plus : son appairage avec B n'est pas remplacé par une demande « de B »
    with pytest.raises(PeerError, match="déjà un appairage en cours"):
        a.pairing.incoming(_commit_payload(B_ID, A_ID, "PC fixe")[0], M_IP)
    assert a.pairing.get(B_ID).role == "initiator" and a.pairing.get(B_ID).address == B_IP


def test_the_answer_names_the_request_that_was_shown(lan):
    a, b, _ = lan
    a.pairing.start(B_IP, 47701)
    ref = b.requests()[0]["ref"]
    with pytest.raises(PeerError, match="a changé"):
        b.pairing.answer(A_ID, True, sharing.MINE, ref="0" * 16)
    assert b.pairing.get(A_ID).local_ok is None             # rien n'est parti
    a_ref = a.pairing.snapshot()[0]["ref"]
    a.pairing.answer(B_ID, True, sharing.MINE, ref=a_ref)
    b.pairing.answer(A_ID, True, sharing.MINE, ref=ref)
    assert peers.trust_of(A_ID) == sharing.MINE and peers.trust_of(B_ID) == sharing.MINE
    assert peers.get(A_ID)["key"] == peers.get(B_ID)["key"]


def test_an_already_paired_machine_is_replaced_only_on_purpose(lan):
    a, b, _ = lan
    old_key = bytes(range(32))
    peers.save(A_ID, "PC portable", old_key, A_IP, 47701, sharing.FRIEND)
    pending = a.pairing.start(B_IP, 47701)
    request = b.requests()[0]
    assert request["known"] == "PC portable"
    a.pairing.answer(B_ID, True, sharing.MINE, ref=pending.ref)
    with pytest.raises(PeerError, match="déjà appairée"):
        b.pairing.answer(A_ID, True, sharing.MINE, ref=request["ref"])
    assert peers.get(A_ID)["key"] == old_key and peers.trust_of(A_ID) == sharing.FRIEND
    b.pairing.answer(A_ID, True, sharing.MINE, ref=request["ref"], replace=True)
    assert peers.get(A_ID)["key"] == pending.key and peers.trust_of(A_ID) == sharing.MINE


def test_pairing_stops_if_another_machine_answers_at_that_address(lan):
    a, _, net = lan
    with pytest.raises(PairingRefused, match="pas celle de la liste"):
        a.pairing.start(B_IP, 47701, M_ID)
    assert net.sent == [] and a.pairing.snapshot() == []


# ---------------------------------------------------------- versions plus anciennes
def test_an_older_initiator_is_told_to_update(lan):
    """Une machine 2.0.1 (sans engagement) envoie sa clé tout de suite : refus clair, et
    jamais l'ancien échange."""
    _, b, _ = lan
    _, public = crypto.new_exchange()
    old = {"node_id": A_ID, "name": "Vieux PC", "port": 47701, "pub": envelope.b64e(public)}
    with pytest.raises(PairingRefused, match="Mets AIODOS à jour sur cette machine"):
        b.pairing.incoming(old, A_IP)
    assert b.pairing.get(A_ID) is None and b.requests() == []


def test_an_older_responder_is_told_to_update(lan, monkeypatch):
    a, _, _ = lan

    def old_responder(address, port, payload, timeout=10):
        # Ce que répond pairing.incoming de la 2.0.1 à une demande sans « pub »
        raise PeerError("Demande d'appairage incomplète.")
    monkeypatch.setattr(pairing.client, "pair_start", old_responder)
    with pytest.raises(PairingRefused, match="mets AIODOS à jour sur « PC fixe »"):
        a.pairing.start(B_IP, 47701, B_ID)
    assert a.pairing.get(B_ID) is None                     # la place est rendue

    def old_reply(address, port, payload, timeout=10):     # réponse sans nonce ni version
        return {"node_id": B_ID, "name": "PC fixe", "pub": envelope.b64e(bytes(32))}
    monkeypatch.setattr(pairing.client, "pair_start", old_reply)
    with pytest.raises(PairingRefused, match="mets AIODOS à jour"):
        a.pairing.start(B_IP, 47701, B_ID)


# ------------------------------------------------------------ vue Réseau (Node.js)
NODE = shutil.which("node")

HOSTILE = {
    "memory": {"total": "<img src=x onerror=alert(1)>"},
    "gpus": [{"name": "<b>x</b>", "vram_total": {"length": "<img src=x onerror=alert(2)>"}}],
    "ollama": {"models": {"length": "<img src=x onerror=alert(3)>"}},
    "power": {"mobile": True, "plugged": "<i>", "level": "<img src=x onerror=alert(4)>",
              "temperature": "<svg onload=alert(5)>"},
    "idle_seconds": "<img src=x onerror=alert(6)>",
    "version": "<script>alert(7)</script>",
}
HOSTILE_FRIEND = {"lends_models": True, "ollama": {"models": {"length": "<img onerror=alert(8)>"}}}
PHONE = {"memory": {"total": 8 * 1024 ** 3}, "gpus": [], "ollama": {"models": ["a", "b"]},
         "power": {"mobile": True, "plugged": True, "level": 84, "temperature": 31.2},
         "idle_seconds": 30, "version": "2.0.2"}

STUBS = {
    # Les appels à l'API et les fenêtres sont notés (globalThis) au lieu d'être faits
    "api.js": "export const get = async () => ({});\n"
              "export const post = async (url, body) => { (globalThis.posts ||= []).push({ url, body });\n"
              "  return { peers: [] }; };\n"
              "export const del = async () => ({});\n",
    "icons.js": "export const icon = () => '';\n",
    "ui.js": "export const modal = async opts => { (globalThis.modals ||= []).push(opts);\n"
             "  return { ok: true, how: 'yes', choice: 'friend' }; };\n"
             "export const menu = () => {};\n"
             "export const toast = () => {}; export const errorToast = e => { throw e; };\n"
             "export const confirmDanger = async () => false;\n",
    "views/connected.js": "export const renderConnected = () => {};\n",
}


def _run_network_js(tmp_path, script: str, *args) -> object:
    """network.js (avec dom.js et state.js tels quels, le reste remplacé par STUBS) sous
    Node.js : `script` importe ce qu'il veut de './js/views/network.js' et écrit son
    résultat en JSON."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    static = paths.STATIC_DIR / "js"
    for name in ("dom.js", "state.js", "views/network.js"):
        (js / name).write_bytes((static / name).read_bytes())
    for name, text in STUBS.items():
        (js / name).write_text(text, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (tmp_path / "run.js").write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "run.js"), *args], capture_output=True,
                          text=True, encoding="utf-8", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_a_hostile_profile_never_becomes_html(tmp_path):
    """Le profil reçu d'une machine appairée (un ami, ou une machine compromise) : un objet à
    la place d'un nombre, ou du HTML à la place d'un niveau de batterie, reste du texte."""
    hostile, friend, phone = _run_network_js(
        tmp_path,
        "import { peerFacts } from './js/views/network.js';\n"
        "const profiles = JSON.parse(process.argv[2]);\n"
        "console.log(JSON.stringify(profiles.map(profile => peerFacts({ profile }))));\n",
        json.dumps([HOSTILE, HOSTILE_FRIEND, PHONE]))
    for html in (hostile, friend):
        assert "<" not in html and ">" not in html, html
    assert "0 modèle(s)" in hostile and "&lt;script&gt;" in hostile
    assert "2 modèle(s)" in phone and "batterie 84 %" in phone and "31,2 °C" in phone
    assert "en charge" in phone and "utilisée en ce moment" in phone


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_pairing_window_answers_the_request_it_showed(tmp_path):
    """La fenêtre d'une demande reçue renvoie sa « ref », dit l'adresse de l'autre machine,
    et, pour une machine déjà appairée, l'annonce et demande le remplacement explicitement."""
    request = {"node_id": A_ID, "name": "PC portable", "code": "123456", "ref": "0123abcd" * 2,
               "address": A_IP, "internet": False, "known": "PC portable"}
    result = _run_network_js(
        tmp_path,
        "import { answerPairRequest } from './js/views/network.js';\n"
        "await answerPairRequest(JSON.parse(process.argv[2]));\n"
        "await answerPairRequest({ ...JSON.parse(process.argv[2]), known: null });\n"
        "console.log(JSON.stringify({ posts: globalThis.posts, modals: globalThis.modals }));\n",
        json.dumps(request))
    known, fresh = result["posts"]
    assert known == {"url": "/network/pair/answer", "body": {
        "node_id": A_ID, "accept": True, "trust": "friend", "ref": request["ref"],
        "replace": True}}
    assert fresh["body"]["ref"] == request["ref"] and fresh["body"]["replace"] is False
    shown, plain = result["modals"]
    assert A_IP in shown["text"] and "déjà appairée" in shown["text"] and shown["danger"]
    assert "remplacer" in shown["confirm"] and shown["cautious"]
    assert "déjà appairée" not in plain["text"] and not plain["danger"]


def test_the_caller_is_recognised_whatever_the_form_of_its_address():
    """En double pile, la même machine arrive en « ::ffff:192.168.1.20 » ou en
    « 192.168.1.20 » : la révélation de sa clé n'était pas reconnue (relecture de
    l'appairage, 21/10/2026). Une autre adresse reste refusée."""
    pending = pairing.Pending("x", "x", "192.168.1.20", 1, "responder", b"k" * 32, "1")
    assert pairing.Pairing._same_caller(pending, "::ffff:192.168.1.20", None)
    assert pairing.Pairing._same_caller(pending, "192.168.1.20", None)
    assert not pairing.Pairing._same_caller(pending, "192.168.1.21", None)
    assert not pairing.Pairing._same_caller(pending, None, None)
