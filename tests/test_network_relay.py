"""Un relais auto-hébergé sert deux machines qui ne peuvent pas se joindre en direct
(étape 2.7 de docs/phase2-internet.md).

Le relais est iroh-relay 1.3.0 en mode --dev (HTTP, sans certificat ; sur un port libre
plutôt que 3340), réservé par sa liste blanche aux clés de deux machines, comme le décrit
docs/relais.md. Chaque nœud lance son compagnon en « relais seulement »
(--internet-relay-only : aucune socket UDP, donc jamais de connexion directe). Les deux
nœuds ne se connaissent pas sur le réseau local : ils s'appairent sur invitation, et tout
passe ensuite par le relais. Une troisième machine, absente de la liste blanche, n'en
obtient rien. Il faut iroh-relay (cargo install iroh-relay --version 1.3.0 --features
server --locked) et le compagnon compilé ; le partage d'une indexation demande en plus
Ollama et le modèle d'embedding. Sans eux, seul reste le contrôle de l'exemple du guide
(la liste blanche doit y précéder [tls]).
"""
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos import settings
from aiodos.network import companion
from tests.nodes import APP_ROOT, Node, free_port, pair_by_invitation


def _relay_binary() -> Path | None:
    found = shutil.which("iroh-relay")
    if found:
        return Path(found)
    path = Path.home() / ".cargo" / "bin" / ("iroh-relay.exe" if sys.platform == "win32"
                                              else "iroh-relay")
    return path if path.is_file() else None


RELAY = _relay_binary()
EMBED_MODEL = settings.DEFAULTS["embed_model"]
NOTES = {
    "chats.txt": "Les chats dorment seize heures par jour, surtout l'après-midi.",
    "cafe.md": "# Café\nUn bon espresso se prépare en vingt-cinq secondes.",
    "pluie.txt": "En Bretagne, la pluie fine s'appelle le crachin.",
}

needs_relay = pytest.mark.skipif(
    companion.find_binary() is None or RELAY is None,
    reason="il faut le compagnon compilé (compagnon/README.md) et iroh-relay "
           "(cargo install iroh-relay --version 1.3.0 --features server --locked)")


def _ollama_has_embed_model() -> bool:
    try:
        tags = requests.get("http://127.0.0.1:11434/api/tags", timeout=2).json()
    except (requests.RequestException, ValueError):
        return False
    return any(m["name"].startswith(EMBED_MODEL) for m in tags.get("models", []))


def _machine_key(data_dir: Path) -> str:
    """Clé Internet d'une machine, créée avant le premier lancement de son compagnon
    (même fichier que lui), et sa moitié publique : l'hébergeur du relais l'inscrit dans
    la liste blanche avant que la machine ne s'y présente."""
    seed = os.urandom(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "aiodos-net.key").write_text(seed.hex() + "\n", encoding="ascii")
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw().hex()


class Relay:
    """iroh-relay --dev, avec le fichier de réglages de docs/relais.md (liste blanche)."""

    def __init__(self, folder: Path, allowed: list):
        folder.mkdir(parents=True, exist_ok=True)
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        config = folder / "relais.toml"
        keys = ", ".join(f'"{key}"' for key in allowed)
        config.write_text(f'http_bind_addr = "127.0.0.1:{port}"\n'
                          "enable_metrics = false\n"
                          f"access.allowlist = [{keys}]\n", encoding="utf-8")
        self.log = folder / "relais.log"
        with open(self.log, "wb") as log:
            self.process = subprocess.Popen(
                [str(RELAY), "--dev", "--config-path", str(config)], stdout=log,
                stderr=subprocess.STDOUT, env=dict(os.environ, RUST_LOG="iroh_relay=info"))
        deadline = time.time() + 20
        while True:
            try:
                requests.get(self.url, timeout=1, proxies={"http": None})
                return
            except requests.RequestException:
                if self.process.poll() is not None or time.time() > deadline:
                    self.stop()
                    raise RuntimeError("Le relais n'a pas démarré :\n" + self.log.read_text(
                        encoding="utf-8", errors="replace")[-2000:])
                time.sleep(0.1)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)


@pytest.fixture(scope="module")
def place(tmp_path_factory):
    """Dossiers des trois machines, et le relais qui n'admet que les deux premières."""
    root = tmp_path_factory.mktemp("relais")
    dirs = {name: root / name for name in ("a", "b", "c")}
    keys = {name: _machine_key(folder) for name, folder in dirs.items()}
    relay = Relay(root / "relais", [keys["a"], keys["b"]])
    yield relay, dirs, keys
    relay.stop()


def _start(place, name, label, args=()):
    relay, dirs, keys = place
    node = Node(dirs[name], label, [*args, "--internet-relay-only", relay.url]).start()
    state = node.wait_for(lambda: (s := node.api("GET", "/network/internet"))["running"] and s,
                          what="compagnon prêt")
    assert state["id"] == keys[name] and state["relay_only"], state
    return node


def _peer(node, node_id):
    return next((p for p in node.api("GET", "/network")["peers"] if p["node_id"] == node_id), None)


@pytest.fixture(scope="module")
def two_machines(place):
    """A (sans embeddings locaux : son travail part ailleurs) et B, appairées par le relais."""
    a = _start(place, "a", "PC de la maison", ["--slots", "embed=0"])
    b = _start(place, "b", "PC du bureau")
    try:
        a_id, b_id = pair_by_invitation(a, b, trust="mine")    # sans réseau local
        yield a, b, a_id, b_id
    finally:
        a.stop()
        b.stop()


def _path(node, other_id):
    """Chemin vers cette machine, tel que la vue Réseau le montre (état relu à l'instant)."""
    node.api("GET", "/network/internet")
    return ((_peer(node, other_id) or {}).get("internet") or {}).get("path")


def _seen_by_companion(node, other) -> dict:
    key = other.api("GET", "/network/internet")["id"]
    peers = node.api("GET", "/network/internet")["companion"]["peers"]
    return next((p for p in peers if p["id"] == key), {})


def _job(node, job_id):
    return next((j for j in node.api("GET", "/jobs")["jobs"] if j["id"] == job_id), None)


def test_the_guide_puts_the_allowlist_where_the_relay_reads_it():
    """docs/relais.md : dans l'exemple, la liste blanche précède [tls]. Après, TOML la
    rangerait dans cette section, et iroh-relay l'ignorerait sans rien dire : le relais
    servirait tout le monde (vérifié avec iroh-relay 1.3.0)."""
    guide = (APP_ROOT / "docs" / "relais.md").read_text(encoding="utf-8")
    example = tomllib.loads(re.search(r"```toml\n(.*?)```", guide, re.S).group(1))
    assert example.get("access", {}).get("allowlist"), example
    assert "access" not in example.get("tls", {}), example


@needs_relay
def test_the_self_hosted_relay_joins_two_machines_without_a_direct_path(two_machines, place):
    a, b, a_id, b_id = two_machines
    relay, _, _ = place
    for node in (a, b):
        state = node.api("GET", "/network/internet")
        assert state["relays"] == [relay.url + "/"] and state["denied_by"] == []
        status = state["companion"]
        assert status["addrs"] == [] and status["bound"] == [], "aucune adresse directe"
    # Les machines appairées ne se connaissent que par Internet : pas d'adresse locale
    assert not _peer(a, b_id)["address"] and not _peer(b, a_id)["address"]
    # Des nouvelles dans les deux sens, puis la vue Réseau dit par où elles passent
    for node, other_id in ((a, b_id), (b, a_id)):
        peer = node.api("POST", f"/network/peers/{other_id}/refresh")
        assert _peer(node, other_id)["online"], peer
        assert node.wait_for(lambda: _path(node, other_id), what="chemin connu") == "relay"
    profile = a.wait_for(lambda: _peer(a, b_id).get("profile"), timeout=40, what="profil de B")
    assert profile["name"] == "PC du bureau"
    seen = _seen_by_companion(a, b)
    assert seen["path"] == "relay" and seen["remote"] == relay.url + "/"
    assert seen["connections"] == 1


@needs_relay
@pytest.mark.skipif(not _ollama_has_embed_model(), reason=f"Ollama avec {EMBED_MODEL} requis")
def test_work_goes_through_the_self_hosted_relay(two_machines, tmp_path):
    a, b, a_id, b_id = two_machines
    b.api("PUT", "/settings", {"share_enabled": True, "share_when_idle": False,
                               "share_on_ac_only": False})
    folder = tmp_path / "notes"
    folder.mkdir()
    for name, text in NOTES.items():
        (folder / name).write_text(text, encoding="utf-8")
    before = _seen_by_companion(b, a).get("requests_out", 0)
    collection = a.api("GET", "/collections")["collections"][0]["id"]
    job_id = a.api("POST", "/index", {"paths": [str(folder)],
                                      "collection_id": collection})["job_id"]
    job = a.wait_for(lambda: (j := _job(a, job_id)) and j["status"] not in
                     ("queued", "running") and j, timeout=180, what="indexation terminée")
    assert job["status"] == "done" and job["done"] == 3, job

    entries = a.api("GET", f"/entries?collection_id={collection}")
    assert sorted(e["source"] for e in entries) == sorted(f"notes/{n}" for n in NOTES)
    assert b.api("GET", "/entries") == []            # rien n'est resté chez B
    assert _peer(a, b_id)["received_units"] == 3
    # Bail, fichier et résultat de chaque unité sont passés par le relais
    assert _seen_by_companion(b, a)["requests_out"] - before >= 9
    assert _path(a, b_id) == _path(b, a_id) == "relay"


@needs_relay
def test_the_relay_refuses_a_machine_outside_its_allowlist(two_machines, place):
    a, _, _, _ = two_machines
    relay, _, _ = place
    c = _start(place, "c", "PC d'un inconnu")
    try:
        state = c.wait_for(lambda: (s := c.api("GET", "/network/internet"))["denied_by"] and s,
                           what="refus du relais")
        assert state["denied_by"] == [relay.url + "/"] and state["relays"] == []
        # A invite C, et C répond : chacun admet l'autre dans son compagnon. Seul le
        # relais fait obstacle, et il ne transmet rien à une clé qu'il refuse.
        invitation = a.api("POST", "/network/invitations")
        reply = c.api("POST", "/network/invitations/accept", {"code": invitation["code"]})
        refused = a.api("POST", f"/network/invitations/{invitation['id']}/reply",
                        {"code": reply["reply"]}, expect=400)
        assert "injoignable par Internet" in refused["detail"], refused
        assert c.api("GET", "/network")["pairing"] == []
        # Rien n'est parvenu jusqu'au compagnon de A : le relais a tout arrêté avant
        assert a.api("GET", "/network/internet")["companion"]["refused"]["count"] == 0
    finally:
        c.stop()
