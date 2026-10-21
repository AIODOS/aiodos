"""Faille 34 (audit du 20/10/2026) et fin de la relecture des mises à jour (points 2 à 4).

Quand la version courante ne démarre pas, le lanceur rend la main à la version d'origine.
Une 2.0.x ne connaît ni les amis ni les machines mises de côté : elle traiterait un ami
comme une de mes machines, avec sa clé, là où les clés sont en clair (Termux, Linux).

- Un nœud dont la version d'origine ignore les amis (launcher.friends_refused) n'en
  accepte aucun : ni par code (pairing.py), ni présenté par groupe (group.py, chez F comme
  chez A, et _store en dernier recours). Le refus dit quoi faire ; rien n'est gardé, rien
  n'est refusé pour de bon. Une de mes machines s'appaire toujours.
- Hors DPAPI, la clé d'un ami ou d'une machine mise de côté est marquée (vault.HELD,
  « k2: ») : une 2.0.x, qui la lit telle quelle, n'en peut rien faire (35 octets, refusés
  par ChaCha20-Poly1305). La clé suit le niveau et la mise de côté ; la migration 18 marque
  les paires d'avant.
- L'approbation porte l'empreinte du texte montré (api/routes/updates.py, approval.js), et
  la vue Mises à jour dit pourquoi un nœud refuse les amis (updates.js).

Les machines sont jouées dans ce processus (pas de réseau) ; l'interface, avec Node.js."""
import json
import logging
import shutil
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from aiodos import db, paths
from aiodos.network import crypto, envelope, group, pairing, peers, sharing
from aiodos.network.client import PeerError
from aiodos.node import vault
from aiodos.updates import launcher

ME, MATE, LEA, NEW, OTHER = "e" * 32, "f" * 32, "a" * 32, "1" * 32, "2" * 32
KEY = bytes(range(32))
K2 = b"k2:"            # la marque telle qu'écrite sur le disque (vault.HELD) : un format gardé
CARD = {"node_id": NEW, "name": "Téléphone de Léa", "address": "127.0.0.1", "port": 9}
NODE = shutil.which("node")
windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI : Windows seulement")


class Machine:
    """Le minimum du gestionnaire dont l'appairage et les présentations se servent."""
    p2p_port = 47701

    def __init__(self):
        self.node_id = ME
        self.events, self.presented = [], []
        self.pairing = pairing.Pairing(self)
        self.group = group.Group(self)

    def name(self):
        return "Téléphone"

    def publish(self, event):
        self.events.append(event)

    def changed(self):
        pass

    def net_changed(self, *args):
        pass

    def on_paired(self, node_id):
        pass

    def present_later(self, friend_id, only=None, delay=0):
        self.presented.append(friend_id)

    def present_new_machine(self, node_id):
        pass


@pytest.fixture
def termux(monkeypatch):
    """Pas de DPAPI ici (Termux, Linux, macOS, Android) : les clés sont en clair."""
    db.connect()
    monkeypatch.setattr(vault, "available", lambda: False)


@pytest.fixture
def old_origin(termux, tmp_path, monkeypatch):
    """Ce nœud tourne sous le lanceur d'une version d'origine 2.0.1, installée par archive :
    le vrai launcher.friends_refused le dit. → son message."""
    base = tmp_path / "origine"
    (base / "aiodos").mkdir(parents=True)
    (base / "aiodos" / "__init__.py").write_text('__version__ = "2.0.1"\n', encoding="utf-8")
    monkeypatch.setenv(launcher.CHILD_ENV, "1")
    monkeypatch.setenv(launcher.BASE_ENV, str(base))
    reason = launcher.friends_refused()
    assert reason and "2.0.1" in reason and "Mets AIODOS à jour" in reason
    return reason


@pytest.fixture
def mine(termux, monkeypatch):
    """Ce nœud, avec une de mes machines (MATE) et la machine d'une amie (LEA)."""
    machine = Machine()
    monkeypatch.setattr(group.Group, "_background", staticmethod(lambda f, *a: f(*a)))
    peers.save(MATE, "PC fixe", b"f" * 32, "127.0.0.1", 1, sharing.MINE)
    peers.save(LEA, "PC de Léa", b"a" * 32, "127.0.0.1", 2, sharing.FRIEND)
    return machine


def _raw(node_id) -> bytes:
    """La clé telle qu'une 2.0.x la lirait : la colonne, sans rien retirer (v2.0.1 peers.get)."""
    return bytes(db.query_one("SELECT key FROM peers WHERE node_id=?", (node_id,))["key"])


def _usable_by_2_0_x(key: bytes) -> bool:
    """Ce que la v2.0.1 fait de la clé lue (crypto.seal, open_sealed) : ChaCha20-Poly1305."""
    try:
        ChaCha20Poly1305(key).encrypt(bytes(12), b"work/lease", b"")
    except ValueError:
        return False
    return True


def _ready(machine, node_id=LEA, name="PC de Léa") -> pairing.Pending:
    """Une demande d'appairage reçue, code calculé et affiché (comme après la révélation)."""
    pending = pairing.Pending(node_id, name, "192.168.1.20", 47701, pairing.RESPONDER, KEY,
                              "123456")
    with machine.pairing._lock:
        machine.pairing._pending[node_id] = pending
    return pending


def _pub() -> str:
    return envelope.b64e(crypto.new_exchange()[1])


def _calls(monkeypatch, reply=None) -> list:
    sent = []
    monkeypatch.setattr(group.client, "call", lambda my_id, peer, action, payload, *a, **k:
                        sent.append((peer["node_id"], action, payload)) or (reply or {"ok": True}))
    return sent


# ============================================================= appairage par code
def test_a_friend_is_not_paired_where_the_original_version_ignores_friends(old_origin,
                                                                          monkeypatch):
    machine = Machine()
    pending = _ready(machine)
    sent = []
    monkeypatch.setattr(pairing.client, "call", lambda *a, **k: sent.append(a) or {"accept": True})
    with pytest.raises(pairing.PairingRefused) as refused:
        machine.pairing.answer(LEA, True, sharing.FRIEND, ref=pending.ref)
    assert str(refused.value) == old_origin                 # le message dit quoi faire
    assert sent == [] and pending.local_ok is None and peers.trust_of(LEA) is None
    assert machine.pairing.get(LEA) is pending              # toujours affichée : à refuser
    machine.pairing.answer(LEA, False, sharing.FRIEND, ref=pending.ref)
    assert machine.pairing.get(LEA) is None and peers.trust_of(LEA) is None
    # Une de mes machines s'y appaire toujours : une 2.0.x la traiterait comme ce qu'elle est
    pending = _ready(machine, MATE, "PC fixe")
    machine.pairing.answer(MATE, True, sharing.MINE, ref=pending.ref)
    assert peers.trust_of(MATE) == sharing.MINE and peers.get(MATE)["key"] == KEY
    assert _usable_by_2_0_x(_raw(MATE))


def test_elsewhere_a_friend_is_paired_and_kept_out_of_reach_of_a_2_0_x(termux, monkeypatch):
    assert launcher.friends_refused() is None               # pas lancé par un lanceur
    machine = Machine()
    pending = _ready(machine)
    monkeypatch.setattr(pairing.client, "call", lambda *a, **k: {"accept": True})
    machine.pairing.answer(LEA, True, sharing.FRIEND, ref=pending.ref)
    assert peers.trust_of(LEA) == sharing.FRIEND and peers.get(LEA)["key"] == KEY
    assert not _usable_by_2_0_x(_raw(LEA))
    assert _raw(LEA) == K2 + KEY and vault.HELD == K2


# ========================================================== appairage par groupe
def test_my_machine_presented_to_a_friend_refuses_at_once(old_origin, mine):
    """Chez F (une de mes machines, qui garderait l'amie en amie) : refus dès la
    présentation, sans clé préparée, sans refus pour de bon (réinstallée, elle acceptera)."""
    reply = mine.group.introduced(MATE, {"intro": "i1", "machine": CARD})
    assert reply == {"declined": True, "refused": old_origin}
    assert mine.group._waiting == {} and not group.declined(NEW)
    with pytest.raises(PeerError, match="inconnue ou expirée"):
        mine.group.keyed(MATE, {"intro": "i1", "pub": _pub()})
    assert peers.trust_of(NEW) is None


def test_a_presentation_by_a_friend_is_refused_before_any_question(old_origin, mine):
    """Chez A : présentée par une amie, la machine serait gardée en amie. Refus, même avec
    « toujours accepter ce groupe » ; ni question, ni paire, ni refus pour de bon."""
    group.set_group_ok(LEA, True)
    reply = mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()})
    assert reply == {"accept": False, "refused": old_origin}
    assert peers.trust_of(NEW) is None and mine.group.offers() == [] and mine.events == []
    assert not group.declined(NEW)
    # Présentée par une de mes machines, elle serait des miennes : une 2.0.x la traiterait
    # comme telle, la question est posée
    assert mine.group.proposed(MATE, {"intro": "i2", "machine": CARD, "pub": _pub()}) == {
        "pending": True}


def test_the_presenter_notes_why_and_keeps_nothing_waiting(mine, monkeypatch, caplog):
    """Chez P : le refus de l'amie arrive comme un refus (« refused »), sa raison est notée
    au journal (sur une ligne), et ma machine n'attend plus."""
    reason = "Ce nœud a été installé avec AIODOS 2.0.1,\nqui ne connaît pas les amis."

    def call(my_id, peer, action, payload, *a, **k):
        sent.append((peer["node_id"], action))
        if action == "group/introduce":
            return {"pub": _pub()}
        if action == "group/propose":
            return {"accept": False, "refused": reason}
        return {"ok": True}
    sent = []
    monkeypatch.setattr(group.client, "call", call)
    with caplog.at_level(logging.INFO, logger="aiodos.network"):
        assert mine.group.present(LEA)["refused"] == ["PC fixe"]
    assert "2.0.1, qui ne connaît pas les amis" in caplog.text
    assert (MATE, "group/cancel") in sent and mine.group._sent == {}


def test_an_answer_given_after_the_fact_is_refused_and_said(mine, monkeypatch):
    """Défense en profondeur : une question affichée avant que le refus ne vaille. Accepter
    ne garde rien, le dit, prévient celle qui présentait, sans « toujours » ni refus pour de
    bon."""
    assert mine.group.proposed(LEA, {"intro": "i1", "machine": CARD, "pub": _pub()}) == {
        "pending": True}
    reason = "Ce nœud a été installé avec AIODOS 2.0.1, qui ne connaît pas les amis."
    monkeypatch.setattr(launcher, "friends_refused", lambda: reason)
    sent = _calls(monkeypatch)
    with pytest.raises(PeerError, match="installé avec AIODOS 2.0.1"):
        mine.group.answer("i1", True, always=True)
    assert peers.trust_of(NEW) is None and not group.declined(NEW) and not group.group_ok(LEA)
    assert sent == [(LEA, "group/answer", {"intro": "i1", "accept": False, "refused": reason})]
    assert mine.group.offers() == []


def test_a_friend_is_never_stored_there_even_past_the_presentations(old_origin, mine,
                                                                    monkeypatch):
    with pytest.raises(PeerError, match="Mets AIODOS à jour"):
        mine.group._store(CARD, KEY, sharing.FRIEND, MATE)
    assert peers.trust_of(NEW) is None
    # Une présentation préparée avant (friends_refused changé entre-temps) : la clé arrive,
    # rien n'est gardé
    monkeypatch.setattr(launcher, "friends_refused", lambda: None)
    assert "pub" in mine.group.introduced(MATE, {"intro": "i1", "machine": CARD})
    monkeypatch.setattr(launcher, "friends_refused", lambda: old_origin)
    with pytest.raises(PeerError, match="Mets AIODOS à jour"):
        mine.group.keyed(MATE, {"intro": "i1", "pub": _pub()})
    assert peers.trust_of(NEW) is None
    mine.group._store(CARD, KEY, sharing.MINE, MATE)        # une des miennes : gardée
    assert peers.trust_of(NEW) == sharing.MINE


# ================================================ clés hors de portée d'une 2.0.x
def test_without_dpapi_the_key_follows_the_trust_and_the_setting_aside(termux):
    peers.save(MATE, "PC fixe", KEY, "127.0.0.1", 1)                   # une des miennes
    peers.save(LEA, "PC de Léa", KEY, "127.0.0.1", 2, sharing.FRIEND)
    assert _usable_by_2_0_x(_raw(MATE)) and not _usable_by_2_0_x(_raw(LEA))
    assert _raw(MATE) == KEY and _raw(LEA) == K2 + KEY
    steps = [(lambda: peers.set_trust(MATE, sharing.FRIEND), False),   # devenue amie
             (lambda: peers.set_trust(MATE, sharing.MINE), True),      # de nouveau à moi
             (lambda: peers.set_aside(MATE, "contrôle échoué"), False),
             (lambda: peers.set_trust(MATE, sharing.MINE), False),     # toujours de côté
             (lambda: peers.set_aside(MATE, None), True),              # rétablie
             (lambda: peers.set_aside(LEA, "contrôle échoué"), False),
             (lambda: peers.set_aside(LEA, None), False)]              # amie rétablie : amie
    for number, (step, usable) in enumerate(steps):
        step()
        assert _usable_by_2_0_x(_raw(MATE if number < 5 else LEA)) is usable, number
    assert _raw(MATE) == KEY and _raw(LEA) == K2 + KEY
    assert peers.get(MATE)["key"] == KEY == peers.get(LEA)["key"]
    assert peers.set_trust("9" * 32, sharing.FRIEND) is False          # pas appairée
    peers.set_aside("9" * 32, "rien")


def test_a_clear_key_that_starts_like_the_mark_is_not_cut(termux):
    """Une clé en clair a 32 octets, une clé marquée 35 : une clé tirée au hasard qui
    commence par « k2: » n'est ni coupée à la lecture, ni laissée utilisable."""
    odd = K2 + bytes(range(29))                            # 32 octets en clair
    peers.save(LEA, "PC de Léa", odd, "127.0.0.1", 2, sharing.FRIEND)
    assert peers.get(LEA)["key"] == odd and not _usable_by_2_0_x(_raw(LEA))
    assert not vault.held(odd) and vault.unprotect(odd) == odd and vault.release(odd) == odd
    marked = vault.hold(odd)
    assert vault.unprotect(marked) == odd and vault.hold(marked) == marked
    for other in (b"", b"court", vault.MARK + bytes(64)):  # pas une clé de paire en clair
        assert vault.hold(other) == other


def test_the_migration_holds_the_keys_paired_before(termux):
    """Les paires enregistrées en clair avant cette version : celles d'un ami ou d'une
    machine mise de côté sont marquées, octet pour octet ; rien d'autre ne change, et la
    migration rejouée ne marque rien deux fois."""
    path = db.connect().execute("PRAGMA database_list").fetchone()[2]
    sealed = vault.MARK + bytes(range(180))
    rows = {MATE: (KEY, "mine", None),                     # une des miennes : telle quelle
            LEA: (b"\x00\xff" * 16, "friend", None),       # une amie : marquée
            NEW: (KEY, "mine", 1.0),                       # mise de côté : marquée
            OTHER: (K2 + bytes(range(29)), "friend", None),   # en clair, malgré son début
            "3" * 32: (K2 + KEY, "friend", None),          # déjà marquée
            "4" * 32: (sealed, "friend", None)}            # scellée par DPAPI
    for node_id, (key, trust, aside) in rows.items():
        db.execute("INSERT INTO peers(node_id, name, key, address, port, paired_at, trust, "
                   "set_aside_at) VALUES(?, ?, ?, '127.0.0.1', 1, 0, ?, ?)",
                   (node_id, node_id[:4], key, trust, aside))
    for _ in range(2):
        db.execute("PRAGMA user_version=17")               # base d'avant cette version
        db.close()
        db._initialized.discard(path)
        assert db.query_one("PRAGMA user_version")[0] == db.SCHEMA_VERSION
        assert _usable_by_2_0_x(_raw(MATE))
        for node_id in (LEA, NEW, OTHER):
            assert not _usable_by_2_0_x(_raw(node_id)), node_id
            assert _raw(node_id) == K2 + rows[node_id][0]
            assert peers.get(node_id)["key"] == rows[node_id][0]
        assert _raw(MATE) == KEY and _raw("3" * 32) == K2 + KEY
        assert _raw("4" * 32) == sealed


@windows
def test_with_dpapi_a_friend_key_is_sealed_and_a_marked_one_sealed_later(monkeypatch):
    """Sous Windows, DPAPI fait déjà l'affaire ; s'il manque au moment de l'appairage, la
    marque garde la clé, puis protect_keys la scelle sans sa marque."""
    db.connect()
    peers.save(LEA, "PC de Léa", KEY, "127.0.0.1", 2, sharing.FRIEND)
    assert vault.is_protected(_raw(LEA)) and not _usable_by_2_0_x(_raw(LEA))

    def broken(function, data):
        raise vault.Unreadable("DPAPI indisponible")
    with monkeypatch.context() as patched:
        patched.setattr(vault, "_call", broken)
        peers.save(NEW, "Téléphone de Léa", KEY, "127.0.0.1", 3, sharing.FRIEND)
        assert not _usable_by_2_0_x(_raw(NEW)) and _raw(NEW) == K2 + KEY
    assert peers.protect_keys() == 1 and peers.protect_keys() == 0
    vault._opened.clear()                                  # relue par DPAPI, pas de mémoire
    assert vault.is_protected(_raw(NEW)) and peers.get(NEW)["key"] == KEY


# ======================================================= interface (Node.js)
_STUBS = {
    "api.js": """
export const posted = [];
export async function get(path) { return structuredClone(globalThis.answers[path]); }
export async function post(path, body = {}) { posted.push({ path, body }); return { ok: true }; }
""",
    "icons.js": "export const icon = () => '';\n",
    "ui.js": """
export const asked = [];
export function toast() {}
export function errorToast(e) { throw e; }
export async function confirmDanger(title, text) { asked.push(text); return globalThis.confirmed; }
""",
    "state.js": "export const state = {};\nexport function emit() {}\n",
}

# Juste assez de DOM pour les vues : un élément par sélecteur, et les boutons « Approuver »
# lus dans le HTML écrit
_DOM = """
const settle = () => new Promise(done => setTimeout(done, 0));   // la vue redessinée
class El {
  constructor() { this.innerHTML = ''; this.found = {}; this.dataset = {}; this.buttons = []; }
  querySelector(sel) { return (this.found[sel] ??= new El()); }
  querySelectorAll() {
    this.buttons = [...this.innerHTML.matchAll(/data-approve="(\\d+)"/g)].map(m => {
      const b = new El(); b.dataset.approve = m[1]; return b;
    });
    return this.buttons;
  }
}
"""


def _run_views(tmp_path, script: str) -> dict:
    """Les vraies vues (approval.js, updates.js) et dom.js, le reste remplacé (api, ui…)."""
    js = tmp_path / "js"
    (js / "views").mkdir(parents=True)
    (js / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    static = paths.STATIC_DIR / "js"
    (js / "dom.js").write_bytes((static / "dom.js").read_bytes())
    for name in ("approval.js", "updates.js"):
        (js / "views" / name).write_bytes((static / "views" / name).read_bytes())
    for name, text in _STUBS.items():
        (js / name).write_text(text, encoding="utf-8")
    (js / "essai.js").write_text(_DOM + script, encoding="utf-8")
    done = subprocess.run([NODE, str(js / "essai.js")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert done.returncode == 0, done.stderr[:600]
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_approval_view_shows_and_sends_the_text_it_signs(tmp_path):
    """Relecture : approval.js n'envoyait ni n'affichait l'empreinte, ni l'avertissement
    « conflict » ; deux lignes « 2.0.1 » s'affichaient pareilles."""
    first, second = "a" * 64, "b" * 64
    out = _run_views(tmp_path, f"""
import {{ renderApproval }} from './views/approval.js';
import {{ posted }} from './api.js';
import {{ asked }} from './ui.js';
const line = (digest, conflict) => ({{ version: '2.0.1', machine: 'PC portable',
  node_id: 'e'.repeat(32), notes: '', commit: '0'.repeat(40), published: 0, digest, conflict,
  warning: conflict ? 'Plusieurs textes sous ce numéro <img src=x onerror=alert(1)>' : '',
  files: [{{ kind: 'source', name: 'aiodos-2.0.1.zip', size: 1, sha256: '0'.repeat(64) }}] }});
globalThis.answers = {{
  '/updates/approval': {{ key: {{ id: 'k', declared: true, line: '' }}, required: true,
                          waiting_here: [] }},
  '/updates/approval/pending': {{ pending: [line('{first}', false)] }},
}};
const box = new El();
await renderApproval(box);
await box.querySelector('#approval-look').onclick();
const list = box.querySelector('#approval-list');
const single = list.innerHTML;
await list.buttons[0].onclick();
await settle();
const plain = {{ posted: posted.splice(0), asked: asked.splice(0) }};
globalThis.answers['/updates/approval/pending'] = {{
  pending: [line('{first}', true), line('{second}', true)] }};
await box.querySelector('#approval-look').onclick();
const doubled = list.innerHTML;
globalThis.confirmed = false;
await list.buttons[1].onclick();
const declined = {{ posted: posted.splice(0), asked: asked.splice(0) }};
globalThis.confirmed = true;
await list.buttons[1].onclick();
await settle();
const confirmed = {{ posted: posted.splice(0), asked: asked.splice(0) }};
console.log(JSON.stringify({{ single, plain, doubled, declined, confirmed }}));
""")
    assert first[:16] in out["single"] and "Plusieurs textes" not in out["single"]
    assert out["plain"] == {"posted": [{"path": "/updates/approval/approve", "body": {
        "node_id": "e" * 32, "version": "2.0.1", "digest": first}}], "asked": []}
    # Deux textes sous un numéro : dit sur chaque ligne, échappé, chaque empreinte visible
    assert out["doubled"].count("Plusieurs textes sous ce numéro") == 2
    assert "<img" not in out["doubled"] and "&lt;img" in out["doubled"]
    assert first[:16] in out["doubled"] and second[:16] in out["doubled"]
    # Approuver demande confirmation ; refusée, rien ne part ; acceptée, c'est CE texte-là
    assert out["declined"]["posted"] == [] and len(out["declined"]["asked"]) == 1
    assert second in out["declined"]["asked"][0]
    assert [p["body"]["digest"] for p in out["confirmed"]["posted"]] == [second]


@pytest.mark.skipif(NODE is None, reason="Node.js absent")
def test_the_updates_view_says_why_friends_are_refused(tmp_path):
    out = _run_views(tmp_path, """
import { updatesStatus } from './views/updates.js';
import { state } from './state.js';
const reason = 'Ce nœud a été installé avec AIODOS 2.0.1, qui ne connaît pas les amis <b>x</b>';
state.updates = { version: '2.0.2', auto: true, friends_refused: reason };
const refused = updatesStatus();
state.updates = { version: '2.0.2', auto: true, friends_refused: null };
console.log(JSON.stringify({ refused, fine: updatesStatus() }));
""")
    assert "installé avec AIODOS 2.0.1, qui ne connaît pas les amis" in out["refused"]
    assert "&lt;b&gt;" in out["refused"] and "<b>x" not in out["refused"]
    assert 'class="warn"' in out["refused"] and 'class="warn"' not in out["fine"]
