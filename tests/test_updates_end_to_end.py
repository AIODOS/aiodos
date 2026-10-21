"""De bout en bout, avec de vrais nœuds : une machine installée (hors d'un dépôt git)
reçoit d'une machine appairée une version signée, l'installe, redémarre dessus, et
son lanceur la garde. Les deux machines font tourner une copie du code de travail,
qui fait confiance à une clé de publication jetable."""
import json
import re
import shutil
from pathlib import Path

import requests

from aiodos.updates.store import Store
from tests.nodes import APP_ROOT, Node, pair
from tests.updates_kit import new_key, publish, zip_folder


def copy_code(dest: Path, version: str, trusted: dict) -> Path:
    shutil.copytree(APP_ROOT / "aiodos", dest / "aiodos",
                    ignore=shutil.ignore_patterns("__pycache__"))
    init = dest / "aiodos" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"',
                           init.read_text(encoding="utf-8")), encoding="utf-8")
    (dest / "aiodos" / "updates" / "keys.py").write_text(
        f"TRUSTED_KEYS = {trusted!r}\nAPPROVER_KEYS = {{}}\n", encoding="utf-8")
    return dest


def stop(node: Node):
    """Le nœud, celui qui le tient après un redémarrage et le lanceur qui le surveille
    (cf. tests/nodes.Node.stop)."""
    node.stop()


def version_of(node: Node):
    try:
        return node.api("GET", "/updates")["version"]
    except (requests.RequestException, AssertionError):
        return None                       # en plein redémarrage, ou jeton déjà changé


def offered_to(node: Node, version: str) -> bool:
    return any(((p.get("profile") or {}).get("updates") or {}).get("offer") == version
               for p in node.api("GET", "/network")["peers"])


def test_installed_machine_updates_itself_from_a_paired_machine(tmp_path):
    private, trusted = new_key()
    lender_code = copy_code(tmp_path / "code-lender", "7.0.1", trusted)
    friend_code = copy_code(tmp_path / "code-friend", "7.0.0", trusted)
    lender = Node(tmp_path / "data-lender", "Prêteur", root=lender_code)
    friend = Node(tmp_path / "data-friend", "Ami", root=friend_code)
    publish(Store(lender.data_dir / "updates"), zip_folder(lender_code, tmp_path / "7.0.1.zip"),
            "7.0.1", private, trusted, notes="Version d'essai")
    try:
        lender.start()
        friend.start()
        lender_id, _ = pair(lender, friend)
        # Son profil tout de suite, sans attendre le tour suivant (sous la charge d'une suite
        # complète, 20 s ne suffisaient pas toujours)
        friend.api("POST", f"/network/peers/{lender_id}/refresh")
        friend.wait_for(lambda: offered_to(friend, "7.0.1"), timeout=40,
                        what="profil du prêteur reçu")
        friend.api("POST", "/updates/check")
        friend.wait_for(lambda: version_of(friend) == "7.0.1", timeout=120,
                        what="redémarrage sur la version 7.0.1")
        state = friend.data_dir / "updates" / "state.json"
        friend.wait_for(lambda: json.loads(state.read_text(encoding="utf-8")).get("current")
                        == "7.0.1", timeout=60, what="version gardée par le lanceur")
        status = friend.api("GET", "/updates")
        assert status["offer"] == "7.0.1"                  # transmise à son tour
        assert status["notice"]["kind"] == "updated"
        assert status["notice"]["previous"] == "7.0.0"
        assert status["notice"]["notes"] == "Version d'essai"
        assert friend.wait_for(lambda: [p for p in friend.api("GET", "/network")["peers"]
                                        if p["online"]], what="toujours appairée")
        log = (friend.data_dir / "logs" / "aiodos.log").read_text(encoding="utf-8")
        assert "Version 7.0.1 reçue de « Prêteur »" in log
        assert "Lancement de la version 7.0.1 (essai)" in log
    finally:
        stop(friend)
        stop(lender)
