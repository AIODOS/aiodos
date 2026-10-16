"""De bout en bout, avec de vrais nœuds et le vrai compagnon : une machine installée
(hors d'un dépôt git), sans compagnon, reçoit une version d'une machine appairée. Sa
version actuelle ne connaît pas les compagnons livrés, comme une 2.0.x : elle ne prend
que l'archive du code. La nouvelle version, une fois lancée, va chercher elle-même le
compagnon de sa plateforme, en vérifie l'empreinte, le met en place et le lance.
Il faut le compagnon compilé (cd compagnon && cargo build --release)."""
import json
import shutil

import pytest

from aiodos import paths
from aiodos.network import companion as companion_mod
from aiodos.updates import platforms
from aiodos.updates.store import Store, sha256_of
from tests.nodes import Node, pair
from tests.test_updates_end_to_end import copy_code, offered_to, stop, version_of
from tests.updates_kit import new_key, publish, zip_folder

REAL = companion_mod._built(paths.APP_ROOT)
HERE = platforms.tag()
pytestmark = pytest.mark.skipif(REAL is None or HERE is None or shutil.which("aiodos-net"),
                                reason="compagnon non compilé, ou déjà dans le PATH")


def internet(node: Node) -> dict:
    try:
        return node.api("GET", "/network")["internet"]
    except Exception:
        return {}                         # en plein redémarrage


def test_a_version_received_by_an_old_machine_fetches_and_runs_its_companion(tmp_path,
                                                                            monkeypatch):
    monkeypatch.delenv("AIODOS_NET_BIN", raising=False)
    private, trusted = new_key()
    lender_code = copy_code(tmp_path / "code-lender", "7.0.1", trusted)
    friend_code = copy_code(tmp_path / "code-friend", "7.0.0", trusted)
    old = friend_code / "aiodos" / "updates" / "service.py"      # la 7.0.0 fait comme une
    old.write_text(old.read_text(encoding="utf-8").replace(       # 2.0.x : pas de compagnon
        "        self._obtain_companion()\n", ""), encoding="utf-8")
    lender = Node(tmp_path / "data-lender", "Prêteur", ["--internet-local"], root=lender_code)
    friend = Node(tmp_path / "data-friend", "Ami", ["--internet-local"], root=friend_code)
    package = publish(Store(lender.data_dir / "updates"),
                      zip_folder(lender_code, tmp_path / "7.0.1.zip"), "7.0.1", private,
                      trusted, companions={HERE: REAL}, notes="Avec son compagnon")
    item = next(f for f in package["files"] if f["kind"] == f"aiodos-net-{HERE}")
    try:
        lender.start()
        friend.start()
        assert not internet(friend)["running"]           # aucun compagnon nulle part
        assert internet(lender)["program"] in (None, "livré avec la version 7.0.1")
        pair(lender, friend)
        friend.wait_for(lambda: offered_to(friend, "7.0.1"), what="profil du prêteur reçu")
        friend.api("POST", "/updates/check")
        friend.wait_for(lambda: version_of(friend) == "7.0.1", timeout=120,
                        what="redémarrage sur la version 7.0.1")
        friend.wait_for(lambda: friend.api("POST", "/updates/check") and
                        internet(friend).get("program") == "livré avec la version 7.0.1",
                        timeout=150, what="compagnon livré, reçu puis lancé")
        assert internet(friend)["running"]
        active = friend.data_dir / "updates" / "bin"
        meta = json.loads((active / "aiodos-net.json").read_text(encoding="utf-8"))
        assert meta == {"version": "7.0.1", "kind": item["kind"], "sha256": item["sha256"]}
        assert sha256_of(active / platforms.executable("aiodos-net")) == item["sha256"]
        log = (friend.data_dir / "logs" / "aiodos.log").read_text(encoding="utf-8")
        assert "Compagnon Internet de la version 7.0.1 reçu de « Prêteur »" in log
        assert "livré avec la version 7.0.1, version 0.1.0" in log
        assert friend.api("GET", "/updates")["offer"] == "7.0.1"
        # La machine transmet à son tour la version et le compagnon de sa plateforme
        profile = friend.api("GET", "/node")["updates"]
        assert item["kind"] in profile["files"]["7.0.1"]
    finally:
        stop(friend)
        stop(lender)
